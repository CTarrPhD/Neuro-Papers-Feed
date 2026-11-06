#!/usr/bin/env python3
"""
build_digest.py - fetch all feeds, keep only new papers, and render a scrollable page.

Every run:
  1. Fetches every feed in UNFILTERED (kept as-is) and filter_feeds.FEEDS (keyword-filtered).
     If a journal's RSS feed fails and it has a Crossref ISSN, it falls back to Crossref
     (a free public index of journal articles) so blocked publisher feeds don't leave gaps.
  2. Adds papers it has not seen before to state/seen.json, stamped with today's date (Pacific time).
  3. Drops papers first seen more than --keep-days ago.
  4. Writes docs/index.html: newest day first, with a search box.

"New" means new to this digest, not the publisher's timestamp, so weekends, time zones,
and late-indexed papers never leave gaps.
"""
import argparse
import datetime as dt
import html
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import feedparser

import filter_feeds as ff

TZ = ZoneInfo("America/Los_Angeles")
UA = "Mozilla/5.0 (compatible; neuro-digest/1.0; personal feed reader)"
feedparser.USER_AGENT = UA

# Feeds shown without keyword filtering (field-specific, manageable volume).
# (name, RSS url or None, Crossref ISSN or None)
# The ISSN is the fallback when RSS fails, or the only source when the url is None.
UNFILTERED = [
    ("Nature Neuroscience", "https://www.nature.com/neuro.rss", "1546-1726"),
    ("bioRxiv Neuroscience", "https://connect.biorxiv.org/biorxiv_xml.php?subject=neuroscience", None),
    ("medRxiv Neurology", "https://connect.medrxiv.org/medrxiv_xml.php?subject=neurology", None),
    ("arXiv q-bio.NC", "https://rss.arxiv.org/rss/q-bio.NC", None),
    ("Nature Medicine", "https://www.nature.com/nm.rss", "1078-8956"),
    ("Nature Genetics", "https://www.nature.com/ng.rss", "1061-4036"),
    ("Nature Aging", "https://www.nature.com/nataging.rss", "2662-8465"),
    ("Nature Reviews Neuroscience", "https://www.nature.com/nrn.rss", "1471-003X"),
    ("Nature Reviews Neurology", "https://www.nature.com/nrneurol.rss", "1759-4766"),
    ("Nature Methods", "https://www.nature.com/nmeth.rss", "1548-7091"),
    ("Nature Biotechnology", "https://www.nature.com/nbt.rss", "1087-0156"),
    ("Molecular Psychiatry", "https://www.nature.com/mp.rss", "1359-4184"),
    # AI journal, all of it: shown in the "AI & models" tab
    ("Nature Machine Intelligence", "https://www.nature.com/natmachintell.rss", "2522-5839"),
    ("Neuron", "https://www.cell.com/neuron/current.rss", "0896-6273"),
    ("Cell Stem Cell", "https://www.cell.com/cell-stem-cell/current.rss", "1934-5909"),
    ("Stem Cell Reports", "https://www.cell.com/stem-cell-reports/current.rss", "2213-6711"),
    ("Alzheimer's & Dementia", "https://alz-journals.onlinelibrary.wiley.com/feed/15525279/most-recent", "1552-5260"),
    ("Lancet Neurology", "https://www.thelancet.com/rssfeed/laneur_current.xml", "1474-4422"),
    # No usable RSS feed: pulled from Crossref
    ("Acta Neuropathologica", None, "1432-0533"),
    ("Molecular Neurodegeneration", None, "1750-1326"),
    ("Brain", None, "1460-2156"),
    ("JAMA Neurology", None, "2168-6149"),
    ("Annals of Neurology", None, "1531-8249"),
    ("Neurobiology of Aging", None, "0197-4580"),
    ("Alzheimer's Research & Therapy", None, "1758-9193"),
    # Translational journals, kept to neurodegeneration (see FILTER_PROFILE)
    ("Nature Reviews Drug Discovery", "https://www.nature.com/nrd.rss", "1474-1776"),
    ("Nature Biomedical Engineering", "https://www.nature.com/natbiomedeng.rss", "2157-846X"),
    ("Molecular Therapy", None, "1525-0016"),
    ("Journal of Clinical Investigation", None, "0021-9738"),
    ("Cell Reports Medicine", None, "2666-3791"),
]

# Skip correction notices
SKIP_TITLE = re.compile(
    r"^\s*((publisher |author )?(correction|erratum)\b|(issue information|cover image|front matter|table of contents|masthead)\s*$)",
    re.IGNORECASE)


# Keyword filters for broad journals listed in UNFILTERED (profiles live in filter_feeds.py).
#   neurodegen    - neurodegeneration only (broad clinical / aging journals)
#   neurogenetics - neurodegeneration, OR genetics of the nervous system
#   neuroscience - any neuroscience, so big general findings and new techniques still show
# Field journals (Nature Neuroscience, Neuron, Nature Reviews Neurology...) stay unfiltered.
FILTER_PROFILE = {
    "Nature Medicine": "neurodegen",
    "Nature Genetics": "neurogenetics",
    "Nature Aging": "neurodegen",
    "Nature Reviews Drug Discovery": "neurodegen",
    "Nature Biomedical Engineering": "neurodegen",
    "Molecular Therapy": "neurodegen",
    "Journal of Clinical Investigation": "neurodegen",
    "Cell Reports Medicine": "neurodegen",
    "Nature Methods": "neuroscience",
    "Nature Biotechnology": "neuroscience",
    "Cell Stem Cell": "neuroscience",
    "Stem Cell Reports": "neuroscience",
}

# Crossref backup (by ISSN) for keyword-filtered journals in filter_feeds.FEEDS whose
# publisher RSS often fails.
FILTERED_ISSN = {
    "Nature": "0028-0836",
    "Nature Communications": "2041-1723",
    "Science": "0036-8075",
    "Science Translational Medicine": "1946-6234",
    "Cell": "0092-8674",
    "Cell Reports": "2211-1247",
    "Scientific Reports (Neuroscience)": "2045-2322",
    "American Journal of Human Genetics": "0002-9297",
    "Cell Genomics": "2666-979X",
    "Genome Research": "1088-9051",
    "Genome Biology": "1474-760X",
}


def all_feeds():
    return ([(n, u, FILTER_PROFILE.get(n, "all"), i) for n, u, i in UNFILTERED]
            + [(n, u, p, FILTERED_ISSN.get(n)) for n, u, p in ff.FEEDS])


# ---------------------------------------------------------------- fetching

def candidate_urls(url):
    """Primary URL, plus Nature's alternate feed host, which sometimes works when the first doesn't."""
    urls = [url]
    m = re.match(r"https://www\.nature\.com/([a-z]+)\.rss$", url)
    if m:
        urls.append(f"https://feeds.nature.com/{m.group(1)}/rss/current")
    return urls


