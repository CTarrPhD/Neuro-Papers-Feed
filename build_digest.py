#!/usr/bin/env python3
"""
build_digest.py - fetch all feeds, keep only new papers, and render a scrollable page.

Every run:
  1. Fetches every feed in UNFILTERED (kept as-is) and filter_feeds.FEEDS (keyword-filtered).
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
import sys
from zoneinfo import ZoneInfo

import feedparser

import filter_feeds as ff

TZ = ZoneInfo("America/Los_Angeles")
feedparser.USER_AGENT = "Mozilla/5.0 (compatible; neuro-digest/1.0; personal feed reader)"

# Feeds shown without keyword filtering (field-specific, manageable volume)
UNFILTERED = [
    ("Nature Neuroscience", "https://www.nature.com/neuro.rss"),
    ("bioRxiv Neuroscience", "https://connect.biorxiv.org/biorxiv_xml.php?subject=neuroscience"),
    ("medRxiv Neurology", "https://connect.medrxiv.org/medrxiv_xml.php?subject=neurology"),
    ("arXiv q-bio.NC", "https://rss.arxiv.org/rss/q-bio.NC"),
    ("Nature Medicine", "https://www.nature.com/nm.rss"),
    ("Nature Genetics", "https://www.nature.com/ng.rss"),
    ("Nature Aging", "https://www.nature.com/nataging.rss"),
    ("Nature Reviews Neuroscience", "https://www.nature.com/nrn.rss"),
    ("Nature Reviews Neurology", "https://www.nature.com/nrneurol.rss"),
    ("Nature Methods", "https://www.nature.com/nmeth.rss"),
    ("Nature Biotechnology", "https://www.nature.com/nbt.rss"),
    ("Molecular Psychiatry", "https://www.nature.com/mp.rss"),
    ("Neuron", "https://www.cell.com/neuron/current.rss"),
    ("Cell Stem Cell", "https://www.cell.com/cell-stem-cell/current.rss"),
    ("Stem Cell Reports", "https://www.cell.com/stem-cell-reports/current.rss"),
    ("Alzheimer's & Dementia", "https://alz-journals.onlinelibrary.wiley.com/feed/15525279/most-recent"),
    ("Acta Neuropathologica", "https://link.springer.com/search.rss?facet-journal-id=401&query=*&search-within=Journal&sortOrder=newestFirst"),
    ("Molecular Neurodegeneration", "https://link.springer.com/search.rss?facet-journal-id=13024&query=*&search-within=Journal&sortOrder=newestFirst"),
    ("Lancet Neurology", "https://www.thelancet.com/rssfeed/laneur_current.xml"),
]


def all_feeds():
    return [(n, u, "all") for n, u in UNFILTERED] + list(ff.FEEDS)


def load_state(path):
    p = pathlib.Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"items": {}, "feed_status": {}, "updated": None}


def ingest(feeds, state, now, ingest_days):
    cutoff = now - dt.timedelta(days=ingest_days)
    today = now.astimezone(TZ).date().isoformat()
    compiled = {}
    status = {}
    for name, url, profile in feeds:
        if profile != "all" and profile not in compiled:
            compiled[profile] = ff.compile_profile(profile)
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # network or parse failure should never stop the run
            status[name] = f"error: {exc.__class__.__name__}"
            continue
        if not parsed.entries:
            status[name] = "no entries (bad URL, blocked, or empty)"
            continue
        total = new = 0
        for e in parsed.entries:
            total += 1
            link = e.get("link") or e.get("id")
            if not link or link in state["items"]:
                continue
            when = ff.entry_time(e) or now
            if when < cutoff:
                continue
            if profile != "all" and not ff.matches(e, *compiled[profile]):
                continue
            state["items"][link] = {
                "title": ff.clean(e.get("title", "")) or "(untitled)",
                "source": name,
                "published": when.isoformat(),
                "summary": ff.clean(e.get("summary", ""))[:700],
                "first_seen": today,
            }
            new += 1
        status[name] = f"{new} new of {total}"
    state["feed_status"] = status
    state["updated"] = now.astimezone(TZ).strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")


def prune(state, now, keep_days):
    floor = (now.astimezone(TZ).date() - dt.timedelta(days=keep_days)).isoformat()
    state["items"] = {k: v for k, v in state["items"].items() if v["first_seen"] >= floor}


def day_label(day_iso, today_iso):
    d = dt.date.fromisoformat(day_iso)
    pretty = d.strftime("%A, %B %d").replace(" 0", " ")
    return f"New today - {pretty}" if day_iso == today_iso else pretty


TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Morning papers</title>
<style>
:root{--bg:#fafaf7;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e4e3dc;--accent:#2f5d8a;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe5;--muted:#a09f98;--card:#1f1f1c;--line:#33332e;--accent:#8fb4dc;--chip:#2a2f35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:1.5rem;margin:8px 0 2px}
.sub{color:var(--muted);font-size:.9rem;margin-bottom:14px}
#q{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;margin-bottom:6px}
h2{font-size:1.05rem;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h2 span{color:var(--muted);font-weight:400;font-size:.9rem}
.paper{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 10px}
.paper a.t{color:var(--fg);font-weight:600;text-decoration:none}
.paper a.t:hover{color:var(--accent);text-decoration:underline}
.src{display:inline-block;background:var(--chip);color:var(--muted);font-size:.78rem;padding:1px 8px;border-radius:99px;margin-top:6px}
details{margin-top:6px;color:var(--muted);font-size:.92rem}
summary{cursor:pointer;color:var(--accent)}
.status{margin-top:36px;color:var(--muted);font-size:.82rem}
.status summary{color:var(--muted)}
.empty{color:var(--muted);padding:20px 0}
</style></head>
<body><main>
<h1>Morning papers</h1>
<div class="sub">Updated __UPDATED__ &middot; __TOTAL__ papers in the last __KEEP__ days</div>
<input id="q" type="search" placeholder="Filter by keyword (tau, iPSC, microglia...)" autocomplete="off">
__BODY__
<details class="status"><summary>Feed status (check for broken feeds)</summary><ul>__STATUS__</ul></details>
</main>
<script>
const q=document.getElementById('q');
q.addEventListener('input',()=>{
  const t=q.value.trim().toLowerCase();
  document.querySelectorAll('.day').forEach(day=>{
    let shown=0;
    day.querySelectorAll('.paper').forEach(p=>{
      const hit=!t||p.textContent.toLowerCase().includes(t);
      p.style.display=hit?'':'none'; if(hit)shown++;
    });
    day.style.display=shown?'':'none';
  });
});
</script>
</body></html>
"""


