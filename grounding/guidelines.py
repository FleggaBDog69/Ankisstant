"""
Curated library of WA and Australian clinical-guideline URLs, organised by
specialty. These are VERIFIED, real URLs — they get injected into the model's
prompt so it cites actual pages instead of hallucinating slugs that don't exist.

Where a guideline is publicly fetchable (no login required), we can optionally
retrieve the page content so cards are based on real, current text. Live fetch
is OFF by default because it does blocking network I/O; with a CLI provider the
model can fetch the allow-listed URLs itself via its own web tools.

Ported from Patrick's claude_cards (guidelines_library.py).
"""

import re
import urllib.request

_UA = "Ankisstant/1.8 (Anki addon)"


# ── Guideline registry ─────────────────────────────────────────────────────────
# Each entry:
#   name        Display name used in citations
#   url         Root / index URL (always cited; also used as fetch target if fetchable)
#   fetch_url   Override URL to actually fetch content from (optional)
#   fetchable   True if content is publicly accessible without login
#   specialties List of tag keywords that trigger this guideline

BUILTIN_GUIDELINES = [

    # ── WA-specific ────────────────────────────────────────────────────────────

    {
        "name": "Perth Children's Hospital – Emergency Department Clinical Guidelines",
        "url": "https://www.pch.health.wa.gov.au/For-health-professionals/Emergency-Department-Guidelines",
        "fetchable": True,
        "specialties": ["paediatric", "paediatrics", "emergency", "neonatal", "neonatology"],
    },
    {
        "name": "Perth Children's Hospital – Clinical Guidelines",
        "url": "https://www.pch.health.wa.gov.au/For-health-professionals/Clinical-guidelines",
        "fetchable": True,
        "specialties": ["paediatric", "paediatrics", "neonatal", "neonatology",
                        "cardiology", "respiratory", "gastroenterology", "haematology",
                        "endocrinology", "nephrology", "neurology", "oncology"],
    },
    {
        "name": "King Edward Memorial Hospital – Clinical Guidelines (KEMH)",
        "url": "https://www.kemh.health.wa.gov.au/For-health-professionals/Clinical-guidelines",
        "fetchable": True,
        "specialties": ["obstetric", "obstetrics", "gynaecology", "gynecology",
                        "maternal", "antenatal", "postnatal", "neonatal"],
    },
    {
        "name": "WA Health – Antimicrobial Stewardship Program",
        "url": "https://ww2.health.wa.gov.au/Articles/A_E/Antimicrobial-stewardship",
        "fetchable": True,
        "specialties": ["infectious", "antimicrobial", "antibiotic", "microbiology",
                        "infectious_diseases"],
    },
    {
        "name": "HealthyWA – Clinical Guidelines",
        "url": "https://www.healthywa.wa.gov.au/Articles/clinical-guidelines",
        "fetchable": True,
        "specialties": ["general_practice", "general", "emergency", "chronic_disease"],
    },
    {
        "name": "WA Health – Mental Health Commission Clinical Guidelines",
        "url": "https://www.mhc.wa.gov.au/mental-health-in-wa/clinical-resources/",
        "fetchable": True,
        "specialties": ["psychiatry", "mental_health", "substance", "addiction"],
    },
    {
        "name": "PathWest – WA Pathology Guidelines",
        "url": "https://pathwest.health.wa.gov.au/For-Health-Professionals/Pathology-Handbook",
        "fetchable": True,
        "specialties": ["haematology", "pathology", "biochemistry", "microbiology",
                        "endocrinology"],
    },
    {
        "name": "WA Cancer and Palliative Care Network – Clinical Guidelines",
        "url": "https://www.health.wa.gov.au/Articles/A_E/Cancer-and-Palliative-Care",
        "fetchable": True,
        "specialties": ["oncology", "palliative", "cancer", "haematology_oncology"],
    },
    {
        "name": "WA Health – Stroke Clinical Guidelines",
        "url": "https://ww2.health.wa.gov.au/Articles/S_T/Stroke",
        "fetchable": True,
        "specialties": ["neurology", "stroke", "emergency"],
    },
    {
        "name": "WA Pharmacist Immunisation Program – Vaccination Guidelines",
        "url": "https://www.health.wa.gov.au/Articles/U_Z/Vaccination",
        "fetchable": True,
        "specialties": ["immunology", "vaccination", "preventive", "epidemiology"],
    },

    # ── National Australian (login-gated — URL injected; model uses training knowledge) ──

    {
        "name": "eTG Complete – Therapeutic Guidelines",
        "url": "https://www.tg.org.au",
        "fetchable": False,
        "specialties": ["*"],   # applies to everything
    },
    {
        "name": "RACGP – Royal Australian College of General Practitioners Guidelines",
        "url": "https://www.racgp.org.au/clinical-resources/clinical-guidelines",
        "fetchable": False,
        "specialties": ["general_practice", "preventive", "chronic_disease",
                        "diabetes", "cardiovascular", "respiratory", "mental_health"],
    },
    {
        "name": "RANZCOG – Clinical Guidelines (Obstetrics & Gynaecology)",
        "url": "https://ranzcog.edu.au/training-resources/ranzcog-college-statements-guidelines",
        "fetchable": False,
        "specialties": ["obstetric", "obstetrics", "gynaecology", "maternal"],
    },
    {
        "name": "Cardiac Society of Australia and New Zealand (CSANZ) Guidelines",
        "url": "https://www.csanz.edu.au/resources/guidelines/",
        "fetchable": False,
        "specialties": ["cardiology", "arrhythmia", "heart_failure", "coronary"],
    },
    {
        "name": "Thoracic Society of Australia and New Zealand (TSANZ) Guidelines",
        "url": "https://www.thoracic.org.au/clinical-resources/guidelines",
        "fetchable": False,
        "specialties": ["respiratory", "lung", "copd", "asthma", "pneumonia"],
    },
    {
        "name": "Gastroenterological Society of Australia (GESA) Guidelines",
        "url": "https://www.gesa.org.au/resources/clinical-guidelines/",
        "fetchable": False,
        "specialties": ["gastroenterology", "liver", "inflammatory_bowel",
                        "hepatology", "pancreas"],
    },
    {
        "name": "Australian and New Zealand Society of Nephrology (ANZSN) Guidelines",
        "url": "https://www.anzsn.org/resources/guidelines/",
        "fetchable": False,
        "specialties": ["nephrology", "renal", "dialysis", "transplant"],
    },
    {
        "name": "Australasian Society for Infectious Diseases (ASID) Guidelines",
        "url": "https://www.asid.net.au/resources/guidelines",
        "fetchable": False,
        "specialties": ["infectious", "infectious_diseases", "hiv", "tropical"],
    },
    {
        "name": "Endocrine Society of Australia Guidelines",
        "url": "https://www.endocrinesociety.org.au/resources/clinical-guidelines.aspx",
        "fetchable": False,
        "specialties": ["endocrinology", "diabetes", "thyroid", "adrenal", "pituitary"],
    },
    {
        "name": "Haematology Society of Australia and New Zealand (HSANZ) Guidelines",
        "url": "https://www.hsanz.org.au/resources/guidelines",
        "fetchable": False,
        "specialties": ["haematology", "haematology_oncology", "blood", "coagulation"],
    },
    {
        "name": "Australasian College for Emergency Medicine (ACEM) Guidelines",
        "url": "https://acem.org.au/Content-Sources/Advancing-Emergency-Medicine/Better-Outcomes-for-Patients/Guidelines",
        "fetchable": False,
        "specialties": ["emergency", "toxicology", "resuscitation", "trauma"],
    },
    {
        "name": "Australian Rheumatology Association (ARA) Guidelines",
        "url": "https://rheumatology.org.au/resources/guidelines-and-recommendations",
        "fetchable": False,
        "specialties": ["rheumatology", "arthritis", "lupus", "vasculitis", "gout"],
    },
    {
        "name": "Australasian College of Dermatologists Guidelines",
        "url": "https://www.dermcoll.edu.au/atoz/",
        "fetchable": False,
        "specialties": ["dermatology", "skin", "melanoma", "psoriasis"],
    },

    # ── Free educational references (search-based; no stable per-topic URL) ─────

    {
        "name": "LITFL – Life in the Fast Lane",
        "url": "https://litfl.com",
        "fetchable": False,
        "specialties": ["emergency", "intensive_care", "cardiology", "toxicology",
                        "pharmacology", "ecg"],
    },
    {
        "name": "Radiopaedia – Radiology Reference",
        "url": "https://radiopaedia.org",
        "fetchable": False,
        "specialties": ["radiology", "imaging", "respiratory", "neurology",
                        "orthopaedics", "emergency"],
    },
    {
        "name": "DermNet NZ – Dermatology Reference",
        "url": "https://dermnetnz.org",
        "fetchable": False,
        "specialties": ["dermatology", "skin", "melanoma"],
    },
]


