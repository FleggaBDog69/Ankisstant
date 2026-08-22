"""Bringing knowledge gaps in from outside Anki.

Two sources, one shape. Both produce the record `kg_store.add_many` takes, and
both carry a **group** — because that is the unit the rest of the add-on works
on, and a batch of gaps that arrives ungrouped has to be grouped by hand before
any of the group-scoped tools can touch it.

  • Things 3 — read straight out of its SQLite store. Every task carrying a tag
    (default "KG") becomes a gap; its Things PROJECT becomes the group; its
    notes and checklist items become the gap's notes.
  • Markdown — a pasted project export. Headings become groups, list items
    become gaps, indented text under an item becomes its notes.

Nothing here writes to Things. It is a read of a file the user's own app owns,
taken from a snapshot copy so a running Things can never be blocked by us.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile

from ...core import log

# Things 3 keeps its store in a group container. The team ID is stable; the
# per-install suffix on the data directory is not, hence the glob.
_THINGS_GLOB = os.path.expanduser(
    "~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/"
    "ThingsData-*/Things Database.thingsdatabase/main.sqlite")

DEFAULT_TAG = "KG"

# Things task status codes. 0 = open; 3 = completed; 2 = cancelled.
_OPEN, _CANCELLED, _DONE = 0, 2, 3


def db_path() -> str:
    """The live Things database, or "" when Things isn't installed here."""
    hits = sorted(glob.glob(_THINGS_GLOB))
    return hits[0] if hits else ""


def available() -> bool:
    return bool(db_path())


def _snapshot(src: str) -> str:
    """Copy the store (and its WAL) to a temp file and read THAT.

    Things may be running and holding a write lock; more importantly a
    half-copied database without its -wal is missing every recent change, which
    is exactly the gap you just captured on your phone. Copy all three parts."""
    tmp = tempfile.mkdtemp(prefix="ankisstant-things-")
    dst = os.path.join(tmp, "main.sqlite")
    for suffix in ("", "-wal", "-shm"):
        try:
            shutil.copyfile(src + suffix, dst + suffix)
        except FileNotFoundError:
            pass          # -wal/-shm are absent on a cleanly closed database
    return dst


def list_tags() -> list[str]:
    """Every tag name in Things, so the picker isn't a blank text box."""
    path = db_path()
    if not path:
        return []
    snap = ""
    try:
        snap = _snapshot(path)
        con = sqlite3.connect(snap)
        try:
            return [str(r[0]) for r in con.execute(
                "select title from TMTag where title is not null "
                "order by title collate nocase")]
        finally:
            con.close()
    except Exception as e:
        log.warn(f"things: couldn't read tags: {e}")
        return []
    finally:
        _cleanup(snap)


def _cleanup(snap: str) -> None:
    if not snap:
        return
    try:
        shutil.rmtree(os.path.dirname(snap), ignore_errors=True)
    except Exception:
        pass


def fetch(tag: str = DEFAULT_TAG, *, include_done: bool = False) -> list[dict]:
    """Every Things task carrying `tag`, as import candidates.

    Returns dicts with: title, notes, group, things_uuid, done, project, area.
    Never raises — a missing or unreadable Things store is an empty list and a
    log line, because this sits behind a button the user may press on a machine
    that has never had Things on it."""
    path = db_path()
    if not path:
        return []
    snap = ""
    try:
        snap = _snapshot(path)
        con = sqlite3.connect(snap)
        try:
            return _fetch(con, tag, include_done)
        finally:
            con.close()
    except Exception as e:
        log.error(f"things: import read failed: {e}")
        return []
    finally:
        _cleanup(snap)