def render(state, now, keep_days):
    today = now.astimezone(TZ).date().isoformat()
    by_day = {}
    for link, it in state["items"].items():
        by_day.setdefault(it["first_seen"], []).append((link, it))
    parts = []
    if not by_day:
        parts.append('<p class="empty">No papers yet. Check the feed status below.</p>')
    for day in sorted(by_day, reverse=True):
        papers = sorted(by_day[day], key=lambda x: x[1]["published"], reverse=True)
        parts.append(f'<section class="day"><h2>{html.escape(day_label(day, today))} '
                     f'<span>({len(papers)})</span></h2>')
        for link, it in papers:
            summary = ""
            if it["summary"]:
                summary = f'<details><summary>Abstract</summary><p>{html.escape(it["summary"])}</p></details>'
            parts.append(
                '<article class="paper">'
                f'<a class="t" href="{html.escape(link, quote=True)}" target="_blank" rel="noopener noreferrer">'
                f'{html.escape(it["title"])}</a><br>'
                f'<span class="src">{html.escape(it["source"])}</span>{summary}</article>')
        parts.append("</section>")
    status = "".join(f"<li>{html.escape(n)}: {html.escape(s)}</li>"
                     for n, s in sorted(state["feed_status"].items()))
    return (TEMPLATE.replace("__UPDATED__", html.escape(state["updated"] or "never"))
            .replace("__TOTAL__", str(len(state["items"])))
            .replace("__KEEP__", str(keep_days))
            .replace("__BODY__", "\n".join(parts))
            .replace("__STATUS__", status))


def run(feeds, state_path, out_path, ingest_days, keep_days, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    state = load_state(state_path)
    ingest(feeds, state, now, ingest_days)
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