# ── United States ───────────────────────────────────────────────────────────
# Reputable US bodies. Mostly allow-list only (fetchable False) — society and
# agency guideline indexes aren't useful to scrape; the URL is still a valid
# citation. Open patient/reference sites are marked fetchable.

USA_GUIDELINES = [
    {
        "name": "UpToDate – Clinical Decision Support",
        "url": "https://www.uptodate.com",
        "fetchable": False,
        "specialties": ["*"],
    },
    {
        "name": "CDC – Clinical Guidance",
        "url": "https://www.cdc.gov",
        "fetchable": False,
        "specialties": ["infectious", "infectious_diseases", "antimicrobial",
                        "vaccination", "immunology", "epidemiology", "preventive",
                        "public_health", "travel"],
    },
    {
        "name": "USPSTF – Preventive Services Recommendations",
        "url": "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation-topics",
        "fetchable": True,
        "specialties": ["preventive", "screening", "general_practice", "general"],
    },
    {
        "name": "AHA/ACC – Cardiovascular Clinical Guidelines",
        "url": "https://www.acc.org/guidelines",
        "fetchable": False,
        "specialties": ["cardiology", "arrhythmia", "heart_failure", "coronary",
                        "cardiovascular", "hypertension"],
    },
    {
        "name": "IDSA – Infectious Diseases Practice Guidelines",
        "url": "https://www.idsociety.org/practice-guideline/practice-guidelines/",
        "fetchable": False,
        "specialties": ["infectious", "infectious_diseases", "antimicrobial",
                        "antibiotic", "hiv"],
    },
    {
        "name": "ADA – Standards of Care in Diabetes",
        "url": "https://professional.diabetes.org/standards-of-care",
        "fetchable": False,
        "specialties": ["endocrinology", "diabetes"],
    },
    {
        "name": "ACOG – Clinical Guidance (Obstetrics & Gynecology)",
        "url": "https://www.acog.org/clinical/clinical-guidance",
        "fetchable": False,
        "specialties": ["obstetric", "obstetrics", "gynaecology", "gynecology",
                        "maternal", "antenatal"],
    },
    {
        "name": "NCCN – Oncology Clinical Practice Guidelines",
        "url": "https://www.nccn.org/guidelines/category_1",
        "fetchable": False,
        "specialties": ["oncology", "cancer", "haematology_oncology"],
    },
    {
        "name": "AAP – Pediatric Clinical Practice Guidelines",
        "url": "https://www.aap.org/en/practice-management/aap-clinical-practice-guidelines/",
        "fetchable": False,
        "specialties": ["paediatric", "paediatrics", "pediatric", "pediatrics",
                        "neonatal", "neonatology"],
    },
    {
        "name": "ACR – Rheumatology Clinical Practice Guidelines",
        "url": "https://rheumatology.org/clinical-practice-guidelines",
        "fetchable": False,
        "specialties": ["rheumatology", "arthritis", "lupus", "vasculitis", "gout"],
    },
    {
        "name": "KDIGO – Kidney Disease Guidelines",
        "url": "https://kdigo.org/guidelines/",
        "fetchable": False,
        "specialties": ["nephrology", "renal", "dialysis", "transplant"],
    },
    {
        "name": "AGA – Gastroenterology Clinical Guidelines",
        "url": "https://gastro.org/guidelines/",
        "fetchable": False,
        "specialties": ["gastroenterology", "liver", "hepatology",
                        "inflammatory_bowel", "pancreas"],
    },
    {
        "name": "AAN – Neurology Clinical Practice Guidelines",
        "url": "https://www.aan.com/practice/guidelines",
        "fetchable": False,
        "specialties": ["neurology", "stroke", "epilepsy", "headache"],
    },
    {
        "name": "APA – Psychiatry Clinical Practice Guidelines",
        "url": "https://www.psychiatry.org/psychiatrists/practice/clinical-practice-guidelines",
        "fetchable": False,
        "specialties": ["psychiatry", "mental_health", "substance", "addiction"],
    },
    {
        "name": "GOLD – Global Initiative for Chronic Obstructive Lung Disease",
        "url": "https://goldcopd.org/",
        "fetchable": False,
        "specialties": ["respiratory", "copd", "lung"],
    },
    {
        "name": "GINA – Global Initiative for Asthma",
        "url": "https://ginasthma.org/",
        "fetchable": False,
        "specialties": ["asthma", "respiratory"],
    },
    {
        "name": "MedlinePlus (NIH) – Health Reference",
        "url": "https://medlineplus.gov",
        "fetchable": True,
        "specialties": ["general", "general_practice"],
    },
    {
        "name": "Radiopaedia – Radiology Reference",
        "url": "https://radiopaedia.org",
        "fetchable": False,
        "specialties": ["radiology", "imaging", "neurology", "respiratory",
                        "orthopaedics", "emergency"],
    },
    {
        "name": "LITFL – Life in the Fast Lane",
        "url": "https://litfl.com",
        "fetchable": False,
        "specialties": ["emergency", "intensive_care", "cardiology", "toxicology",
                        "pharmacology", "ecg"],
    },
    {
        "name": "DermNet – Dermatology Reference",
        "url": "https://dermnetnz.org",
        "fetchable": False,
        "specialties": ["dermatology", "skin", "melanoma", "psoriasis"],
    },
]