def _fetch(con, tag: str, include_done: bool) -> list[dict]:
    cur = con.cursor()
    cur.execute("select uuid from TMTag where title = ? collate nocase", (tag,))
    row = cur.fetchone()
    if row is None:
        log.warn(f"things: no tag named {tag!r}")
        return []
    tag_uuid = row[0]

    statuses = (_OPEN,) if not include_done else (_OPEN, _DONE, _CANCELLED)
    marks = ",".join("?" * len(statuses))
    cur.execute(f"""
        select t.uuid, t.title, t.notes, t.status,
               p.title, a.title, h.title, t."index"
        from TMTaskTag tt
        join TMTask t on t.uuid = tt.tasks
        left join TMTask p on p.uuid = t.project
        left join TMTask h on h.uuid = t.heading
        left join TMArea a on a.uuid = t.area
        where tt.tags = ? and t.trashed = 0 and t.status in ({marks})
        order by p.title collate nocase, t."index"
    """, (tag_uuid, *statuses))
    rows = cur.fetchall()

    # Checklist items in ONE query, not one per task: they are part of the
    # gap's notes ("what specifically don't I know") and dropping them loses
    # the detail the task was written to hold.
    checks: dict[str, list[str]] = {}
    uuids = [r[0] for r in rows]
    for chunk in (uuids[i:i + 400] for i in range(0, len(uuids), 400)):
        if not chunk:
            continue
        marks = ",".join("?" * len(chunk))
        cur.execute(f'''select task, title from TMChecklistItem
                        where task in ({marks}) and title is not null
                        order by task, "index"''', chunk)
        for task, title in cur.fetchall():
            checks.setdefault(task, []).append(str(title))

    out: list[dict] = []
    for uuid, title, notes, status, proj, area, heading, _idx in rows:
        title = clean_title(title or "")
        if not title:
            continue
        body = clean_notes(notes or "")
        extra = [clean_title(c) for c in checks.get(uuid, [])]
        extra = [c for c in extra if c]
        if extra:
            body = (body + "\n" if body else "") + "\n".join(f"- {c}"
                                                             for c in extra)
        out.append({
            "title": title,
            "notes": body,
            # Project first, then heading, then area. A project is what he
            # actually organises study by ("SRS 12 Jaundice"); area is the
            # coarse bucket and would collapse a dozen projects into "Study".
            "group": str(proj or heading or area or "").strip(),
            "things_uuid": str(uuid),
            "done": status in (_DONE, _CANCELLED),
            "project": str(proj or ""),
            "area": str(area or ""),
        })
    return out


# ── writing back ────────────────────────────────────────────────────────────
#
# Reads go through SQLite; writes go through AppleScript, and the asymmetry is
# deliberate. Things syncs its store through Things Cloud and keeps its own
# change log; writing a completion straight into the database would tick the
# to-do off on this Mac and then be overwritten, or worse, desync the store.
# AppleScript hands the change to Things itself, which is the only thing that
# knows how to record it.

_APP = "Things3"

# One osascript for the whole set, not one per to-do: each launch is ~100ms of
# Apple-events setup, and a fifty-gap sync would spend five seconds doing
# nothing else. Each id is tried independently so one stale id (the to-do was
# deleted in Things after the import) can't take the rest of the batch down.
_COMPLETE_SCRIPT = """
on run argv
    set doneCount to 0
    set missingCount to 0
    tell application "%s"
        repeat with theId in argv
            set tid to theId as text
            try
                set td to to do id tid
                if status of td is open then
                    set status of td to completed
                    set doneCount to doneCount + 1
                end if
            on error
                set missingCount to missingCount + 1
            end try
        end repeat
    end tell
    return (doneCount as text) & "|" & (missingCount as text)
end run
""" % _APP


def app_installed() -> bool:
    return os.path.isdir("/Applications/%s.app" % _APP)