def fetch_rss(url):
    """Returns (entries, error_text). Tries each candidate URL twice."""
    last = "no entries"
    for candidate in candidate_urls(url):
        for attempt in range(2):
            try:
                parsed = feedparser.parse(candidate)
            except Exception as exc:
                last = f"error: {exc.__class__.__name__}"
            else:
                if parsed.entries:
                    return parsed.entries, None
                code = parsed.get("status")
                exc = parsed.get("bozo_exception")
                last = "no entries" + (f" (HTTP {code})" if code else "") \
                       + (f" ({exc.__class__.__name__})" if exc else "")
            time.sleep(2)
    return [], last


def _crossref_get(url):
    """GET a Crossref URL; returns (items, note)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            items = data.get("message", {}).get("items", [])
            return items, f"{len(items)} items"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = exc.__class__.__name__
        time.sleep(3)
    return [], last


def fetch_crossref(issn, days):
    """Recent journal articles for an ISSN from Crossref, shaped like feed entries.
    Tries three query styles, because Crossref sometimes returns nothing for one of them
    (late indexing, or the journal registered under its other ISSN)."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    wide = (dt.date.today() - dt.timedelta(days=max(days, 7))).isoformat()
    common = {"rows": "100", "select": "DOI,title,abstract,created,URL"}
    attempts = [
        ("journal", f"https://api.crossref.org/journals/{issn}/works?" + urllib.parse.urlencode(
            {**common, "filter": f"from-created-date:{since},type:journal-article", "sort": "created", "order": "desc"})),
        ("issn filter", "https://api.crossref.org/works?" + urllib.parse.urlencode(
            {**common, "filter": f"issn:{issn},from-created-date:{since}", "sort": "created", "order": "desc"})),
        ("index date", "https://api.crossref.org/works?" + urllib.parse.urlencode(
            {**common, "filter": f"issn:{issn},from-index-date:{wide}", "sort": "indexed", "order": "desc"})),
    ]
    notes = []
    for label, url in attempts:
        items, note = _crossref_get(url)
        notes.append(f"{label}: {note}")
        entries = []
        for it in items:
            title = " ".join(it.get("title") or [])
            ts = (it.get("created") or {}).get("timestamp")
            if not title or not ts:
                continue
            entries.append({
                "title": title,
                "link": it.get("URL") or f"https://doi.org/{it['DOI']}",
                "summary": it.get("abstract", ""),
                "published_parsed": time.gmtime(ts / 1000),
            })
        if entries:
            return entries, None
    return [], "crossref returned nothing (" + "; ".join(notes) + ")"


def dedupe_key(link):
    """Same paper reached through RSS or Crossref should count once."""
    low = link.lower()
    m = re.search(r"doi\.org/(10\.\S+)$", low)
    if m:
        return m.group(1)
    m = re.search(r"nature\.com/articles/([a-z0-9\-]+)$", low)
    if m:
        return "10.1038/" + m.group(1)
    return low


# ---------------------------------------------------------------- state

def load_state(path):
    p = pathlib.Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"items": {}, "feed_status": {}, "updated": None}


def ingest(feeds, state, now, ingest_days):
    cutoff = now - dt.timedelta(days=ingest_days)
    today = now.astimezone(TZ).date().isoformat()
    seen_keys = {dedupe_key(link) for link in state["items"]}
    compiled = {}
    status = {}
    for feed in feeds:
        name, url, profile, *rest = feed
        issn = rest[0] if rest else None
        if profile != "all" and profile not in compiled:
            compiled[profile] = ff.compile_profile(profile)

        entries, error, via = [], None, ""
        if url:
            entries, error = fetch_rss(url)
        if not entries and issn:
            entries, cr_error = fetch_crossref(issn, max(ingest_days, 3))
            if entries:
                via = " (via Crossref)"
            else:
                error = f"{error}; {cr_error}" if error else cr_error
        if not entries:
            status[name] = error or "no entries"
            continue

        total = new = 0
        for e in entries:
            total += 1
            link = e.get("link") or e.get("id")
            if not link:
                continue
            key = dedupe_key(link)
            if key in seen_keys:
                continue
            title = ff.clean(e.get("title", ""))
            if not title or SKIP_TITLE.match(title):
                continue
            when = ff.entry_time(e) or now
            if when < cutoff:
                continue
            if profile != "all" and not ff.matches(e, *compiled[profile]):
                continue
            state["items"][link] = {
                "title": title,
                "source": name,
                "published": when.isoformat(),
                "summary": ff.clean(e.get("summary", ""))[:700],
                "first_seen": today,
            }
            seen_keys.add(key)
            new += 1
        status[name] = f"{new} new of {total}{via}"
    state["feed_status"] = status
    state["updated"] = now.astimezone(TZ).strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")


def refilter(state):
    """Re-apply every feed's keyword filter to papers already on the page, so a changed filter takes effect at once."""
    profiles = {name: profile for name, _url, profile, _issn in all_feeds() if profile != "all"}
    compiled = {}
    for link, it in list(state["items"].items()):
        if SKIP_TITLE.match(it["title"]):
            del state["items"][link]
            continue
        profile = profiles.get(it["source"])
        if not profile:
            continue
        if profile not in compiled:
            compiled[profile] = ff.compile_profile(profile)
        if not ff.matches(it, *compiled[profile]):
            del state["items"][link]


def prune(state, now, keep_days):
    floor = (now.astimezone(TZ).date() - dt.timedelta(days=keep_days)).isoformat()
    state["items"] = {k: v for k, v in state["items"].items() if v["first_seen"] >= floor}


def day_label(day_iso, today_iso):
    d = dt.date.fromisoformat(day_iso)
    pretty = d.strftime("%A, %B %d").replace(" 0", " ")
    return f"New today - {pretty}" if day_iso == today_iso else pretty


# ---------------------------------------------------------------- article types

PREPRINT_SOURCES = ("biorxiv", "medrxiv", "arxiv")
RE_COMMENTARY = re.compile(
    r"^\s*(news\s*(&|and)\s*views|editorial|comment(ary)?|correspondence|viewpoint|preview|insights?|"
    r"research highlights?|news( feature)?|career|q\s*&\s*a|obituary|outlook|spotlight|retraction note)\b\s*[:\-–—|]?",
    re.IGNORECASE)
RE_REVIEW = re.compile(
    r"(\bsystematic review\b|\bmeta-?analys[ie]s\b|\bnarrative review\b|\bscoping review\b|"
    r"\b(a|the) review\b|:\s*a review\b|\bmini-?review\b|^\s*(review|perspective|primer)\b\s*[:\-–—|]|"
    r"\bthis review\b|\bin this review\b|\bwe review\b|\bhere we review\b|\bthis perspective\b)",
    re.IGNORECASE)
RE_CLINICAL = re.compile(
    r"(randomi[sz]ed|\bclinical trial\b|\bphase (i{1,3}|[123])\b|\bplacebo\b|\bcohort\b|"
    r"\bcase (report|series)\b|\bpatients? with\b|\bsecondary analysis\b|\bregistry\b|\bcase-control\b)",
    re.IGNORECASE)