# ── International ───────────────────────────────────────────────────────────
# Region-neutral, globally respected sources for a broad audience.

INTL_GUIDELINES = [
    {
        "name": "UpToDate – Clinical Decision Support",
        "url": "https://www.uptodate.com",
        "fetchable": False,
        "specialties": ["*"],
    },
    {
        "name": "BMJ Best Practice",
        "url": "https://bestpractice.bmj.com",
        "fetchable": False,
        "specialties": ["*"],
    },
    {
        "name": "Cochrane Library – Systematic Reviews",
        "url": "https://www.cochranelibrary.com",
        "fetchable": False,
        "specialties": ["general", "general_practice", "preventive", "pharmacology",
                        "evidence"],
    },
    {
        "name": "WHO – Clinical Guidelines",
        "url": "https://www.who.int/publications/who-guidelines",
        "fetchable": False,
        "specialties": ["infectious", "infectious_diseases", "public_health",
                        "vaccination", "tropical", "epidemiology", "general"],
    },
    {
        "name": "NICE – Clinical Guidance (UK)",
        "url": "https://www.nice.org.uk/guidance",
        "fetchable": False,
        "specialties": ["general_practice", "cardiovascular", "cardiology",
                        "respiratory", "mental_health", "diabetes", "endocrinology",
                        "cancer", "oncology", "neurology", "preventive"],
    },
    {
        "name": "ESC – European Society of Cardiology Guidelines",
        "url": "https://www.escardio.org/Guidelines",
        "fetchable": False,
        "specialties": ["cardiology", "arrhythmia", "heart_failure", "coronary",
                        "cardiovascular"],
    },
    {
        "name": "ERS – European Respiratory Society Guidelines",
        "url": "https://www.ersnet.org/science-and-research/clinical-practice-guidelines/",
        "fetchable": False,
        "specialties": ["respiratory", "lung", "copd", "asthma", "pneumonia"],
    },
    {
        "name": "EASL – Liver Disease Clinical Practice Guidelines",
        "url": "https://easl.eu/publication-category/clinical-practice-guidelines/",
        "fetchable": False,
        "specialties": ["liver", "hepatology", "gastroenterology"],
    },
    {
        "name": "ESMO – European Society for Medical Oncology Guidelines",
        "url": "https://www.esmo.org/guidelines",
        "fetchable": False,
        "specialties": ["oncology", "cancer", "haematology_oncology"],
    },
    {
        "name": "KDIGO – Kidney Disease Guidelines",
        "url": "https://kdigo.org/guidelines/",
        "fetchable": False,
        "specialties": ["nephrology", "renal", "dialysis", "transplant"],
    },
    {
        "name": "GOLD – Global Initiative for Chronic Obstructive Lung Disease",
        "url": "https://goldcopd.org/",
        "fetchable": False,
        "specialties": ["respiratory", "copd", "lung"],
    },
    {
        "name": "GINA – Global Initiative for Asthma",
        "url": "https://ginasthma.org/",
        "fetchable": False,
        "specialties": ["asthma", "respiratory"],
    },
    {
        "name": "WHO – Model List of Essential Medicines",
        "url": "https://www.who.int/groups/expert-committee-on-selection-and-use-of-essential-medicines",
        "fetchable": False,
        "specialties": ["pharmacology", "therapeutics"],
    },
    {
        "name": "PubMed – Biomedical Literature",
        "url": "https://pubmed.ncbi.nlm.nih.gov",
        "fetchable": False,
        "specialties": ["*"],
    },
    {
        "name": "MedlinePlus (NIH) – Health Reference",
        "url": "https://medlineplus.gov",
        "fetchable": True,
        "specialties": ["general", "general_practice"],
    },
    {
        "name": "Radiopaedia – Radiology Reference",
        "url": "https://radiopaedia.org",
        "fetchable": False,
        "specialties": ["radiology", "imaging", "neurology", "respiratory",
                        "orthopaedics", "emergency"],
    },
    {
        "name": "LITFL – Life in the Fast Lane",
        "url": "https://litfl.com",
        "fetchable": False,
        "specialties": ["emergency", "intensive_care", "cardiology", "toxicology",
                        "pharmacology", "ecg"],
    },
    {
        "name": "DermNet – Dermatology Reference",
        "url": "https://dermnetnz.org",
        "fetchable": False,
        "specialties": ["dermatology", "skin", "melanoma", "psoriasis"],
    },
]


