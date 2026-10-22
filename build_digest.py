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
    ("Nature Medicine", "https://www.nature.com/nm.rss", None),
    ("Nature Genetics", "https://www.nature.com/ng.rss", "1061-4036"),
    ("Nature Aging", "https://www.nature.com/nataging.rss", "2662-8465"),
    ("Nature Reviews Neuroscience", "https://www.nature.com/nrn.rss", None),
    ("Nature Reviews Neurology", "https://www.nature.com/nrneurol.rss", "1759-4766"),
    ("Nature Methods", "https://www.nature.com/nmeth.rss", None),
    ("Nature Biotechnology", "https://www.nature.com/nbt.rss", None),
    ("Molecular Psychiatry", "https://www.nature.com/mp.rss", None),
    ("Neuron", "https://www.cell.com/neuron/current.rss", None),
    ("Cell Stem Cell", "https://www.cell.com/cell-stem-cell/current.rss", None),
    ("Stem Cell Reports", "https://www.cell.com/stem-cell-reports/current.rss", None),
    ("Alzheimer's & Dementia", "https://alz-journals.onlinelibrary.wiley.com/feed/15525279/most-recent", None),
    ("Lancet Neurology", "https://www.thelancet.com/rssfeed/laneur_current.xml", None),
    # No usable RSS feed: pulled from Crossref
    ("Acta Neuropathologica", None, "1432-0533"),
    ("Molecular Neurodegeneration", None, "1750-1326"),
    ("Brain", None, "1460-2156"),
    ("JAMA Neurology", None, "2168-6149"),
    ("Annals of Neurology", None, "1531-8249"),
    ("Neurobiology of Aging", None, "0197-4580"),
]

# Skip correction notices
SKIP_TITLE = re.compile(r"^\s*(publisher |author )?(correction|erratum)\b", re.IGNORECASE)


def all_feeds():
    return ([(n, u, "all", i) for n, u, i in UNFILTERED]
            + [(n, u, p, None) for n, u, p in ff.FEEDS])


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


