# Online image search for AI Create.
#
# Ported from Patrick's claude_cards (image_fetcher.py + ui/browser_dialog.py).
# Backends, each independently toggleable in Settings → AI Create → Images:
#   • Wikipedia / Wikimedia (REST media-list + summary)      — reliable, no key
#   • NLM Open-i biomedical image database                   — reliable, no key
#   • DermNet (dermatology)                                  — best-effort scrape
#   • Radiopaedia (radiology)                                — best-effort scrape
#   • Bing Image Search                                      — official API, key-gated
# The scrapers are fragile by nature (they break when the site restyles) and
# fail silently; Bing only runs when an API key is configured.
#
# Plus an in-Anki "visual browse" dialog: an embedded webview pointed at an
# image search engine where right-clicking any image offers "Attach to card(s)"
# — robust because it reads Qt's context-menu media URL, not scraped HTML.
#
# The picker downloads the chosen full-res image(s) to temp files and returns
# their paths, so they ride the SAME pipeline as "📷 Attach images for Extra"
# (imported into Anki media at card-creation time). Fail-open throughout.

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from aqt import mw
from aqt.qt import (
    QDialog, QDialogButtonBox, QHBoxLayout, QIcon, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPixmap, QPushButton, QSize, Qt, QVBoxLayout,
)

from ..core import log
from ..core.qt_utils import theme_dialog

# A descriptive, browser-shaped User-Agent. Wikimedia's upload host (and a few
# others) return 403 for terse bot-looking agents — that rejection hit BOTH the
# thumbnail previews and the full-image download, so cards showed source buttons
# with no preview and "couldn't download". A real-looking UA fixes both.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36 "
       "Ankisstant/1.8 (Anki addon)")

# Shared, bounded pool for ALL background image work (searches + thumbnail +
# download fetches) across every card row. Without this, a batch with several
# visual cards spawned one raw thread per card *plus* one per thumbnail — a
# burst of simultaneous requests that triggered rate-limiting (so it "worked 1
# of 3 tries") and flooded the UI thread with callbacks (the "froze" feeling).
# Capping concurrency smooths the load and makes results reliable.
_POOL: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ankisstant-img")
    return _POOL


def submit(fn, *args, **kwargs):
    """Run an image task on the shared bounded pool (throttled). Returns a
    Future. Use this instead of raw threads so concurrent card rows don't all
    hit the network at once."""
    return _pool().submit(fn, *args, **kwargs)
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
_SKIP = {"icon", "logo", "flag", "symbol", "button", "commons-logo",
         "edit-icon", "ooui", "wikimedia-button", "poweredby", "placeholder"}

OPENI_ENDPOINT = "https://openi.nlm.nih.gov/api/search"
OPENI_BASE = "https://openi.nlm.nih.gov"
BING_ENDPOINT = "https://api.bing.microsoft.com/v7.0/images/search"


# ── config ───────────────────────────────────────────────────────────────────

def _images_cfg() -> dict:
    """The card_creator.images config sub-dict, or {} (fail-open)."""
    try:
        from ..core.config import tool_config
        return tool_config("card_creator").get("images", {}) or {}
    except Exception:
        return {}