# ── Region presets ──────────────────────────────────────────────────────────
# The shipped source sets a user can pick between. `region` in config selects
# one (key); an edited `sources` list overrides the preset ("custom" region).

AU_WA_GUIDELINES = BUILTIN_GUIDELINES   # readable alias for the default set

REGION_PRESETS = {
    "au_wa": {"label": "Australian & WA", "sources": AU_WA_GUIDELINES},
    "usa":   {"label": "United States",   "sources": USA_GUIDELINES},
    "intl":  {"label": "International",    "sources": INTL_GUIDELINES},
}

DEFAULT_REGION = "au_wa"


def preset_keys() -> list:
    """Region keys in display order."""
    return ["au_wa", "usa", "intl"]


def region_preset(key: str) -> dict:
    """The preset dict ({label, sources}) for a region key, falling back to the
    default region for unknown keys."""
    return REGION_PRESETS.get(key) or REGION_PRESETS[DEFAULT_REGION]


def preset_label(key: str) -> str:
    return region_preset(key)["label"]


# Back-compat alias: older code referenced GUIDELINES directly. The live
# registry is now resolved per-call from config (see _load_sources).
GUIDELINES = BUILTIN_GUIDELINES


# ── Editable, region-transferable registry ─────────────────────────────────────

def _grounding_cfg() -> dict:
    """The card_creator.grounding config sub-dict, or {} (fail-open)."""
    try:
        from ..core.config import tool_config
        return tool_config("card_creator").get("grounding", {}) or {}
    except Exception:
        return {}


