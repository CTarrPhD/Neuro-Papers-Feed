#!/usr/bin/env python3
"""
filter_feeds.py - keep only neuroscience / neurodegeneration-relevant items from
high-volume feeds and write them to one RSS file you can subscribe to.

Usage:
    pip install feedparser
    python3 filter_feeds.py                  # writes filtered-neuro-feed.xml, last 7 days
    python3 filter_feeds.py --days 3 --out my-feed.xml

Edit the KEYWORDS sections and FEEDS list below to tune what gets through.
Run it daily (cron, launchd, or a GitHub Action) and point your reader at the output file.
"""
import argparse
import calendar
import datetime as dt
import email.utils
import html
import re
import sys
import xml.etree.ElementTree as ET

import feedparser

# --------------------------------------------------------------------------
# Keywords. Stems are case-insensitive and match at the start of a word.
# Acronyms are case-sensitive and match as whole words.
# --------------------------------------------------------------------------
NEURO_STEMS = [
    r"neuro",                       # neuroscience, neuronal, neurodegeneration, neuropathology...
    r"neuron",
    r"alzheimer", r"parkinson", r"dementia", r"huntington", r"frontotemporal",
    r"amyotrophic", r"tauopath", r"\btau\b", r"amyloid", r"synuclein", r"prion",
    r"microglia", r"astrocyt", r"oligodendrocyt", r"\bglia", r"synap",
    r"brain", r"cortex", r"cortical", r"hippocamp", r"cerebell", r"cerebr",
    r"neural (?:circuit|activity|crest|stem|progenitor|development|dynamics|population|coding|decoding|response|tube)",
    r"induced pluripotent", r"organoid", r"cognitive decline",
]
NEURO_ACRONYMS = [
    "MAPT", "ALS", "FTD", "FTLD", "APOE", "TREM2", "LRRK2", "GBA1", "GRN", "C9orf72",
    "TDP-43", "iPSC", "iPSCs", "hiPSC", "hiPSCs", "EEG", "fMRI", "MEG",
]
# Your own methods interests (drop these if the feed gets noisy)
EXTRA_STEMS = [r"crispr", r"base edit", r"prime edit", r"guide rna", r"off-target"]

# Added for genomics-flavored feeds
GENOMICS_STEMS = [
    r"gwas", r"genome-wide association", r"eqtl", r"single-cell", r"single-nucle",
    r"splicing", r"polygenic", r"variant effect", r"gene regulat", r"epigen",
]

# Added for machine-learning feeds (most ML papers are irrelevant; keep biology/neuro ones).
# Kept deliberately narrow: broad terms like "molecul" or "biolog" let through dozens of
# generic ML papers a day. Add terms back if you want a wider net.
AI_BIO_STEMS = [
    r"protein", r"genom", r"single-cell", r"single-nucle", r"gene expression",
    r"drug discovery", r"connectom", r"scientific literature",
]

PROFILES = {
    # name: (stems, acronyms)
    "neuro":    (NEURO_STEMS + EXTRA_STEMS, NEURO_ACRONYMS),
    "genomics": (NEURO_STEMS + EXTRA_STEMS + GENOMICS_STEMS, NEURO_ACRONYMS),
    "ai":       (NEURO_STEMS + EXTRA_STEMS + AI_BIO_STEMS, NEURO_ACRONYMS),
}

# --------------------------------------------------------------------------
# Feeds to filter: (display name, URL, profile)
# URLs marked "untested" were not reachable for checking when this was built.
# --------------------------------------------------------------------------
FEEDS = [
    ("Nature", "https://www.nature.com/nature.rss", "neuro"),
    ("Nature Communications", "https://www.nature.com/ncomms.rss", "neuro"),
    ("Scientific Reports (Neuroscience)", "https://www.nature.com/subjects/neuroscience/srep.rss", "neuro"),
    ("eLife", "https://elifesciences.org/rss/recent.xml", "neuro"),
    ("bioRxiv Genomics", "https://connect.biorxiv.org/biorxiv_xml.php?subject=genomics", "genomics"),
    ("arXiv q-bio.GN", "https://rss.arxiv.org/rss/q-bio.GN", "genomics"),
    ("arXiv q-bio.QM", "https://rss.arxiv.org/rss/q-bio.QM", "genomics"),
    ("arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG", "ai"),
    # untested URLs:
    ("Science", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science", "neuro"),
    ("Science Translational Medicine", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=stm", "neuro"),
    ("Cell", "https://www.cell.com/cell/current.rss", "neuro"),
    ("Cell Reports", "https://www.cell.com/cell-reports/current.rss", "neuro"),
    ("bioRxiv Genetics", "https://connect.biorxiv.org/biorxiv_xml.php?subject=genetics", "genomics"),
    ("bioRxiv Cell Biology", "https://connect.biorxiv.org/biorxiv_xml.php?subject=cell_biology", "neuro"),
    ("bioRxiv Molecular Biology", "https://connect.biorxiv.org/biorxiv_xml.php?subject=molecular_biology", "neuro"),
    ("bioRxiv Bioinformatics", "https://connect.biorxiv.org/biorxiv_xml.php?subject=bioinformatics", "genomics"),
    ("arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI", "ai"),
    ("arXiv stat.ML", "https://rss.arxiv.org/rss/stat.ML", "ai"),
]


def compile_profile(name):
    stems, acronyms = PROFILES[name]
    stem_re = re.compile("|".join(stems), re.IGNORECASE)
    acr_re = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(a) for a in acronyms) + r")(?![A-Za-z0-9])")
    return stem_re, acr_re


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc)
    return None


def matches(entry, stem_re, acr_re):
    text = clean(entry.get("title", "")) + " " + clean(entry.get("summary", ""))
    return bool(stem_re.search(text) or acr_re.search(text))


def collect(feeds, days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    seen, items, report = set(), [], []
    compiled = {}
    for name, url, profile in feeds:
        if profile not in compiled:
            compiled[profile] = compile_profile(profile)
        stem_re, acr_re = compiled[profile]
        parsed = feedparser.parse(url)
        if not parsed.entries:
            report.append(f"  SKIPPED {name}: no entries (bad URL, blocked, or empty)")
            continue
        total = kept = 0
        for e in parsed.entries:
            total += 1
            link = e.get("link") or e.get("id")
            if not link or link in seen:
                continue
            when = entry_time(e) or dt.datetime.now(dt.timezone.utc)
            if when < cutoff or not matches(e, stem_re, acr_re):
                continue
            seen.add(link)
            kept += 1
            items.append((when, name, clean(e.get("title", "")), link, clean(e.get("summary", ""))))
        report.append(f"  {name}: kept {kept} of {total}")
    items.sort(key=lambda x: x[0], reverse=True)
    return items, report


def write_rss(items, path):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Filtered neuroscience / neurodegeneration papers"
    ET.SubElement(ch, "link").text = "https://example.invalid/filtered-neuro-feed"
    ET.SubElement(ch, "description").text = "Keyword-filtered items from high-volume feeds."
    for when, source, title, link, summary in items:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = f"[{source}] {title}"
        ET.SubElement(it, "link").text = link
        ET.SubElement(it, "guid").text = link
        ET.SubElement(it, "pubDate").text = email.utils.format_datetime(when)
        ET.SubElement(it, "description").text = summary[:1500]
    ET.indent(rss)
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="only keep items from the last N days")
    ap.add_argument("--out", default="filtered-neuro-feed.xml")
    args = ap.parse_args(argv)
    items, report = collect(FEEDS, args.days)
    write_rss(items, args.out)
    print("\n".join(report))
    print(f"Wrote {len(items)} items to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