def _source_enabled(key: str, cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else _images_cfg()
    srcs = cfg.get("sources") or {}
    return bool(srcs.get(key, key in ("wikipedia", "openi")))


# ── low-level fetch ──────────────────────────────────────────────────────────

def _headers(url: str = "") -> dict:
    """Request headers. Adds a Referer for Wikimedia hosts, which otherwise
    reject hot-linked image fetches."""
    h = {"User-Agent": _UA, "Accept": "*/*",
         "Accept-Language": "en-US,en;q=0.9"}
    host = urllib.parse.urlparse(url).netloc.lower()
    if "wikimedia.org" in host or "wikipedia.org" in host:
        h["Referer"] = "https://en.wikipedia.org/"
    return h


def _get(url: str, timeout: int = 12, retries: int = 1) -> bytes:
    """Fetch a URL with the shared headers and a single retry on transient
    failure (one flaky request shouldn't lose a whole search)."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_headers(url))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.4)
    raise last if last else RuntimeError("fetch failed")


def _fix_url(url: str, base: str = "") -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base + url
    return url


def _is_junk(url: str) -> bool:
    low = url.lower()
    if any(s in low for s in _SKIP):
        return True
    return not any(low.split("?")[0].endswith(e) for e in _IMG_EXTS)


# ── search backends ──────────────────────────────────────────────────────────

def _find_wikipedia_title(query: str) -> str:
    params = {"action": "query", "list": "search", "srsearch": query,
              "srnamespace": "0", "srlimit": "1", "format": "json"}
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url))
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else query


def search_wikipedia(query: str, limit: int = 4) -> list[dict]:
    """Images from the best-matching Wikipedia article. Never raises."""
    results: list[dict] = []
    try:
        try:
            title = _find_wikipedia_title(query)
        except Exception:
            title = query
        title_enc = urllib.parse.quote(title.replace(" ", "_"))
        try:
            data = json.loads(_get(
                f"https://en.wikipedia.org/api/rest_v1/page/media-list/{title_enc}"))
            for item in data.get("items", []):
                if item.get("type") != "image":
                    continue
                t = item.get("title", "")
                if any(s in t.lower() for s in _SKIP):
                    continue
                srcset = item.get("srcset", [])
                if not srcset:
                    continue
                full = _fix_url(srcset[-1].get("src", ""))
                thumb = _fix_url(srcset[0].get("src", full))
                if not full or _is_junk(full):
                    continue
                results.append({"title": t.lstrip("File:")[:70], "url": full,
                                "thumb_url": thumb, "source": "Wikipedia"})
                if len(results) >= limit:
                    break
        except Exception:
            pass
        if not results:
            try:
                data = json.loads(_get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{title_enc}"))
                thumb = data.get("thumbnail", {})
                orig = data.get("originalimage", {})
                src = orig.get("source") or thumb.get("source")
                if src:
                    results.append({"title": title[:70], "url": src,
                                    "thumb_url": thumb.get("source", src),
                                    "source": "Wikipedia"})
            except Exception:
                pass
    except Exception as e:
        log.error(f"wikipedia image search failed: {e}")
    return results[:limit]


def search_openi(query: str, limit: int = 4) -> list[dict]:
    """NLM Open-i biomedical images (clinical photos, X-rays, histology). No key.
    Never raises."""
    results: list[dict] = []
    try:
        params = {"query": query, "it": "x,p,g,xg", "m": "1",
                  "n": str(min(limit * 2, 20))}
        url = OPENI_ENDPOINT + "?" + urllib.parse.urlencode(params)
        data = json.loads(_get(url, timeout=15))
        if data.get("Query-Error"):
            return []
        for item in data.get("list", []):
            img_path = item.get("imgLarge") or item.get("imgSmall") or ""
            thumb_path = item.get("imgThumb") or item.get("imgSmall") or img_path
            if not img_path:
                continue
            img_url = (OPENI_BASE + img_path) if img_path.startswith("/") else img_path
            thumb_url = (OPENI_BASE + thumb_path) if thumb_path.startswith("/") else thumb_path
            title = (item.get("title") or item.get("journal_title") or query)[:70]
            results.append({"title": title, "url": img_url,
                            "thumb_url": thumb_url, "source": "NLM Open-i"})
            if len(results) >= limit:
                break
    except Exception as e:
        log.error(f"openi image search failed: {e}")
    return results


def _scrape_img_urls(html: str, base: str) -> list[str]:
    """Pull plausible content-image URLs out of a results page. Best-effort."""
    urls: list[str] = []
    for m in re.finditer(r'<img[^>]+(?:data-src|data-original|src)=["\']([^"\']+)["\']',
                         html, flags=re.IGNORECASE):
        u = _fix_url(m.group(1).strip(), base)
        if u and not _is_junk(u) and u not in urls:
            urls.append(u)
    return urls


def search_dermnet(query: str, limit: int = 4) -> list[dict]:
    """DermNet dermatology images via its site search. HTML-scraped, fragile,
    never raises."""
    results: list[dict] = []
    try:
        url = "https://dermnetnz.org/search?q=" + urllib.parse.quote(query)
        html = _get(url, timeout=12).decode("utf-8", errors="ignore")
        for u in _scrape_img_urls(html, "https://dermnetnz.org"):
            if "dermnetnz.org" not in u and "/assets/" not in u and "imgix" not in u:
                continue
            results.append({"title": f"{query} (DermNet)"[:70], "url": u,
                            "thumb_url": u, "source": "DermNet"})
            if len(results) >= limit:
                break
    except Exception as e:
        log.error(f"dermnet image search failed: {e}")
    return results


def search_radiopaedia(query: str, limit: int = 4) -> list[dict]:
    """Radiopaedia case/article images via its site search. HTML-scraped,
    fragile, never raises."""
    results: list[dict] = []
    try:
        url = "https://radiopaedia.org/search?q=" + urllib.parse.quote(query) + "&scope=all"
        html = _get(url, timeout=12).decode("utf-8", errors="ignore")
        for u in _scrape_img_urls(html, "https://radiopaedia.org"):
            if "prod-images" not in u and "radiopaedia" not in u:
                continue
            results.append({"title": f"{query} (Radiopaedia)"[:70], "url": u,
                            "thumb_url": u, "source": "Radiopaedia"})
            if len(results) >= limit:
                break
    except Exception as e:
        log.error(f"radiopaedia image search failed: {e}")
    return results


def search_bing(query: str, limit: int = 4, api_key: str = "") -> list[dict]:
    """Bing Image Search via the official Azure API. Needs a key (no scraping).
    Returns [] with no key. Never raises."""
    if not api_key:
        return []
    results: list[dict] = []
    try:
        params = {"q": query, "count": str(min(limit * 2, 35)),
                  "safeSearch": "Strict", "imageType": "Photo"}
        req = urllib.request.Request(
            BING_ENDPOINT + "?" + urllib.parse.urlencode(params),
            headers={"Ocp-Apim-Subscription-Key": api_key, "User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        for item in data.get("value", []):
            u = item.get("contentUrl") or ""
            if not u or _is_junk(u):
                continue
            results.append({"title": (item.get("name") or query)[:70], "url": u,
                            "thumb_url": item.get("thumbnailUrl") or u,
                            "source": "Bing"})
            if len(results) >= limit:
                break
    except Exception as e:
        log.error(f"bing image search failed: {e}")
    return results


def search_images(query: str, cfg: dict | None = None,
                  limit_per_source: int = 4) -> list[dict]:
    """Aggregate every enabled source, de-duplicated by URL. Which sources run
    is driven by config (Settings → AI Create → Images); Bing additionally
    needs an API key."""
    cfg = cfg if cfg is not None else _images_cfg()
    bing_key = str(cfg.get("bing_api_key") or "").strip()
    jobs = []
    if _source_enabled("wikipedia", cfg):
        jobs.append(lambda q, n: search_wikipedia(q, n))
    if _source_enabled("openi", cfg):
        jobs.append(lambda q, n: search_openi(q, n))
    if _source_enabled("dermnet", cfg):
        jobs.append(lambda q, n: search_dermnet(q, n))
    if _source_enabled("radiopaedia", cfg):
        jobs.append(lambda q, n: search_radiopaedia(q, n))
    if _source_enabled("bing", cfg) and bing_key:
        jobs.append(lambda q, n: search_bing(q, n, api_key=bing_key))
    if not jobs:   # all disabled → fall back to the reliable core
        jobs = [lambda q, n: search_wikipedia(q, n),
                lambda q, n: search_openi(q, n)]

    out: list[dict] = []
    seen: set[str] = set()
    for fn in jobs:
        try:
            found = fn(query, limit_per_source)
        except Exception:
            found = []
        for r in found:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            out.append(r)
    return out


def download_to_temp(url: str) -> str | None:
    """Download an image URL to a temp file; return the path (or None). The
    AI-Create pipeline imports it into Anki media at card-creation time."""
    data = b""
    last: Exception | None = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=_headers(url))
            with urllib.request.urlopen(req, timeout=20) as r:
                # Cap the read so a pathological full-res original can't pull
                # hundreds of MB; the create pipeline downscales on import anyway.
                data = r.read(40 * 1024 * 1024)
            break
        except Exception as e:
            last = e
            if attempt == 0:
                time.sleep(0.4)
    try:
        if not data or len(data) < 256:
            if last:
                log.error(f"image download failed ({url}): {last}")
            return None
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        if ext not in _IMG_EXTS:
            ext = ".jpg"
        fd, path = tempfile.mkstemp(prefix="ankisstant_img_", suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path
    except Exception as e:
        log.error(f"image download failed ({url}): {e}")
        return None


# ── picker dialog ────────────────────────────────────────────────────────────

class ImageSearchDialog(QDialog):
    """Search the reliable medical image sources and pick one or more results.
    On accept, downloads the chosen full-res images to temp files; read them via
    `selected_paths()`."""

    def __init__(self, initial_query: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find image online")
        self.setMinimumSize(480, 460)
        self._results: list[dict] = []
        self._paths: list[str] = []
        self._closed = False
        self._build(initial_query)
        theme_dialog(self)

    def _build(self, initial_query: str):
        root = QVBoxLayout(self)
        hint = QLabel(
            "Searches Wikipedia and NLM Open-i (biomedical). Pick one or more "
            "images — they'll be added to the Extra field of this batch's cards."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(hint)

        row = QHBoxLayout()
        self._query = QLineEdit(initial_query)
        self._query.setPlaceholderText("e.g. malignant melanoma, chest x-ray pneumothorax")
        self._query.returnPressed.connect(self._search)
        row.addWidget(self._query, 1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._search)
        row.addWidget(self._search_btn)
        root.addLayout(row)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._list.setIconSize(QSize(96, 96))
        self._list.itemDoubleClicked.connect(lambda _i: self._accept())
        root.addWidget(self._list, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(self._status)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        if initial_query.strip():
            self._search()

    def _search(self):
        q = self._query.text().strip()
        if not q:
            return
        self._list.clear()
        self._results = []
        self._status.setText("Searching…")
        self._search_btn.setEnabled(False)

        def _run():
            res = search_images(q)
            mw.taskman.run_on_main(lambda: self._on_results(res))

        threading.Thread(target=_run, daemon=True).start()

    def _on_results(self, res: list[dict]):
        if self._closed:
            return
        self._search_btn.setEnabled(True)
        self._results = res
        if not res:
            self._status.setText("No images found. Try different wording.")
            return
        self._status.setText(f"{len(res)} image(s) — loading previews…")
        for r in res:
            it = QListWidgetItem(f"  {r['title']}  ·  {r['source']}")
            self._list.addItem(it)
        self._load_thumbs(res)

    def _load_thumbs(self, res: list[dict]):
        def _run():
            for i, r in enumerate(res):
                if self._closed:
                    return
                data = None
                try:
                    data = _get(r.get("thumb_url") or r["url"], timeout=12)
                except Exception:
                    data = None
                if data:
                    mw.taskman.run_on_main(lambda i=i, data=data: self._set_thumb(i, data))
            mw.taskman.run_on_main(lambda: self._status.setText(
                f"{len(res)} image(s). Select one or more, then OK."))

        threading.Thread(target=_run, daemon=True).start()

    def _set_thumb(self, i: int, data: bytes):
        if self._closed or i >= self._list.count():
            return
        try:
            pix = QPixmap()
            if pix.loadFromData(data):
                self._list.item(i).setIcon(QIcon(pix))
        except Exception:
            pass

    def _accept(self):
        rows = sorted({ix.row() for ix in self._list.selectedIndexes()})
        if not rows:
            self._status.setText("Select at least one image (or Cancel).")
            return
        self._status.setText("Downloading selected image(s)…")
        self._search_btn.setEnabled(False)
        chosen = [self._results[r] for r in rows if r < len(self._results)]

        def _run():
            paths = [p for p in (download_to_temp(c["url"]) for c in chosen) if p]
            mw.taskman.run_on_main(lambda: self._finish(paths))

        threading.Thread(target=_run, daemon=True).start()

    def _finish(self, paths: list[str]):
        if self._closed:
            return
        self._paths = paths
        if not paths:
            self._status.setText("Couldn't download the selected image(s).")
            self._search_btn.setEnabled(True)
            return
        self.accept()

    def selected_paths(self) -> list[str]:
        return list(self._paths)

    def reject(self):
        self._closed = True
        super().reject()

    def accept(self):
        self._closed = True
        super().accept()


def pick_images(parent, initial_query: str = "") -> list[str]:
    """Open the picker; return temp file paths of the chosen images (or [])."""
    try:
        dlg = ImageSearchDialog(initial_query, parent=parent)
        if dlg.exec():
            return dlg.selected_paths()
    except Exception as e:
        log.error(f"image picker failed: {e}")
    return []


# ── in-Anki visual browse (embedded webview) ──────────────────────────────────

def _browse_url(query: str, cfg: dict | None = None) -> str:
    """Build the image-search URL for the configured browse engine."""
    cfg = cfg if cfg is not None else _images_cfg()
    engine = str(cfg.get("browse_engine") or "duckduckgo").lower()
    q = urllib.parse.quote(query or "")
    if engine == "custom":
        tpl = str(cfg.get("browse_custom_url") or "").strip()
        if tpl:
            return tpl.replace("{q}", q) if "{q}" in tpl else (tpl + q)
        engine = "duckduckgo"
    return {
        "bing":   f"https://www.bing.com/images/search?q={q}",
        "google": f"https://www.google.com/search?tbm=isch&q={q}",
        "duckduckgo": f"https://duckduckgo.com/?q={q}&iax=images&ia=images",
    }.get(engine, f"https://duckduckgo.com/?q={q}&iax=images&ia=images")


def browse_for_images(parent, initial_query: str = "") -> list[str]:
    """Open an embedded web browser pointed at an image search. The user
    right-clicks any image and chooses "Attach", which downloads it to a temp
    file. Returns the list of attached temp paths (or []). Fail-open: if the
    webview is unavailable, returns [] after a log line."""
    try:
        from aqt.qt import QWebEngineView, QMenu  # noqa: F401
    except Exception as e:
        log.error(f"embedded webview unavailable: {e}")
        try:
            from aqt.utils import showWarning
            showWarning("The in-Anki image browser isn't available on this build. "
                        "Use 🔍 Find image online instead.")
        except Exception:
            pass
        return []
    try:
        dlg = _BrowseDialog(initial_query, parent=parent)
        dlg.exec()
        return dlg.attached_paths()
    except Exception as e:
        log.error(f"image browse failed: {e}")
        return []


def _make_browse_dialog_classes():
    """Build the webview + dialog classes lazily so the QWebEngine import only
    happens when browsing is actually used."""
    from aqt.qt import (
        QWebEngineView, QMenu, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
        QLineEdit, QPushButton,
    )

    class _ImgWebView(QWebEngineView):
        def __init__(self, attach_cb, parent=None):
            super().__init__(parent)
            self._attach_cb = attach_cb

        def contextMenuEvent(self, event):
            media_url = ""
            try:
                req = self.lastContextMenuRequest()
                if req is not None:
                    mu = req.mediaUrl()
                    if mu is not None and not mu.isEmpty():
                        media_url = mu.toString()
            except Exception:
                media_url = ""
            menu = QMenu(self)
            if media_url:
                act = menu.addAction("📎 Attach this image to the card(s)")
                act.triggered.connect(lambda: self._attach_cb(media_url))
            else:
                a = menu.addAction("Right-click directly on an image to attach it")
                a.setEnabled(False)
            menu.addSeparator()
            back = menu.addAction("◀ Back")
            back.triggered.connect(self.back)
            menu.exec(event.globalPos())

    class _BrowseDialog(QDialog):
        def __init__(self, initial_query="", parent=None):
            super().__init__(parent)
            self.setWindowTitle("Browse for an image")
            self.setMinimumSize(820, 640)
            self._paths: list[str] = []
            root = QVBoxLayout(self)

            hint = QLabel(
                "Browse normally, then <b>right-click any image → Attach</b>. "
                "Each attached image is added to the card(s). Click Done when finished."
            )
            hint.setTextFormat(Qt.TextFormat.RichText)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray; font-size: 11px;")
            root.addWidget(hint)

            row = QHBoxLayout()
            self._query = QLineEdit(initial_query)
            self._query.setPlaceholderText("search images…")
            self._query.returnPressed.connect(self._go)
            row.addWidget(self._query, 1)
            go = QPushButton("Search")
            go.clicked.connect(self._go)
            row.addWidget(go)
            root.addLayout(row)

            self._view = _ImgWebView(self._attach, self)
            root.addWidget(self._view, 1)

            bottom = QHBoxLayout()
            self._status = QLabel("0 attached")
            self._status.setStyleSheet("color: gray; font-size: 11px;")
            bottom.addWidget(self._status)
            bottom.addStretch(1)
            done = QPushButton("Done")
            done.clicked.connect(self.accept)
            bottom.addWidget(done)
            root.addLayout(bottom)

            self._view.load(_qurl(_browse_url(initial_query)))

        def _go(self):
            q = self._query.text().strip()
            if q:
                self._view.load(_qurl(_browse_url(q)))

        def _attach(self, url: str):
            def _run():
                path = download_to_temp(url)
                mw.taskman.run_on_main(lambda: self._on_attached(path))
            threading.Thread(target=_run, daemon=True).start()
            self._status.setText("downloading…")

        def _on_attached(self, path):
            if path:
                self._paths.append(path)
            self._status.setText(
                f"{len(self._paths)} attached" if path
                else f"{len(self._paths)} attached (last download failed)")

        def attached_paths(self) -> list[str]:
            return list(self._paths)

    return _BrowseDialog


def _qurl(url: str):
    from aqt.qt import QUrl
    return QUrl(url)


# `_BrowseDialog` is created lazily (it subclasses QWebEngineView, which we
# don't want to touch at import time). browse_for_images resolves it on demand.
def _BrowseDialog(initial_query="", parent=None):  # noqa: N802
    cls = _make_browse_dialog_classes()
    return cls(initial_query, parent=parent)