def _active_region() -> str:
    """Selected region key from config (default DEFAULT_REGION)."""
    return str(_grounding_cfg().get("region") or DEFAULT_REGION).strip().lower()


def _load_sources() -> list:
    """The active guideline registry, resolved in priority order:
      1. the user's edited `sources` list (a "custom" region) if non-empty;
      2. the selected region preset's sources;
      3. the default AU/WA set.
    Each entry must be a dict with at least name/url; malformed entries dropped."""
    src = _grounding_cfg().get("sources")
    if isinstance(src, list) and src:
        clean = [g for g in src
                 if isinstance(g, dict) and g.get("url") and g.get("name")]
        if clean:
            return clean
    return region_preset(_active_region())["sources"]


def _region_label() -> str:
    """Citation-header label: the user's free-text label when they've supplied a
    custom source list, otherwise the selected preset's label."""
    cfg = _grounding_cfg()
    src = cfg.get("sources")
    if isinstance(src, list) and src:
        return str(cfg.get("region_label") or preset_label(_active_region())).strip()
    return preset_label(_active_region())


# ── Specialty matching ─────────────────────────────────────────────────────────

def _tag_keywords(tags: list) -> set:
    """Extract lowercase keyword fragments from tag strings (handles Malleus
    hierarchies like Malleus_CM::#Subjects::Paediatrics::09_Neonatology)."""
    words = set()
    for tag in tags or []:
        for part in re.split(r"[::#_]", str(tag)):
            p = part.lower().strip()
            if len(p) > 3:
                words.add(p)
    return words