# Imaging and fluid-biomarker studies go to "Clinical & imaging" (title only, to stay precise)
RE_IMAGING = re.compile(
    r"(\bneuroimaging\b|\bmagnetic resonance\b|\btomography\b|\bdiffusion tensor\b|\bimaging biomarker|"
    r"\bplasma (?:p-?tau|biomarker|neurofilament)|\bblood-based biomarker|\bcsf biomarker|\bfluid biomarker|"
    r"\bp-?tau ?(?:181|217|231)\b|\bamyloid pet\b|\btau pet\b|\bbrain age\b)",
    re.IGNORECASE)
RE_IMAGING_ACR = re.compile(r"(?<![A-Za-z0-9])(?:PET|MRI|fMRI|dMRI|DTI|SPECT)(?![A-Za-z0-9])")
AI_SOURCES = ("arxiv cs.", "arxiv stat.", "nature machine intelligence")

# Preprints that also appear in the Research tab (they stay listed under Preprints too).
# A preprint is promoted when ANY of these is true (imaging studies never are):
#   1. its title names neurodegeneration (disease, gene, pathology, mechanism)
#   2. its title is about genetics (GWAS, eQTL, CRISPR screen, splicing, chromatin...) and
#      about the nervous system
#   3. its abstract names neurodegeneration and it uses genetics, your methods, or is therapeutic
RE_MY_METHODS = re.compile(
    r"induced pluripotent|\bh?iPSCs?\b|organoid|crispr|base edit|prime edit|single-cell|single-nucle|"
    r"splicing|isoform", re.IGNORECASE)
_RULES = {}


def _rules():
    if not _RULES:
        _RULES["nd"] = ff.compile_profile("neurodegen")
        _RULES["gen"] = re.compile(ff.GENETICS_RE)
        _RULES["ctx"] = re.compile(ff.NEURO_CONTEXT_RE)
        _RULES["tx"] = re.compile(ff.THERAPY_RE)
    return _RULES


def is_translational(title, summary):
    """Neurodegeneration therapeutics: new drugs, gene therapy, ASOs, delivery, trials.
    Needs a neurodegeneration term and a therapy term, at least one of them in the title."""
    r = _rules()
    title, summary = ff.norm_dashes(title), ff.norm_dashes(summary)
    if RE_IMAGING.search(title) or RE_IMAGING_ACR.search(title):
        return False
    nd_s, nd_a, _pairs = r["nd"]
    nd = lambda s: bool(nd_s.search(s) or nd_a.search(s))
    tx = lambda s: bool(r["tx"].search(s))
    head = summary[:400]
    return (nd(title) and tx(title + " " + head)) or (tx(title) and nd(title + " " + head))


def hyper_relevant(title, summary):
    r = _rules()
    title, summary = ff.norm_dashes(title), ff.norm_dashes(summary)
    if RE_IMAGING.search(title) or RE_IMAGING_ACR.search(title):
        return False
    if ff.matches({"title": title}, *r["nd"]):
        return True
    if r["gen"].search(title) and r["ctx"].search(title):
        return True
    both = title + " " + summary
    nd_s, nd_a, _pairs = r["nd"]
    return bool((nd_s.search(both) or nd_a.search(both))
                and (r["gen"].search(both) or RE_MY_METHODS.search(both) or r["tx"].search(both)))


def classify(title, source, summary, link=""):
    """Best-effort article type. Feeds rarely label this, so it works from the source and wording."""
    s = source.lower()
    title, summary = ff.norm_dashes(title), ff.norm_dashes(summary)
    if s.startswith(AI_SOURCES):
        return "ai"
    if s.startswith(PREPRINT_SOURCES):
        return "preprints"
    # Nature's news, features and opinion pieces all use DOIs starting 10.1038/d41586-
    if "/d41586-" in link.lower():
        return "commentary"
    if s.startswith("nature reviews"):
        return "reviews"
    if RE_COMMENTARY.match(title):
        return "commentary"
    if RE_REVIEW.search(title) or RE_REVIEW.search(summary[:300]):
        return "reviews"
    if RE_CLINICAL.search(title) or RE_CLINICAL.search(summary[:300]):
        return "clinical"
    if RE_IMAGING.search(title) or RE_IMAGING_ACR.search(title):
        return "clinical"
    return "research"


# ---------------------------------------------------------------- journal priority

# Nature / Science / Cell families float to the top of each day. The big open-access
# megajournals are in those families by name but stay in the normal group.
NOT_PRIORITY = {"nature communications", "scientific reports", "scientific reports (neuroscience)",
                "cell reports", "stem cell reports"}


def is_priority(source):
    s = source.lower()
    return s not in NOT_PRIORITY and s.startswith(("nature", "science", "cell", "neuron"))


# ---------------------------------------------------------------- page

TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neuroscience Publications</title>
<style>
:root{color-scheme:dark;--w:880px;--bg:#000;--fg:#f5f5f7;--muted:#86868b;--card:#161617;--side:#0c0c0d;--line:#262628;--accent:#2997ff;--chip:#1d1d1f;--on:#10243a;--star:#ffb340}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.layout{max-width:820px;margin:0 auto;padding:20px 16px 60px 44px}
aside{position:fixed;top:0;left:0;bottom:0;width:300px;max-width:85vw;background:var(--bg);border-right:1px solid var(--line);padding:16px 14px 40px;overflow-y:auto;z-index:30;transform:translateX(-100%);transition:transform .2s ease;visibility:hidden}
body.dopen aside{transform:none;visibility:visible;box-shadow:4px 0 18px rgba(0,0,0,.12)}
aside h3{margin:0 0 8px;font-size:1rem;display:flex;justify-content:space-between;align-items:center}
#dtab{position:fixed;top:50%;left:0;transform:translateY(-50%);z-index:31;writing-mode:vertical-rl;border:1px solid var(--accent);border-left:none;background:var(--accent);color:var(--bg);border-radius:0 8px 8px 0;padding:14px 6px;font:inherit;font-size:.9rem;font-weight:600;letter-spacing:.04em;cursor:pointer;transition:left .2s ease}
body.dopen #dtab{left:min(300px,85vw)}
#dtab.drop{outline:2px dashed var(--accent);outline-offset:3px}
body.dragging aside .sitem{border-color:var(--line)}
@media (min-width:900px){body.dopen .layout{padding-left:352px;max-width:1172px}}
.sitem{display:flex;align-items:center;justify-content:space-between;gap:6px;border:1px solid transparent;border-radius:8px;padding:5px 8px;cursor:pointer}
.sitem:hover{background:var(--chip)}
.sitem.on{background:var(--on);border-color:var(--accent)}
.sitem.drop{outline:2px dashed var(--accent)}
.sitem span.n{color:var(--muted);font-size:.85rem}
.sgroup{margin:14px 0 4px;color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}
.sact{display:flex;gap:6px;margin:2px 0 6px 8px;flex-wrap:wrap}
.sl{display:flex;align-items:center;min-width:0}
.tog{display:inline-block;width:1.1em;color:var(--muted);cursor:pointer;user-select:none;flex:none}
.sform{display:grid;gap:6px;margin-top:14px}
.sform input,.sform select,.sact select,.panel input[type=text],.panel select,.mv select{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit;font-size:.9rem;min-width:0}
.btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:4px 10px;font:inherit;font-size:.85rem;cursor:pointer}
h1{font-size:1.5rem;margin:8px 0 2px}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:14px}
#q{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;margin-bottom:10px}
nav{display:flex;gap:6px;overflow-x:auto;padding-bottom:6px;border-bottom:1px solid var(--line);margin-bottom:6px}
nav button{flex:none;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:99px;padding:6px 12px;font:inherit;font-size:.9rem;cursor:pointer}
nav button.on{background:var(--accent);border-color:var(--accent);color:var(--bg)}
nav button small{opacity:.7;margin-left:4px}
h2{font-size:1.05rem;margin:24px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h2 span{color:var(--muted);font-weight:400;font-size:.9rem}
.paper{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 10px}
.paper a.t{color:var(--fg);font-weight:600;text-decoration:none}
.paper a.t:hover{color:var(--accent);text-decoration:underline}
.src{display:inline-block;background:var(--chip);color:var(--muted);font-size:.78rem;padding:1px 8px;border-radius:99px;margin-top:6px}
.star{color:var(--star);margin-right:4px}
button.save{margin-left:8px;border:1px solid var(--line);background:none;color:var(--accent);border-radius:99px;padding:1px 10px;font:inherit;font-size:.78rem;cursor:pointer}
button.save.on{background:var(--on)}
.panel{margin-top:8px;padding:8px 10px;border:1px dashed var(--line);border-radius:8px;font-size:.9rem}
.panel label{display:block;padding:2px 0}
.panel .row{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.panel input[type=text]{flex:1;min-width:120px}
.mv{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:8px}
details{margin-top:6px;color:var(--muted);font-size:.92rem}
summary{cursor:pointer;color:var(--accent)}
.note{color:var(--muted);font-size:.85rem}
.banner{background:var(--on);border:1px solid var(--accent);border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:.9rem;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.status{margin-top:36px;color:var(--muted);font-size:.82rem}
.status summary{color:var(--muted)}
.empty{color:var(--muted);padding:20px 0}
details.day{margin:0;color:var(--fg);font-size:1rem}
details.day>summary{list-style:none;color:var(--fg);font-size:1.05rem;font-weight:600;margin:20px 0 10px;padding:6px 0;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:baseline}
details.day>summary::-webkit-details-marker{display:none}
details.day>summary::before{content:"\\25b8";color:var(--muted);font-size:.85rem;width:1em}
details.day[open]>summary::before{content:"\\25be"}
details.day>summary span{color:var(--muted);font-weight:400;font-size:.9rem}
/* ---- Theme: black "Pro" ---- */
body{-webkit-font-smoothing:antialiased}
aside{background:var(--side);border-right:1px solid var(--line)}
body.dopen aside{box-shadow:none}
.btn,.sform input,.sform select,.sact select,.panel input[type=text],.panel select,.mv select{background:var(--card);border-color:var(--line);color:var(--fg)}
.sitem:hover{background:var(--chip)}
.sitem.on{background:var(--on);border-color:transparent}
#q{background:var(--card);border-color:var(--line);color:var(--fg)}
#q::placeholder{color:var(--muted)}
.paper>button.save+button.save{background:none;border-color:transparent;color:var(--accent)}
@media (min-width:900px){body.dopen .layout{padding-left:352px;max-width:calc(var(--w) + 368px)}}
.layout{max-width:var(--w)}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",Inter,sans-serif;letter-spacing:-.011em}
.layout{padding-top:64px}
h1{font-size:clamp(36px,5vw,60px);line-height:1.04;font-weight:700;letter-spacing:-.04em;margin:0 0 12px;background:linear-gradient(90deg,#a18cff,#5eb4ff 50%,#7ef3c4);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{font-size:17px;margin-bottom:34px}
#q{border-radius:14px;padding:14px 18px;font-size:16px}
nav{border:none;gap:8px;margin:6px 0 0}
nav button{border:1px solid var(--line);background:none;color:#d2d2d7;padding:7px 16px;font-size:14px}
nav button.on{background:#f5f5f7;border-color:#f5f5f7;color:#000}
details.day>summary{font-size:26px;font-weight:700;letter-spacing:-.02em;border:none;margin:42px 0 16px}
.paper{background:linear-gradient(180deg,#1b1b1d,#131314);border:1px solid #232326;border-radius:20px;padding:22px 26px;margin:0 0 14px;transition:border-color .2s,box-shadow .2s}
.paper:hover{border-color:#3a3a3e;box-shadow:0 18px 50px rgba(41,151,255,.08)}
.paper a.t{font-size:20px;line-height:1.3;font-weight:600;letter-spacing:-.02em}
.src{background:none;padding:0;margin-top:10px;font-size:14px;color:var(--muted)}
.paper>button.save{border:none;background:var(--accent);color:#fff;font-size:13px;padding:5px 15px;margin-left:14px}
.paper details{font-size:15px;line-height:1.55;color:#a1a1a6}
#dtab{background:#f5f5f7;color:#000;border:none;border-radius:0 12px 12px 0}
@media (max-width:600px){
  .layout{padding:28px 14px 60px 40px}
  .sub{font-size:14px;margin-bottom:20px}
  details.day>summary{font-size:19px;margin:28px 0 12px}
  .paper{padding:16px 16px;border-radius:16px}
  .paper a.t{font-size:17px}
  .paper>button.save{margin-left:8px}
}
.pp{display:inline-block;margin-left:8px;padding:0 7px;border:1px solid #6e5bd6;border-radius:99px;color:#b4a8ff;font-size:.72rem;font-weight:600;letter-spacing:.03em;text-transform:uppercase;vertical-align:1px}
.pp.tx{border-color:#2f9e6a;color:#6fe0a8}
</style></head>
<body>
<aside id="side"></aside>
<button id="dtab" title="Open or close your folders. Drag a paper here to open them.">Folders</button>
<div class="layout">
<main>
<h1>Neuroscience Publications</h1>
<div class="sub" id="sub"></div>
<div id="banner"></div>
<input id="q" type="search" placeholder="Filter this view (tau, iPSC, microglia...)" autocomplete="off">
<nav id="tabs"></nav>
<div id="view"></div>
<details class="status"><summary>Feed status (check for broken feeds)</summary><ul id="status"></ul></details>
</main></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('data').textContent);
const TABS=[["research","Research"],["preprints","Preprints"],["ai","AI & models"],["reviews","Reviews"],["clinical","Clinical & imaging"],["commentary","News & commentary"]];
const KEY='morning-papers-v1';
let store={folders:[],items:{}};
try{const s=localStorage.getItem(KEY);if(s)store=JSON.parse(s);}catch(e){}
if(!Array.isArray(store.folders))store.folders=[];
if(!store.items||typeof store.items!=='object')store.items={};
let askedPersist=false;
function persist(clean){
  if(!clean)store.dirty=(store.dirty||0)+1;
  try{localStorage.setItem(KEY,JSON.stringify(store));}catch(e){}
  if(!askedPersist){askedPersist=true;try{if(navigator.storage&&navigator.storage.persist)navigator.storage.persist().catch(function(){});}catch(e){}}
}

function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
function safeUrl(u){return /^https?:\\/\\//i.test(u||'')?u:'#';}
function uid(){return 'f'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);}
function folderById(id){return store.folders.find(f=>f.id===id);}
function isRoot(f){return !f.parent||!folderById(f.parent);}
function kids(id){return store.folders.filter(f=>f.parent===id&&f.id!==id);}
function roots(kind){return store.folders.filter(f=>isRoot(f)&&(!kind||f.kind===kind));}
function descIds(id,seen){seen=seen||[id];kids(id).forEach(k=>{if(!seen.includes(k.id)){seen.push(k.id);descIds(k.id,seen);}});return seen;}
function pathName(f){const parts=[f.name];let p=f.parent?folderById(f.parent):null,n=0;while(p&&n<20){parts.unshift(p.name);p=p.parent?folderById(p.parent):null;n++;}return parts.join(' / ');}
function isCollapsed(f){return !!(store.collapsed&&store.collapsed[f.id])&&!(view.k==='folder'&&view.id!==f.id&&descIds(f.id).includes(view.id));}
function treeOrder(){const out=[];const walk=(f,d)=>{out.push({f:f,d:d});kids(f.id).forEach(k=>{if(!out.some(o=>o.f.id===k.id))walk(k,d+1);});};roots().forEach(r=>walk(r,0));return out;}
function kindSelect(){const s=document.createElement('select');[['project','Project'],['question','Question']].forEach(o=>{const op=el('option',null,o[1]);op.value=o[0];s.append(op);});return s;}

let view=parseHash();
function parseHash(){
  const h=location.hash.slice(1);
  if(h.startsWith('folder-'))return {k:'folder',id:h.slice(7)};
  if(TABS.some(t=>t[0]===h))return {k:'tab',id:h};
  return {k:'tab',id:'research'};
}
function go(v){view=v;location.hash=v.k==='tab'?v.id:'folder-'+v.id;render();}

document.getElementById('sub').textContent='Updated '+D.updated+' \\u00b7 '+D.papers.length+' papers in the last '+D.keep+' days \\u00b7 \\u2605 = Nature/Science/Cell family';
const st=document.getElementById('status');
D.status.forEach(s=>st.append(el('li',null,s[0]+': '+s[1])));

/* ---------- saving ---------- */
function ensure(p){return store.items[p.l]||(store.items[p.l]={l:p.l,t:p.t,s:p.s,m:p.m,pr:p.pr,saved:Date.now(),folders:[]});}
function toggle(p,fid,on){
  const it=ensure(p);
  const i=it.folders.indexOf(fid);
  if(on&&i<0)it.folders.push(fid);
  if(!on&&i>=0)it.folders.splice(i,1);
  if(!it.folders.length)delete store.items[p.l];
  persist();
}
function count(p){const it=store.items[p.l];return it?it.folders.length:0;}
function label(btn,p){const n=count(p);btn.textContent=n?'Saved in '+n:'Save';btn.classList.toggle('on',n>0);}

function fillPanel(panel,p,btn){
  panel.replaceChildren();
  const it=store.items[p.l];
  if(!store.folders.length)panel.append(el('div','note','No folders yet. Create one below.'));
  treeOrder().forEach(o=>{
    const f=o.f;
    const lab=el('label');lab.style.paddingLeft=(o.d*16)+'px';
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=!!(it&&it.folders.includes(f.id));
    cb.onchange=()=>{toggle(p,f.id,cb.checked);label(btn,p);renderSide();};
    lab.append(cb,' '+f.name+' ',el('span','note','('+f.kind+')'));
    panel.append(lab);
  });
  const row=el('div','row');
  const inp=document.createElement('input');inp.type='text';inp.placeholder='New folder name';
  const sel=kindSelect();
  const add=el('button','btn','Create + save here');
  add.onclick=()=>{
    const name=inp.value.trim();if(!name)return;
    const f={id:uid(),name:name,kind:sel.value,parent:''};
    store.folders.push(f);toggle(p,f.id,true);label(btn,p);fillPanel(panel,p,btn);renderSide();
  };
  row.append(inp,sel,add);panel.append(row);
}

function overviewPrompt(p){
  return ['Give me a concise structured overview of this paper. I am a neuroscience PhD student studying neurodegeneration (MAPT/tau, iPSC models, CRISPR, genomics).',
    '',
    'Title: '+p.t,
    'Source: '+p.s,
    'Link: '+(p.l||''),
    'Abstract: '+(p.m||'(no abstract available - work from the title and say so)'),
    '',
    'Use exactly these headings, 1-3 short sentences each:',
    '1. Background: what prior work this study builds on',
    '2. Research question',
    '3. Hypothesis',
    '4. Methods used to test it',
    '5. Results',
    '6. Interpretation',
    '7. Gaps and limitations of the study',
    '8. Link to the broader field',
    '',
    'Base items 2-6 only on the abstract. Items 1, 7 and 8 may use your general knowledge, but mark anything inferred rather than stated as "(inferred)". If the abstract does not say something, write "not stated" instead of guessing. If a web fetch of the link works, you may use the full text and say so.'
  ].join('\\n');
}
function copyText(text){
  if(navigator.clipboard&&window.isSecureContext){
    return navigator.clipboard.writeText(text).then(()=>true,()=>fallbackCopy(text));
  }
  return Promise.resolve(fallbackCopy(text));
}
function fallbackCopy(text){
  try{
    const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
    document.body.append(ta);ta.select();const ok=document.execCommand('copy');ta.remove();return ok;
  }catch(e){return false;}
}

let dragP=null,autoOpened=false;
function drawerOpen(){return document.body.classList.contains('dopen');}
function setDrawer(open,save){
  document.body.classList.toggle('dopen',open);
  document.getElementById('dtab').textContent=open?'Close folders':'Folders';
  if(save!==false){autoOpened=false;store.drawer=open;persist(true);}
}
function endDrag(){
  dragP=null;document.body.classList.remove('dragging');
  if(autoOpened){autoOpened=false;setDrawer(false,false);}
}
function card(p,fid){
  const a=el('article','paper');
  a.dataset.src=p.s;if(p.pr)a.dataset.pr='1';
  a.draggable=true;
  a.addEventListener('dragstart',e=>{
    dragP=p;try{e.dataTransfer.setData('text/plain',p.l);e.dataTransfer.effectAllowed='copy';}catch(x){}
    document.body.classList.add('dragging');
    if(!drawerOpen()){autoOpened=true;setDrawer(true,false);}
  });
  a.addEventListener('dragend',endDrag);
  const t=el('a','t',p.t);t.href=safeUrl(p.l);t.target='_blank';t.rel='noopener noreferrer';
  a.append(t,el('br'));
  const chip=el('span','src');
  if(p.pr){const s=el('span','star','\\u2605');s.title='Nature / Science / Cell family';chip.append(s);}
  chip.append(document.createTextNode(p.s));
  if(p.x&&view.k==='tab'&&view.id==='research'){const b=el('span','pp','Preprint');b.title='Highly relevant preprint, also listed under Preprints';chip.append(b);}
  if(p.tx){const b=el('span','pp tx','Therapeutic');b.title='Neurodegeneration therapeutics: new drugs, gene therapy, ASOs, delivery or trials';chip.append(b);}
  a.append(chip);
  const btn=el('button','save');label(btn,p);
  const panel=el('div','panel');panel.hidden=true;
  btn.onclick=()=>{panel.hidden=!panel.hidden;if(!panel.hidden)fillPanel(panel,p,btn);};
  a.append(btn);
  const cp=el('button','save','Copy AI prompt');
  cp.title='Copies a ready-made prompt with this paper\\'s title and abstract. Paste it into Claude.';
  cp.onclick=()=>{
    copyText(overviewPrompt(p)).then(ok=>{cp.textContent=ok?'Copied!':'Copy failed';setTimeout(()=>{cp.textContent='Copy AI prompt';},1800);});
  };
  a.append(cp);
  if(p.m){const d=el('details');d.append(el('summary',null,'Abstract'),el('p',null,p.m));a.append(d);}
  const direct=!!(fid&&fid!=='all'&&store.items[p.l]&&store.items[p.l].folders.includes(fid));
  if(fid&&fid!=='all'&&!direct){
    const its=store.items[p.l];
    const where=its?its.folders.map(folderById).filter(Boolean).map(pathName).join('; '):'';
    a.append(el('div','note','In: '+where));
  }
  if(direct){
    const mv=el('div','mv');
    const sel=document.createElement('select');
    const first=el('option',null,'Move to...');first.value='';sel.append(first);
    treeOrder().filter(o=>o.f.id!==fid).forEach(o=>{const f=o.f;const op=el('option',null,pathName(f)+' ('+f.kind+')');op.value=f.id;sel.append(op);});
    sel.onchange=()=>{if(!sel.value)return;toggle(p,sel.value,true);toggle(p,fid,false);render();};
    const rm=el('button','btn','Remove from this folder');
    rm.onclick=()=>{toggle(p,fid,false);render();};
    mv.append(sel,rm);a.append(mv);
  }
  a.append(panel);
  return a;
}

/* ---------- export ---------- */
const NL=String.fromCharCode(10);
function dl(name,mime,text){const blob=new Blob([text],{type:mime});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();a.remove();}
function doiOf(l){
  l=l||'';const low=l.toLowerCase();
  let i=low.indexOf('doi.org/10.');
  if(i>=0)return l.slice(i+8).split('?')[0].split('#')[0];
  i=low.indexOf('nature.com/articles/');
  if(i>=0)return '10.1038/'+l.slice(i+20).split('?')[0].split('#')[0].split('/')[0];
  i=low.indexOf('rxiv.org/content/10.1101/');
  if(i>=0){
    let x=l.slice(i+17).split('?')[0].split('#')[0];
    const f=x.indexOf('.full');if(f>=0)x=x.slice(0,f);
    const v=x.lastIndexOf('v');if(v>0&&x.slice(v+1)&&!isNaN(x.slice(v+1)))x=x.slice(0,v);
    return x;
  }
  return '';
}
function zoteroId(l){
  const d=doiOf(l);if(d)return d;
  const low=(l||'').toLowerCase();const i=low.indexOf('arxiv.org/abs/');
  if(i>=0){let x=l.slice(i+14).split('?')[0].split('#')[0];const v=x.lastIndexOf('v');if(v>0&&x.slice(v+1)&&!isNaN(x.slice(v+1)))x=x.slice(0,v);return 'arXiv:'+x;}
  return '';
}
function copyIds(btn){
  const sc=scope();const ids=[];let missing=0;
  sc.items.forEach(i=>{const z=zoteroId(i.l);if(z&&!ids.includes(z))ids.push(z);else if(!z)missing++;});
  copyText(ids.join(NL)).then(ok=>{
    btn.textContent=ok?('Copied '+ids.length+' IDs'+(missing?' ('+missing+' have no DOI)':'')):'Copy failed';
    setTimeout(()=>{btn.textContent='Copy DOIs (Zotero wand)';},3000);
  });
}
function scope(){
  const f=view.k==='folder'&&view.id!=='all'?folderById(view.id):null;
  const ids=f?(store.sub===false?[f.id]:descIds(f.id)):null;
  const items=Object.values(store.items).filter(i=>!ids||i.folders.some(x=>ids.includes(x))).sort((a,b)=>b.saved-a.saved);
  return {f:f,items:items,name:f?f.name:'all saved papers'};
}
function fnames(it){return it.folders.map(id=>{const f=folderById(id);return f?pathName(f):'';}).filter(Boolean);}
function slug(t){return t.toLowerCase().split(' ').join('-').replace(/[^a-z0-9-]/g,'')||'papers';}
function q(v){return '"'+String(v==null?'':v).split('"').join('""')+'"';}
function exportCSV(){
  const sc=scope();
  const rows=[['Priority journal','Folders','Title','Journal','DOI','Link','Abstract','Saved on','Status','Notes']];
  sc.items.forEach(i=>rows.push([i.pr?'yes':'',fnames(i).join('; '),i.t,i.s,doiOf(i.l),i.l,i.m||'',new Date(i.saved).toISOString().slice(0,10),'',''])); 
  dl('papers-'+slug(sc.name)+'.csv','text/csv;charset=utf-8',String.fromCharCode(0xFEFF)+rows.map(r=>r.map(q).join(',')).join(NL));
}
function exportMD(){
  const sc=scope();
  const out=[];
  const line=i=>{
    const d=doiOf(i.l);
    out.push('- [ ] **['+i.t+']('+i.l+')** - '+i.s+(d?' (doi:'+d+')':''));
    if(i.m)out.push('  > '+i.m);
    out.push('  - Notes: ');
  };
  if(sc.f){out.push('# '+sc.f.name+' ('+sc.f.kind+')','');sc.items.forEach(line);}
  else{
    out.push('# Saved papers','');
    treeOrder().forEach(o=>{
      const f=o.f;
      const its=sc.items.filter(i=>i.folders.includes(f.id));
      if(!its.length)return;
      out.push('## '+pathName(f)+' ('+f.kind+')','');its.forEach(line);out.push('');
    });
  }
  dl('papers-'+slug(sc.name)+'.md','text/markdown;charset=utf-8',out.join(NL));
}
function exportRIS(){
  const sc=scope();const out=[];
  sc.items.forEach(i=>{
    out.push('TY  - JOUR','TI  - '+i.t,'JO  - '+i.s,'UR  - '+i.l);
    const d=doiOf(i.l);if(d)out.push('DO  - '+d);
    if(i.m)out.push('AB  - '+i.m);
    fnames(i).forEach(n=>out.push('KW  - '+n));
    out.push('ER  - ','');
  });
  dl('papers-'+slug(sc.name)+'.ris','application/x-research-info-systems;charset=utf-8',out.join(NL));
}
function exportJSON(){
  store.lastBackup=Date.now();store.dirty=0;persist(true);
  dl('morning-papers-backup-'+new Date().toISOString().slice(0,10)+'.json','application/json',JSON.stringify(store,null,1));
  render();
}
function renderBanner(){
  const b=document.getElementById('banner');b.replaceChildren();
  const n=Object.keys(store.items).length;
  const stale=!store.lastBackup||(Date.now()-store.lastBackup)>7*864e5;
  if(!n||!(store.dirty>0)||!stale)return;
  const box=el('div','banner');
  box.append(el('span',null,n+' saved papers live only in this browser and have not been backed up recently.'));
  const btn=el('button','btn','Download backup');btn.onclick=exportJSON;box.append(btn);
  b.append(box);
}

/* ---------- sidebar ---------- */
function folderCount(f){return Object.values(store.items).filter(i=>i.folders.includes(f.id)).length;}
function sideItem(name,n,v,folder,depth){
  const row=el('div','sitem'+(view.k==='folder'&&view.id===v.id?' on':''));
  row.style.paddingLeft=(8+(depth||0)*14)+'px';
  const left=el('span','sl');
  if(folder&&kids(folder.id).length){
    const open=!isCollapsed(folder);
    const tg=el('span','tog',open?'\\u25be':'\\u25b8');
    tg.onclick=e=>{e.stopPropagation();store.collapsed=store.collapsed||{};store.collapsed[folder.id]=open;persist(true);renderSide();};
    left.append(tg);
  }else if(folder)left.append(el('span','tog',''));
  left.append(el('span',null,name));
  row.append(left,el('span','n',String(n)));
  row.onclick=()=>{go(v);if(window.innerWidth<900)setDrawer(false);};
  if(folder){
    row.addEventListener('dragover',e=>{if(dragP){e.preventDefault();row.classList.add('drop');}});
    row.addEventListener('dragleave',()=>row.classList.remove('drop'));
    row.addEventListener('drop',e=>{
      e.preventDefault();row.classList.remove('drop');
      if(dragP){
        toggle(dragP,folder.id,true);
        const n=row.querySelector('.n');if(n)n.textContent='\\u2713 saved';
        const keep=autoOpened;endDrag();
        if(keep){setDrawer(true,false);setTimeout(()=>{if(!autoOpened&&!store.drawer)setDrawer(false,false);render();},700);}else render();
      }
    });
  }
  return row;
}
function renderSide(){
  const s=document.getElementById('side');s.replaceChildren();
  const h3=el('h3',null,'My folders');
  const x=el('button','btn','Close');x.onclick=()=>setDrawer(false);h3.append(x);
  s.append(h3);
  s.append(el('p','note','Drag any paper from the feed onto a folder to save it.'));
  s.append(sideItem('All saved',Object.keys(store.items).length,{k:'folder',id:'all'},null));
  [['project','Projects'],['question','Questions']].forEach(g=>{
    const fs=roots(g[0]);
    if(!fs.length)return;
    s.append(el('div','sgroup',g[1]));
    const draw=(f,d)=>{
      s.append(sideItem(f.name,folderCount(f),{k:'folder',id:f.id},f,d));
      if(view.k==='folder'&&view.id===f.id){
        const act=el('div','sact');
        const ren=el('button','btn','Rename');
        ren.onclick=()=>{const n=prompt('Folder name',f.name);if(n&&n.trim()){f.name=n.trim();persist();render();}};
        const sub=el('button','btn','+ Subfolder');
        sub.onclick=()=>{
          const n=prompt('Subfolder name');
          if(n&&n.trim()){store.folders.push({id:uid(),name:n.trim(),kind:f.kind,parent:f.id});if(store.collapsed)delete store.collapsed[f.id];persist();render();}
        };
        const mvsel=document.createElement('select');
        const m0=el('option',null,'Move folder to...');m0.value='';mvsel.append(m0);
        const mt=el('option',null,'(top level)');mt.value='__top';mvsel.append(mt);
        const bad=descIds(f.id);
        treeOrder().filter(o=>!bad.includes(o.f.id)).forEach(o=>{const op=el('option',null,pathName(o.f));op.value=o.f.id;mvsel.append(op);});
        mvsel.onchange=()=>{
          if(!mvsel.value)return;
          const np=mvsel.value==='__top'?'':mvsel.value;
          f.parent=np;
          if(np){const pk=folderById(np).kind;descIds(f.id).forEach(id=>{const x=folderById(id);if(x)x.kind=pk;});}
          persist();render();
        };
        const del=el('button','btn','Delete');
        del.onclick=()=>{
          const ch=kids(f.id);
          if(!confirm('Delete "'+f.name+'"? Papers stay saved in any other folders.'+(ch.length?' Its subfolders move up one level.':'')))return;
          ch.forEach(k=>{k.parent=f.parent||'';});
          store.folders=store.folders.filter(x=>x.id!==f.id);
          Object.keys(store.items).forEach(k=>{const it=store.items[k];it.folders=it.folders.filter(x=>x!==f.id);if(!it.folders.length)delete store.items[k];});
          persist();
          go({k:'folder',id:'all'});
        };
        act.append(ren,sub,mvsel,del);s.append(act);
      }
      if(!isCollapsed(f))kids(f.id).forEach(k=>draw(k,d+1));
    };
    fs.forEach(f=>draw(f,0));
  });
  if(!store.folders.length)s.append(el('p','note','No folders yet. Create a project or a question below, then save papers to it (or drag a paper onto a folder).'));
  const ex=el('div','sform');
  ex.append(el('div','sgroup','Export for triage'));
  ex.append(el('p','note','Exports: '+scope().name));
  const b1=el('button','btn','Spreadsheet (CSV)');b1.onclick=exportCSV;
  const b2=el('button','btn','Notes (Markdown)');b2.onclick=exportMD;
  const b3=el('button','btn','Zotero / EndNote (RIS)');b3.onclick=exportRIS;
  const b4=el('button','btn','Copy DOIs (Zotero wand)');b4.onclick=()=>copyIds(b4);
  ex.append(b3,b4,b1,b2);s.append(ex);
  const form=el('div','sform');
  const inp=document.createElement('input');inp.type='text';inp.placeholder='New folder name';
  const sel=kindSelect();
  const par=document.createElement('select');
  const p0=el('option',null,'Inside: top level');p0.value='';par.append(p0);
  treeOrder().forEach(o=>{const op=el('option',null,'Inside: '+pathName(o.f));op.value=o.f.id;par.append(op);});
  par.onchange=()=>{const pf=par.value?folderById(par.value):null;sel.disabled=!!pf;if(pf)sel.value=pf.kind;};
  const add=el('button','btn','Create folder');
  add.onclick=()=>{
    const n=inp.value.trim();if(!n)return;
    const pf=par.value?folderById(par.value):null;
    const f={id:uid(),name:n,kind:pf?pf.kind:sel.value,parent:pf?pf.id:''};
    store.folders.push(f);persist();go({k:'folder',id:f.id});
  };
  form.append(inp,par,sel,add);s.append(form);

  const bk=el('div','sform');
  const exp=el('button','btn','Backup (JSON)');
  exp.onclick=exportJSON;
  const imp=el('button','btn','Import backup');
  const file=document.createElement('input');file.type='file';file.accept='application/json';file.hidden=true;
  file.onchange=()=>{
    const r=new FileReader();
    r.onload=()=>{
      try{
        const o=JSON.parse(r.result);
        (o.folders||[]).forEach(f=>{if(f&&f.id&&f.name&&!folderById(String(f.id)))store.folders.push({id:String(f.id),name:String(f.name),kind:f.kind==='question'?'question':'project',parent:f.parent?String(f.parent):''});});
        Object.values(o.items||{}).forEach(i=>{
          if(!i||!i.l)return;
          const cur=store.items[i.l]||(store.items[i.l]={l:String(i.l),t:String(i.t||''),s:String(i.s||''),m:String(i.m||''),pr:i.pr?1:0,saved:Number(i.saved)||Date.now(),folders:[]});
          (i.folders||[]).forEach(id=>{if(folderById(id)&&!cur.folders.includes(id))cur.folders.push(id);});
          if(!cur.folders.length)delete store.items[i.l];
        });
        persist();render();
      }catch(e){alert('That file could not be read.');}
    };
    r.readAsText(file.files[0]);
  };
  imp.onclick=()=>file.click();
  bk.append(el('div','sgroup','Safety copy'),exp,imp,file,el('p','note','Folders are stored in this browser only. Download a backup now and then.'));
  s.append(bk);
}

/* ---------- main area ---------- */
function matches(p,q){return !q||(p.t+' '+p.s+' '+(p.m||'')).toLowerCase().includes(q);}

function inTab(p,tab){return p.y===tab||(tab==='research'&&(p.x===1||(p.tx===1&&p.y==='clinical')));}
function renderTabs(){
  const nav=document.getElementById('tabs');nav.replaceChildren();
  TABS.forEach(t=>{
    const n=D.papers.filter(p=>inTab(p,t[0])).length;
    const b=el('button',view.k==='tab'&&view.id===t[0]?'on':'',t[1]);b.append(el('small',null,String(n)));
    b.onclick=()=>go({k:'tab',id:t[0]});
    nav.append(b);
  });
}

function renderPapers(box,q){
  const list=D.papers.filter(p=>inTab(p,view.id)&&matches(p,q));
  if(!list.length){box.append(el('p','empty',q?'Nothing matches that filter.':'No papers here yet.'));return;}
  const newest=list[0].d;
  let day=null,sec=null;
  list.forEach(p=>{
    if(p.d!==day){
      day=p.d;sec=el('details','day');
      const key=view.id+'|'+day;
      sec.open=!!q||day===newest||openDays.has(key);
      const sm=el('summary',null,p.dl+' ');sm.append(el('span',null,'('+list.filter(x=>x.d===day).length+')'));
      sec.append(sm);box.append(sec);
      sec.cards=el('div','cards');sec.append(sec.cards);
      const d=day,me=sec;
      me.addEventListener('toggle',()=>{if(q||d===newest)return;if(me.open)openDays.add(key);else openDays.delete(key);});
    }
    sec.cards.append(card(p));
  });
}
const openDays=new Set();

function renderFolder(box,q){
  const id=view.id;
  const f=id==='all'?null:folderById(id);
  if(id!=='all'&&!f){box.append(el('p','empty','That folder no longer exists.'));return;}
  const ids=f?(store.sub===false?[id]:descIds(id)):null;
  const items=Object.values(store.items).filter(i=>(!ids||i.folders.some(x=>ids.includes(x)))&&matches(i,q)).sort((a,b)=>b.saved-a.saved);
  box.append(el('h2',null,f?pathName(f)+' ('+f.kind+')':'All saved papers'));
  if(f&&kids(f.id).length){
    const lab=el('label','note');
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=store.sub!==false;
    cb.onchange=()=>{store.sub=cb.checked;persist(true);render();};
    lab.append(cb,' Include papers from subfolders');box.append(lab);
  }
  if(!items.length)box.append(el('p','empty',q?'Nothing matches that filter.':'Nothing saved here yet. Use Save on a paper, or drag it onto this folder in the sidebar.'));
  const wrap=el('div','cards');box.append(wrap);
  items.forEach(p=>wrap.append(card(p,id)));
}

function render(){
  renderBanner();renderSide();renderTabs();
  const box=document.getElementById('view');box.replaceChildren();
  const q=document.getElementById('q').value.trim().toLowerCase();
  if(view.k==='folder')renderFolder(box,q);else renderPapers(box,q);
}
document.getElementById('q').addEventListener('input',render);
const dtab=document.getElementById('dtab');
dtab.onclick=()=>setDrawer(!drawerOpen());
dtab.addEventListener('dragenter',e=>{if(dragP){e.preventDefault();dtab.classList.add('drop');if(!drawerOpen()){autoOpened=true;setDrawer(true,false);}}});
dtab.addEventListener('dragover',e=>{if(dragP)e.preventDefault();});
dtab.addEventListener('dragleave',()=>dtab.classList.remove('drop'));
dtab.addEventListener('drop',e=>{e.preventDefault();dtab.classList.remove('drop');});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drawerOpen())setDrawer(false);});
setDrawer(typeof store.drawer==="boolean"&&window.innerWidth>=900?store.drawer:window.innerWidth>=900,false);
window.addEventListener('hashchange',()=>{view=parseHash();render();});
render();
})();
</script>
</body></html>
"""


def render(state, now, keep_days):
    today = now.astimezone(TZ).date().isoformat()
    rows = sorted(state["items"].items(),
                  key=lambda kv: (kv[1]["first_seen"], is_priority(kv[1]["source"]), kv[1]["published"]),
                  reverse=True)
    papers = [{
        "l": link, "t": it["title"], "s": it["source"], "m": it["summary"],
        "d": it["first_seen"], "dl": day_label(it["first_seen"], today),
        "y": classify(it["title"], it["source"], it["summary"], link),
        "pr": 1 if is_priority(it["source"]) else 0,
        "x": 1 if it["source"].lower().startswith(PREPRINT_SOURCES)
             and not it["source"].lower().startswith(AI_SOURCES)
             and hyper_relevant(it["title"], it["summary"]) else 0,
        "tx": 1 if not it["source"].lower().startswith(AI_SOURCES)
              and is_translational(it["title"], it["summary"]) else 0,
    } for link, it in rows]
    data = {
        "papers": papers,
        "status": sorted(state["feed_status"].items()),
        "updated": state["updated"] or "never",
        "keep": keep_days,
    }
    # "<" escaped so abstract text can never close the script tag
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", blob)


def run(feeds, state_path, out_path, ingest_days, keep_days, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    state = load_state(state_path)
    ingest(feeds, state, now, ingest_days)
    refilter(state)
    prune(state, now, keep_days)
    pathlib.Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(state_path).write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out_path).write_text(render(state, now, keep_days), encoding="utf-8")
    return state


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--ingest-days", type=int, default=3,
                    help="ignore papers published more than N days ago when first seen")
    ap.add_argument("--keep-days", type=int, default=14, help="how many days of history to keep on the page")
    args = ap.parse_args(argv)
    state = run(all_feeds(), args.state, args.out, args.ingest_days, args.keep_days)
    for name, s in sorted(state["feed_status"].items()):
        print(f"  {name}: {s}")
    print(f"{len(state['items'])} papers on page; wrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