def fetch_crossref(issn, days):
    """Recent journal articles for an ISSN from Crossref, shaped like feed entries."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    params = {
        "filter": f"from-created-date:{since},type:journal-article",
        "rows": "100", "sort": "created", "order": "desc",
        "select": "DOI,title,abstract,created,URL",
    }
    url = f"https://api.crossref.org/journals/{issn}/works?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            break
        except Exception as exc:
            last = f"{exc.__class__.__name__}"
            time.sleep(3)
    else:
        return [], f"crossref error: {last}"
    entries = []
    for it in data.get("message", {}).get("items", []):
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
    return entries, (None if entries else "crossref returned nothing")


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


def classify(title, source, summary):
    """Best-effort article type. Feeds rarely label this, so it works from the source and wording."""
    s = source.lower()
    if s.startswith(PREPRINT_SOURCES):
        return "preprints"
    if s.startswith("nature reviews"):
        return "reviews"
    if RE_COMMENTARY.match(title):
        return "commentary"
    if RE_REVIEW.search(title) or RE_REVIEW.search(summary[:300]):
        return "reviews"
    if RE_CLINICAL.search(title) or RE_CLINICAL.search(summary[:300]):
        return "clinical"
    return "research"


# ---------------------------------------------------------------- page

TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Morning papers</title>
<style>
:root{--bg:#fafaf7;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e4e3dc;--accent:#2f5d8a;--chip:#eef2f6;--on:#e3eefa}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe5;--muted:#a09f98;--card:#1f1f1c;--line:#33332e;--accent:#8fb4dc;--chip:#2a2f35;--on:#25384d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px 60px}
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
button.save{margin-left:8px;border:1px solid var(--line);background:none;color:var(--accent);border-radius:99px;padding:1px 10px;font:inherit;font-size:.78rem;cursor:pointer}
button.save.on{background:var(--on)}
.panel{margin-top:8px;padding:8px 10px;border:1px dashed var(--line);border-radius:8px;font-size:.9rem}
.panel label{display:block;padding:2px 0}
.panel .row{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.panel input[type=text],.panel select,.fform input[type=text],.fform select{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit;font-size:.9rem}
.panel input[type=text],.fform input[type=text]{flex:1;min-width:120px}
.btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:5px 10px;font:inherit;font-size:.9rem;cursor:pointer}
details{margin-top:6px;color:var(--muted);font-size:.92rem}
summary{cursor:pointer;color:var(--accent)}
.fgroup{margin:14px 0 4px;color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
.fchips{display:flex;gap:6px;flex-wrap:wrap}
.fchips button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 10px;font:inherit;font-size:.9rem;cursor:pointer}
.fchips button.on{background:var(--on);border-color:var(--accent)}
.fform{display:flex;gap:6px;flex-wrap:wrap;margin:14px 0}
.ftools{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
.note{color:var(--muted);font-size:.85rem}
.status{margin-top:36px;color:var(--muted);font-size:.82rem}
.status summary{color:var(--muted)}
.empty{color:var(--muted);padding:20px 0}
</style></head>
<body><main>
<h1>Morning papers</h1>
<div class="sub" id="sub"></div>
<input id="q" type="search" placeholder="Filter this tab (tau, iPSC, microglia...)" autocomplete="off">
<nav id="tabs"></nav>
<div id="view"></div>
<details class="status"><summary>Feed status (check for broken feeds)</summary><ul id="status"></ul></details>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('data').textContent);
const TABS=[["research","Research"],["preprints","Preprints"],["reviews","Reviews"],["commentary","Commentary"],["clinical","Clinical"],["folders","Folders"]];
const KEY='morning-papers-v1';
let store={folders:[],items:{}};
try{const s=localStorage.getItem(KEY);if(s)store=JSON.parse(s);}catch(e){}
if(!Array.isArray(store.folders))store.folders=[];
if(!store.items||typeof store.items!=='object')store.items={};
function persist(){try{localStorage.setItem(KEY,JSON.stringify(store));}catch(e){}}

let tab=(location.hash||'#research').slice(1);
if(!TABS.some(t=>t[0]===tab))tab='research';
let folderSel=null;

function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
function safeUrl(u){return /^https?:\\/\\//i.test(u||'')?u:'#';}
function uid(){return 'f'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);}
function folderById(id){return store.folders.find(f=>f.id===id);}

document.getElementById('sub').textContent='Updated '+D.updated+' \\u00b7 '+D.papers.length+' papers in the last '+D.keep+' days';
const st=document.getElementById('status');
D.status.forEach(s=>st.append(el('li',null,s[0]+': '+s[1])));

/* ---------- saving ---------- */
function toggle(p,fid,on){
  let it=store.items[p.l];
  if(!it)it=store.items[p.l]={l:p.l,t:p.t,s:p.s,m:p.m,saved:Date.now(),folders:[]};
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
  store.folders.forEach(f=>{
    const lab=el('label');
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=!!(it&&it.folders.includes(f.id));
    cb.onchange=()=>{toggle(p,f.id,cb.checked);label(btn,p);};
    lab.append(cb,' '+f.name+' ',el('span','note','('+(f.kind==='project'?'project':'question')+')'));
    panel.append(lab);
  });
  const row=el('div','row');
  const inp=document.createElement('input');inp.type='text';inp.placeholder='New folder name';
  const sel=document.createElement('select');
  [['project','Project'],['question','Question']].forEach(o=>{const op=el('option',null,o[1]);op.value=o[0];sel.append(op);});
  const add=el('button','btn','Create + save here');
  add.onclick=()=>{
    const name=inp.value.trim();if(!name)return;
    const f={id:uid(),name:name,kind:sel.value};
    store.folders.push(f);toggle(p,f.id,true);label(btn,p);fillPanel(panel,p,btn);
  };
  row.append(inp,sel,add);panel.append(row);
}

function card(p){
  const a=el('article','paper');
  const t=el('a','t',p.t);t.href=safeUrl(p.l);t.target='_blank';t.rel='noopener noreferrer';
  a.append(t,el('br'),el('span','src',p.s));
  const btn=el('button','save');label(btn,p);
  const panel=el('div','panel');panel.hidden=true;
  btn.onclick=()=>{panel.hidden=!panel.hidden;if(!panel.hidden)fillPanel(panel,p,btn);};
  a.append(btn);
  if(p.m){const d=el('details');d.append(el('summary',null,'Abstract'),el('p',null,p.m));a.append(d);}
  a.append(panel);
  return a;
}

/* ---------- views ---------- */
function matches(p,q){return !q||(p.t+' '+p.s+' '+(p.m||'')).toLowerCase().includes(q);}

function renderTabs(){
  const nav=document.getElementById('tabs');nav.replaceChildren();
  TABS.forEach(t=>{
    const n=t[0]==='folders'?store.folders.length:D.papers.filter(p=>p.y===t[0]).length;
    const b=el('button',t[0]===tab?'on':'',t[1]);b.append(el('small',null,String(n)));
    b.onclick=()=>{tab=t[0];location.hash=tab;render();};
    nav.append(b);
  });
}

function renderPapers(view,q){
  const list=D.papers.filter(p=>p.y===tab&&matches(p,q));
  if(!list.length){view.append(el('p','empty',q?'Nothing matches that filter.':'No papers here yet.'));return;}
  let day=null,sec=null;
  list.forEach(p=>{
    if(p.d!==day){
      day=p.d;sec=el('section','day');
      const h=el('h2',null,p.dl+' ');h.append(el('span',null,'('+list.filter(x=>x.d===day).length+')'));
      sec.append(h);view.append(sec);
    }
    sec.append(card(p));
  });
}

function folderCount(f){return Object.values(store.items).filter(i=>i.folders.includes(f.id)).length;}

function renderFolders(view,q){
  const form=el('div','fform');
  const inp=document.createElement('input');inp.type='text';inp.placeholder='New folder name';
  const sel=document.createElement('select');
  [['project','Project'],['question','Question']].forEach(o=>{const op=el('option',null,o[1]);op.value=o[0];sel.append(op);});
  const add=el('button','btn','Create folder');
  add.onclick=()=>{const n=inp.value.trim();if(!n)return;const f={id:uid(),name:n,kind:sel.value};store.folders.push(f);persist();folderSel=f.id;render();};
  form.append(inp,sel,add);view.append(form);

  [['project','Projects'],['question','Questions']].forEach(g=>{
    const fs=store.folders.filter(f=>f.kind===g[0]);
    if(!fs.length)return;
    view.append(el('div','fgroup',g[1]));
    const chips=el('div','fchips');
    fs.forEach(f=>{
      const b=el('button',f.id===folderSel?'on':'',f.name+' ('+folderCount(f)+')');
      b.onclick=()=>{folderSel=f.id;render();};chips.append(b);
    });
    view.append(chips);
  });
  if(!store.folders.length)view.append(el('p','empty','Make a folder for a project (e.g. "MAPT off-targets") or a question (e.g. "Does tau spread via microglia?"), then use Save on any paper.'));

  const f=folderById(folderSel);
  if(f){
    const tools=el('div','ftools');
    const ren=el('button','btn','Rename');
    ren.onclick=()=>{const n=prompt('Folder name',f.name);if(n&&n.trim()){f.name=n.trim();persist();render();}};
    const del=el('button','btn','Delete folder');
    del.onclick=()=>{
      if(!confirm('Delete "'+f.name+'"? Papers stay saved in any other folders.'))return;
      store.folders=store.folders.filter(x=>x.id!==f.id);
      Object.keys(store.items).forEach(k=>{const it=store.items[k];it.folders=it.folders.filter(x=>x!==f.id);if(!it.folders.length)delete store.items[k];});
      folderSel=null;persist();render();
    };
    tools.append(ren,del);view.append(tools);
    const papers=Object.values(store.items).filter(i=>i.folders.includes(f.id)&&matches(i,q)).sort((a,b)=>b.saved-a.saved);
    if(!papers.length)view.append(el('p','empty','Nothing saved here yet.'));
    papers.forEach(p=>view.append(card(p)));
  }

  const bk=el('div','ftools');
  const exp=el('button','btn','Export backup');
  exp.onclick=()=>{
    const blob=new Blob([JSON.stringify(store,null,1)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='morning-papers-folders.json';a.click();
  };
  const imp=el('button','btn','Import backup');
  const file=document.createElement('input');file.type='file';file.accept='application/json';file.hidden=true;
  file.onchange=()=>{
    const r=new FileReader();
    r.onload=()=>{
      try{
        const o=JSON.parse(r.result);
        (o.folders||[]).forEach(f=>{if(f&&f.id&&f.name&&!folderById(f.id))store.folders.push({id:String(f.id),name:String(f.name),kind:f.kind==='question'?'question':'project'});});
        Object.values(o.items||{}).forEach(i=>{
          if(!i||!i.l)return;
          const cur=store.items[i.l]||(store.items[i.l]={l:String(i.l),t:String(i.t||''),s:String(i.s||''),m:String(i.m||''),saved:Number(i.saved)||Date.now(),folders:[]});
          (i.folders||[]).forEach(id=>{if(folderById(id)&&!cur.folders.includes(id))cur.folders.push(id);});
          if(!cur.folders.length)delete store.items[i.l];
        });
        persist();render();
      }catch(e){alert('That file could not be read.');}
    };
    r.readAsText(file.files[0]);
  };
  imp.onclick=()=>file.click();
  bk.append(exp,imp,file);view.append(bk);
  view.append(el('p','note','Folders are saved in this browser only. Your phone and laptop will not share them; use Export/Import to move them across.'));
}

function render(){
  renderTabs();
  const view=document.getElementById('view');view.replaceChildren();
  const q=document.getElementById('q').value.trim().toLowerCase();
  if(tab==='folders')renderFolders(view,q);else renderPapers(view,q);
}
document.getElementById('q').addEventListener('input',render);
window.addEventListener('hashchange',()=>{const h=location.hash.slice(1);if(TABS.some(t=>t[0]===h)){tab=h;render();}});
render();
})();
</script>
</body></html>
"""


def render(state, now, keep_days):
    today = now.astimezone(TZ).date().isoformat()
    rows = sorted(state["items"].items(),
                  key=lambda kv: (kv[1]["first_seen"], kv[1]["published"]), reverse=True)
    papers = [{
        "l": link, "t": it["title"], "s": it["source"], "m": it["summary"],
        "d": it["first_seen"], "dl": day_label(it["first_seen"], today),
        "y": classify(it["title"], it["source"], it["summary"]),
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