def get_relevant_guidelines(topic: str, tags: list | None = None) -> list:
    """Return guideline dicts relevant to the topic and tags. Always includes
    eTG (specialty '*'); adds WA and specialty-specific sources that match."""
    keywords = _tag_keywords(tags or [])
    topic_words = set(re.split(r"\W+", (topic or "").lower()))
    all_words = keywords | topic_words

    matches = []
    seen_urls = set()
    for g in _load_sources():
        if g["url"] in seen_urls:
            continue
        specs = g.get("specialties") or []
        if "*" in specs:
            matches.insert(0, g)   # eTG goes first
            seen_urls.add(g["url"])
            continue
        if any(s in all_words for s in specs):
            matches.append(g)
            seen_urls.add(g["url"])
    return matches


# ── Content fetching (optional, blocking — off by default) ─────────────────────

def _fetch(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,*/*",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _strip(html: str) -> str:
    for tag in ("script", "style", "nav", "footer", "header", "aside", "noscript"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", html,
                      flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def fetch_guideline_content(guideline: dict, char_limit: int = 4000) -> str:
    """Fetch content from a publicly accessible guideline page. Returns '' on any
    failure (fail-open)."""
    if not guideline.get("fetchable"):
        return ""
    url = guideline.get("fetch_url") or guideline["url"]
    try:
        html = _fetch(url)
        if len(html) < 2000:
            return ""
        return _strip(html)[:char_limit]
    except Exception:
        return ""


# ── Prompt injection ───────────────────────────────────────────────────────────

def build_guidelines_block(topic: str, tags: list | None = None,
                           fetch_content: bool = False) -> str:
    """Build the source-material block to inject into the generation prompt.

    Each guideline is labelled [LIVE — fetched now] or [TRAINING DATA]; the model
    is told to carry that label through into the card's source citation so the
    student knows which citations are verified vs. from memory. Returns '' when no
    guideline matches (fail-open — generation proceeds ungrounded)."""
    guidelines = get_relevant_guidelines(topic, tags)
    if not guidelines:
        return ""

    import datetime
    today = datetime.date.today().strftime("%Y-%m-%d")

    region = _region_label().upper()
    header = f"{region} CLINICAL GUIDELINES (citation allow-list)"
    lines = [
        header,
        "=" * len(header),
        "Each source below is labelled [LIVE] or [TRAINING DATA].",
        "",
        "CITATION RULES:",
        "• [LIVE] sources: base cards directly on the fetched content below.",
        f"  Citation format: \"Name – URL. Accessed {today}. [Live source]\"",
        "• [TRAINING DATA] sources: use your knowledge of that guideline.",
        "  Citation format: \"Name – URL. [Training data — verify currency]\"",
        "• NEVER invent a URL. Cite ONLY the exact URLs listed here.",
        "• If you have web access, you MAY fetch the listed URLs to verify.",
        "",
    ]

    for g in guidelines:
        fetched_content = ""
        if fetch_content and g.get("fetchable"):
            fetched_content = fetch_guideline_content(g)
        label = "[LIVE — fetched now]" if fetched_content else "[TRAINING DATA]"
        lines.append(f"• {g['name']}  {label}")
        lines.append(f"  URL: {g['url']}")
        if fetched_content:
            lines.append(f"  Content (fetched {today}):")
            for line in fetched_content[:3000].splitlines():
                if line.strip():
                    lines.append(f"    {line}")
        lines.append("")

    return "\n".join(lines)