def complete_in_things(uuids: list, *, timeout: int = 60) -> tuple:
    """Tick these to-dos off in Things. Returns (completed, missing, error).

    Never raises. A to-do that is ALREADY completed is counted as neither — it
    needed nothing — so re-running a sync reports zero rather than re-reporting
    work it did last time. An id Things no longer knows about is counted as
    `missing`, which is information (that to-do was deleted over there), not a
    failure."""
    # `if u` before str(): str(None) is the four-character string "None", which
    # would sail through a truthiness check on the converted value and be sent
    # to Things as a to-do id.
    ids = [str(u).strip() for u in (uuids or []) if u and str(u).strip()]
    if not ids:
        return 0, 0, ""
    if not app_installed():
        return 0, 0, "%s isn't installed on this Mac." % _APP
    try:
        proc = subprocess.run(
            ["osascript", "-", *ids], input=_COMPLETE_SCRIPT,
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 0, 0, ("%s didn't respond within %ss. It may be showing a "
                      "dialog, or syncing." % (_APP, timeout))
    except Exception as e:
        return 0, 0, "Couldn't talk to %s: %s" % (_APP, e)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # The first run asks macOS for permission to control Things. Denied, it
        # comes back as -1743, which as raw AppleScript noise tells the user
        # nothing about the checkbox they actually need to tick.
        if "-1743" in err or "Not authorized" in err or "not allowed" in err:
            return 0, 0, (
                "macOS blocked Anki from controlling Things. Allow it in "
                "System Settings → Privacy & Security → Automation → Anki, "
                "then try again.")
        return 0, 0, err or ("%s refused the change." % _APP)
    out = (proc.stdout or "").strip()
    try:
        done_s, missing_s = out.split("|", 1)
        return int(done_s), int(missing_s), ""
    except ValueError:
        log.warn("things: unexpected write-back reply %r" % out)
        return 0, 0, ""


def open_uuids(uuids) -> set:
    """Of these Things ids, the ones still OPEN in Things right now.

    Read before writing so a sync reports what it actually changed, and so a
    to-do already ticked off by hand isn't counted as work this did."""
    wanted = {str(u).strip() for u in (uuids or []) if u and str(u).strip()}
    if not wanted:
        return set()
    path = db_path()
    if not path:
        return set()
    snap = ""
    try:
        snap = _snapshot(path)
        con = sqlite3.connect(snap)
        try:
            out = set()
            ids = list(wanted)
            for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
                marks = ",".join("?" * len(chunk))
                out.update(str(r[0]) for r in con.execute(
                    "select uuid from TMTask where uuid in (%s) "
                    "and status = %d and trashed = 0" % (marks, _OPEN), chunk))
            return out
        finally:
            con.close()
    except Exception as e:
        log.warn("things: couldn't check open state: %s" % e)
        return set()
    finally:
        _cleanup(snap)


def linked_uuid(kg: dict) -> str:
    return str(((kg or {}).get("fields") or {}).get("things_uuid") or "").strip()


def pending_writeback(kgs: list) -> list:
    """KGs finished here that are still open over there.

    BOTH halves are checked: done in Ankisstant AND open in Things. Skipping
    the second read would make every sync re-send every gap ever completed."""
    candidates = [(k, linked_uuid(k)) for k in (kgs or [])]
    candidates = [(k, u) for k, u in candidates
                  if u and str(k.get("status") or "") == "done"]
    if not candidates:
        return []
    still_open = open_uuids([u for _k, u in candidates])
    return [k for k, u in candidates if u in still_open]


# ── markdown ────────────────────────────────────────────────────────────────

# A leading list marker, with or without a checkbox: "- ", "* ", "+ ", "1. ",
# "- [ ] ", "* [x] ". The checkbox is captured so a ticked item can be treated
# as already done rather than imported as an open gap.
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[([ xX~-])\]\s*)?(.*)$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*)$")
# A bare checkbox with no bullet, which is what several exporters emit.
_BARE_BOX = re.compile(r"^\s*\[([ xX~-])\]\s*(.*)$")
# Inline markdown emphasis/code/link syntax. Stripped from titles only: a title
# is a label, and "**Mirizzi** syndrome" as a KG name is noise the tag builder
# then has to clean out again.
_EMPH = re.compile(r"(\*\*|__|\*|_|`)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def clean_title(text: str) -> str:
    """One line, as a gap title: no bullet, no checkbox, no markdown syntax."""
    s = str(text or "").replace(" ", " ").strip()
    m = _HEADING.match(s)
    if m:
        s = m.group(2)
    m = _ITEM.match(s) or _BARE_BOX.match(s)
    if m:
        s = m.group(2) if m.re is _ITEM else m.group(2)
    s = _LINK.sub(r"\1", s)
    s = _EMPH.sub("", s)
    # A trailing Things-style tag chip, e.g. "Mirizzi syndrome @KG".
    s = re.sub(r"\s+@\w[\w-]*$", "", s)
    return " ".join(s.split())


def clean_notes(text: str) -> str:
    """Notes, with the checkbox syntax removed and everything else kept.

    Deliberately NOT clean_title per line: the bullets and blank-line grouping
    in a notes body are how it reads ("Resp / ILD, CA, Bronchiectasis"), and
    flattening them would destroy the content to tidy the syntax. Only the
    markers that mean "this is a to-do" go, because these are notes now."""
    out = []
    for raw in str(text or "").replace(" ", " ").splitlines():
        line = raw.rstrip()
        m = _ITEM.match(line)
        if m and m.group(1) is not None:
            indent = line[:len(line) - len(line.lstrip())]
            line = f"{indent}- {m.group(2)}".rstrip()
        else:
            m = _BARE_BOX.match(line)
            if m:
                indent = line[:len(line) - len(line.lstrip())]
                line = f"{indent}{m.group(2)}".rstrip()
        out.append(line)
    # Collapse the run of blank lines a stripped checklist tends to leave, and
    # trim blank lines off the ends — but NOT indentation. A line's indent is
    # content here ("Resp" then its indented list), and .strip() would silently
    # reflow the first line of every notes body.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return text.strip("\n")


def dedent(text: str) -> str:
    """Remove the indentation that only ever meant 'this is under that item'.

    Markdown notes arrive carrying the four spaces that put them beneath their
    bullet. That indent is structure in the source and noise in a notes field,
    so the COMMON prefix goes and any deeper nesting keeps its relative shape."""
    lines = [ln for ln in str(text or "").split("\n") if ln.strip()]
    if not lines:
        return str(text or "")
    common = min(len(ln) - len(ln.lstrip()) for ln in lines)
    if not common:
        return text
    return "\n".join(ln[common:] if ln.strip() else ln
                     for ln in str(text).split("\n"))


def is_done_marker(mark) -> bool:
    """True for [x] / [X] / [~] / [-] — every 'this one's handled' spelling."""
    return mark is not None and str(mark).strip() != ""


def parse_markdown(text: str) -> list[dict]:
    """A pasted project export → gap records.

    The shape it reads is what Things (and most outliners) produce:

        # SRS 12 Jaundice          <- a group
        - [ ] Mirizzi syndrome     <- a gap
            GB stone -> biliary obstruction   <- its notes
        - [x] Patterns of hepatic disease     <- a gap, already ticked

    Indentation under an item is that item's notes, NOT a sub-gap: a nested
    to-do list under "Clubbing causes" is the detail of the one gap, and
    importing each line as its own gap is how a specific gap becomes twelve
    fragments that mean nothing apart.
    """
    records: list[dict] = []
    group = ""
    current: dict | None = None
    base_indent = 0

    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            if current is not None:
                current["_notes"].append("")
            continue

        head = _HEADING.match(raw)
        if head:
            group = clean_title(head.group(2))
            current = None
            continue

        indent = len(raw) - len(raw.lstrip())
        item = _ITEM.match(raw) or _BARE_BOX.match(raw)

        # An item indented past the one above it is that item's detail, not a
        # gap of its own.
        if item and current is not None and indent > base_indent:
            current["_notes"].append(raw)
            continue

        if item:
            title = clean_title(item.group(2))
            if not title:
                continue
            current = {"title": title, "group": group,
                       "done": is_done_marker(item.group(1)), "_notes": []}
            base_indent = indent
            records.append(current)
            continue

        if current is not None and indent > base_indent:
            current["_notes"].append(raw)
            continue

        # A bare unindented line with no marker at all: still a gap. Plenty of
        # lists are just one thing per line, and refusing them would make the
        # importer pickier than the paste box it replaces.
        title = clean_title(raw)
        if title:
            current = {"title": title, "group": group, "done": False,
                       "_notes": []}
            base_indent = indent
            records.append(current)

    for rec in records:
        rec["notes"] = clean_notes(dedent("\n".join(rec.pop("_notes"))))
        rec["things_uuid"] = ""
    return records


def to_store_records(items: list[dict], *, type_key: str,
                     use_groups: bool = True,
                     status: str = "open") -> list[dict]:
    """Import candidates → kg_store records.

    `group` is a top-level key on the store record, not a field: it is an
    orthogonal axis the list page filters and the tag builder reads."""
    out = []
    for it in items:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        fields: dict = {}
        notes = str(it.get("notes") or "").strip()
        if notes:
            fields["notes"] = notes
        tu = str(it.get("things_uuid") or "").strip()
        if tu:
            fields["things_uuid"] = tu
        rec = {
            "title": title,
            "source": "things" if tu else "manual",
            "type": type_key,
            "status": "done" if it.get("done") else status,
            "fields": fields,
        }
        if use_groups:
            rec["group"] = str(it.get("group") or "").strip()
        out.append(rec)
    return out


def dedupe_against_store(items: list[dict], existing: list[dict]) -> tuple:
    """Split candidates into (fresh, already imported).

    Matched on things_uuid first — the only identity that survives a rename —
    then on a normalised title within the same group, which is what catches a
    markdown paste of a project already imported from Things."""
    seen_uuid = {
        str((k.get("fields") or {}).get("things_uuid") or "")
        for k in existing
    }
    seen_uuid.discard("")

    def key(title: str, group: str) -> tuple:
        return (" ".join(str(title).split()).casefold(),
                " ".join(str(group or "").split()).casefold())

    seen_title = {key(k.get("title") or "", k.get("group") or "")
                  for k in existing}

    fresh, dupes = [], []
    for it in items:
        tu = str(it.get("things_uuid") or "").strip()
        if (tu and tu in seen_uuid) or key(it.get("title") or "",
                                           it.get("group") or "") in seen_title:
            dupes.append(it)
            continue
        if tu:
            seen_uuid.add(tu)
        seen_title.add(key(it.get("title") or "", it.get("group") or ""))
        fresh.append(it)
    return fresh, dupes
