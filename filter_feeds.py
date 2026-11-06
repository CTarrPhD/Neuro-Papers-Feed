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
    r"microglia", r"astrocyt", r"oligodendrocyt", r"\bglia", r"synap(?!sis|tonemal)",
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

# Neurodegeneration drugs and drug classes (papers often name only the drug). Count both as
# neurodegeneration terms and as therapy terms.
ND_DRUG_STEMS = [
    r"lecanemab", r"donanemab", r"aducanumab", r"gantenerumab", r"remternetug", r"trontinemab",
    r"solanezumab", r"bepranemab", r"posdinemab", r"tofersen", r"ulefnersen", r"prasinezumab",
    r"latozinemab", r"anti-amyloid", r"anti-tau", r"tau antibod", r"tau-targeting", r"amyloid-lowering",
]
ND_DRUG_ACRONYMS = ["BIIB080", "BIIB122", "DNL151", "AL002", "VG-3927"]

# Neurodegeneration only. Used for broad, high-volume journals (Nature Communications,
# Cell Reports, Nature Medicine, Nature Genetics...) so only disease-relevant papers get through.
NEURODEGEN_STEMS = [
    r"neurodegenerat", r"alzheimer", r"parkinson", r"dementia", r"huntington", r"frontotemporal",
    r"amyotrophic", r"motor neuron disease", r"tauopath", r"\btau\b", r"amyloid", r"synucle",
    r"prion", r"lewy bod", r"supranuclear palsy", r"corticobasal", r"multiple system atrophy",
    r"spinocerebellar ataxia", r"neuroinflamm", r"microglia", r"reactive astrocyt",
    r"cognitive (?:decline|impairment)", r"neuronal (?:loss|death|vulnerability)",
    r"brain (?:aging|ageing)", r"(?:aging|ageing) brain", r"protein aggregat", r"progranulin",
    # mechanisms that rarely name a disease
    r"axon(?:al)? degenerat", r"axonal transport", r"synap(?:se|tic) loss", r"stress granule",
    r"nucleocytoplasmic transport", r"amyloid precursor protein", r"neurofibrillary", r"tangle-bearing",
    # iPSC-derived brain cells
    r"\bi(?:neuron|microglia|astrocyte)s?\b", r"ipsc-derived (?:neuron|microglia|astrocyte|glia)",
] + ND_DRUG_STEMS
NEURODEGEN_ACRONYMS = [
    "MAPT", "ALS", "FTD", "FTLD", "APOE", "APOE4", "TREM2", "LRRK2", "GBA1", "C9orf72",
    "TDP-43", "SOD1", "PSEN1", "PSEN2", "MCI",
    # Alzheimer's risk genes that are specific enough on their own
    "SORL1", "BIN1", "PICALM", "ABCA7",
] + ND_DRUG_ACRONYMS

# Terms that only count when the paper is also about the nervous system, because on their
# own they pull in cancer, immunology, etc. (case-sensitive acronyms vs case-insensitive stems)
NEURO_CONTEXT = [
    r"neuro", r"neuron", r"brain", r"\bglia", r"microglia", r"astrocyt", r"oligodendrocyt",
    r"synap(?!sis|tonemal)", r"cortex", r"cortical", r"hippocamp", r"cerebr", r"cerebell", r"spinal cord",
    r"\bCNS\b", r"alzheimer", r"parkinson", r"dementia",
]
NEURODEGEN_CONDITIONAL_STEMS = [
    r"autophag", r"lysosom", r"endolysosom", r"endosom", r"proteostasis", r"mitochondrial dysfunction",
    r"mitophagy", r"RNA-binding protein", r"protein misfolding",
]
NEURODEGEN_CONDITIONAL_ACRONYMS = ["APP", "CD33", "PLCG2", "RBP", "RBPs"]

# Genetics: the core of what you want to see. Counted when the paper is also about the
# nervous system (or names a neurodegeneration term).
GENETICS_STEMS = [
    r"gwas", r"genome-wide association", r"genome-wide", r"risk (?:locus|loci|variant|allele|gene)",
    r"fine-mapp", r"eqtl", r"sqtl", r"caqtl", r"\bqtls?\b", r"polygenic", r"heritab",
    r"rare (?:coding )?variant", r"common variant", r"genetic (?:variant|variation|architecture|risk|modifier|screen|ancestry|association)",
    r"whole[- ](?:genome|exome) sequencing", r"exome", r"haplotype", r"allele-specific", r"mendelian randomi",
    r"crispr(?:i|a)?\b", r"crispr (?:screen|interference|activation|knockout)", r"perturb-seq", r"crop-seq",
    r"base edit", r"prime edit", r"knock-?in", r"isogenic", r"mpra", r"massively parallel reporter",
    r"enhancer", r"cis-regulat", r"regulatory (?:element|variant|region)", r"chromatin", r"atac-seq",
    r"epigen", r"dna methylation", r"histone", r"splicing", r"splice", r"isoform", r"exon",
    r"gene regulat", r"transcriptional regulat", r"variant effect", r"pathogenic variant",
    r"mutation", r"repeat expansion", r"structural variant", r"copy number", r"somatic mosaic",
    r"single-(?:cell|nucleus) (?:rna|multiome|atac)", r"snrna-seq", r"scrna-seq", r"multiome",
    r"transcriptom", r"genotyp",
    # genome biology of individual cells (somatic mutation, mosaicism, genome integrity)
    r"\bgenetic", r"genom", r"\bmutat", r"aneuploid", r"mosaicism", r"somatic (?:variant|cnv|copy|retrotranspos|snv)",
    r"single-(?:cell|neuron|nucleus) (?:sequencing|genom|dna|whole)", r"genome (?:integrity|stability|instability|maintenance)",
    r"dna damage", r"dna repair", r"sequencing",
]
# Translational / therapeutic work. Used together with a neurodegeneration term to flag
# papers on new drugs, gene therapy and delivery.
THERAPY_STEMS = [
    r"gene therap", r"\baav", r"adeno-associated", r"antisense", r"oligonucleotide", r"sirna",
    r"rnai therap", r"small[- ]molecule", r"drug (?:candidate|discovery|development|repurpos|target|delivery)",
    r"therapeutic", r"therap(?:y|ies)\b", r"(?:drug|novel|new|potential|disease-modifying) treatment", r"treatments\b",
    r"(?-i:\b[A-Z]{2,5}-?\d{3,7}\b)",  # coded compounds like BIIB080, VU0155069 r"clinical trial", r"\bphase (?:1|2|3|i{1,3})\b",
    r"first-in-human", r"intrathecal", r"blood-brain barrier", r"brain shuttle", r"transferrin receptor",
    r"in vivo (?:gene |genome |base |prime )?editing", r"cell (?:therapy|replacement)", r"immunotherap",
    r"vaccin", r"(?:therapeutic|monoclonal|passive|active) (?:antibod|immuni[sz]ation)", r"antibody (?:therap|treatment)", r"agonist", r"antagonist", r"inhibitor", r"degrader", r"protac",
    r"target engagement", r"preclinical", r"druggable", r"pharmacolog", r"neuroprotect",
    r"disease-modifying", r"lowering", r"knockdown", r"silencing",
] + ND_DRUG_STEMS
THERAPY_ACRONYMS = ["ASO", "ASOs", "AAV", "AAV9", "BBB", "TfR", "TfR1", "mAb", "PROTAC"] + ND_DRUG_ACRONYMS

GENETICS_ACRONYMS = ["GWAS", "eQTL", "sQTL", "QTL", "MPRA", "WGS", "WES", "SNP", "SNPs", "CNV", "CNVs", "ASO", "ASOs"]

# Any neuroscience, without the iPSC/CRISPR-only terms. Used for the flagship journals
# (Nature, Science, Cell) and methods journals so big neuroscience findings still show.
NEUROSCIENCE_STEMS = [s for s in NEURO_STEMS if s not in (r"induced pluripotent", r"organoid")] \
    + [r"brain organoid", r"neural organoid", r"cerebral organoid"] + NEURODEGEN_STEMS
NEUROSCIENCE_ACRONYMS = [a for a in NEURO_ACRONYMS if "PSC" not in a] + NEURODEGEN_ACRONYMS

# AI and machine-learning papers worth knowing about for biology research: models for
# proteins, DNA/RNA, cells and genomes, AI for scientific discovery, plus any neurodegeneration.
AI_RESEARCH_STEMS = [
    r"protein", r"genom", r"\bDNA\b", r"\bRNA\b", r"single-cell", r"single-nucle", r"transcriptom",
    r"gene expression", r"gene regulat", r"genetic perturbation", r"perturb-seq", r"cell type", r"biomedic",
    r"molecular biology", r"biological sequence", r"drug discovery", r"connectom",
    r"scientific discovery", r"ai scientist", r"scientific literature", r"scientific research",
    r"research agent", r"lab automation", r"laboratory", r"microscopy", r"variant effect",
] + NEURODEGEN_STEMS

def _either(stems, acronyms):
    """One regex: stems case-insensitive, acronyms case-sensitive whole words."""
    parts = []
    if stems:
        parts.append("(?i:" + "|".join(stems) + ")")
    if acronyms:
        parts.append(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(a) for a in acronyms) + r")(?![A-Za-z0-9])")
    return "|".join(parts)


# "Both" rules: a paper also matches when it hits BOTH regexes of a pair.
NEURO_CONTEXT_RE = _either(NEURO_CONTEXT, ["ALS", "FTD"])
NEURODEGEN_PAIRS = [(_either(NEURODEGEN_CONDITIONAL_STEMS, NEURODEGEN_CONDITIONAL_ACRONYMS), NEURO_CONTEXT_RE)]
GENETICS_RE = _either(GENETICS_STEMS, GENETICS_ACRONYMS)
THERAPY_RE = _either(THERAPY_STEMS, THERAPY_ACRONYMS)
NEUROGENETICS_PAIRS = NEURODEGEN_PAIRS + [(GENETICS_RE, NEURO_CONTEXT_RE)]

PROFILES = {
    # name: (stems, acronyms[, pairs])
    "neuro":        (NEURO_STEMS + EXTRA_STEMS, NEURO_ACRONYMS),
    "genomics":     (NEURO_STEMS + EXTRA_STEMS + GENOMICS_STEMS, NEURO_ACRONYMS),
    "ai":           (NEURO_STEMS + EXTRA_STEMS + AI_BIO_STEMS, NEURO_ACRONYMS),
    "neurodegen":   (NEURODEGEN_STEMS, NEURODEGEN_ACRONYMS, NEURODEGEN_PAIRS),
    # neurodegeneration, OR genetics of the nervous system (GWAS, eQTL, CRISPR screens, splicing...)
    "neurogenetics": (NEURODEGEN_STEMS, NEURODEGEN_ACRONYMS, NEUROGENETICS_PAIRS),
    "neuroscience": (NEUROSCIENCE_STEMS, NEUROSCIENCE_ACRONYMS),
    "ai_research":  (AI_RESEARCH_STEMS, [a for a in NEURODEGEN_ACRONYMS if a not in ("ALS", "MCI")]),
}

# --------------------------------------------------------------------------
# Feeds to filter: (display name, URL, profile)
# URLs marked "untested" were not reachable for checking when this was built.
# --------------------------------------------------------------------------
FEEDS = [
    ("Nature", "https://www.nature.com/nature.rss", "neuroscience"),
    ("Nature Communications", "https://www.nature.com/ncomms.rss", "neurogenetics"),
    ("Scientific Reports (Neuroscience)", "https://www.nature.com/subjects/neuroscience/srep.rss", "neurodegen"),
    ("eLife", "https://elifesciences.org/rss/recent.xml", "neuro"),
    ("bioRxiv Genomics", "https://connect.biorxiv.org/biorxiv_xml.php?subject=genomics", "neurogenetics"),
    ("arXiv q-bio.GN", "https://rss.arxiv.org/rss/q-bio.GN", "neurogenetics"),
    ("arXiv q-bio.QM", "https://rss.arxiv.org/rss/q-bio.QM", "genomics"),
    ("arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG", "ai_research"),
    # untested URLs:
    ("Science", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science", "neuroscience"),
    ("Science Translational Medicine", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=stm", "neurodegen"),
    ("Cell", "https://www.cell.com/cell/current.rss", "neuroscience"),
    ("Cell Reports", "https://www.cell.com/cell-reports/current.rss", "neurogenetics"),
    ("bioRxiv Genetics", "https://connect.biorxiv.org/biorxiv_xml.php?subject=genetics", "neurogenetics"),
    ("bioRxiv Cell Biology", "https://connect.biorxiv.org/biorxiv_xml.php?subject=cell_biology", "neuro"),
    ("bioRxiv Molecular Biology", "https://connect.biorxiv.org/biorxiv_xml.php?subject=molecular_biology", "neuro"),
    ("bioRxiv Bioinformatics", "https://connect.biorxiv.org/biorxiv_xml.php?subject=bioinformatics", "genomics"),
    ("arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI", "ai_research"),
    ("arXiv stat.ML", "https://rss.arxiv.org/rss/stat.ML", "ai_research"),
    # Genetics journals, kept to neurodegeneration + nervous-system genetics (untested URLs;
    # build_digest.py falls back to Crossref if they fail)
    ("American Journal of Human Genetics", "https://www.cell.com/ajhg/current.rss", "neurogenetics"),
    ("Cell Genomics", "https://www.cell.com/cell-genomics/current.rss", "neurogenetics"),
    ("Genome Research", "https://genome.cshlp.org/rss/current.xml", "neurogenetics"),
    ("Genome Biology", "https://genomebiology.biomedcentral.com/articles/most-recent/rss.xml", "neurogenetics"),
]


def compile_profile(name):
    """Returns (stem_re, acr_re, pairs). Pass all three to matches()."""
    stems, acronyms, *rest = PROFILES[name]
    stem_re = re.compile("|".join(stems), re.IGNORECASE)
    acr_re = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(a) for a in acronyms) + r")(?![A-Za-z0-9])")
    pairs = [(re.compile(a), re.compile(b)) for a, b in (rest[0] if rest else [])]
    return stem_re, acr_re, pairs


def norm_dashes(text):
    """Publishers often use typographic hyphens (p\u2010tau, TDP\u201043); make them plain so keywords match."""
    return re.sub("[\u2010\u2011\u2012\u2212]", "-", text or "")


def clean(text):
    return norm_dashes(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip())


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc)
    return None


def matches(entry, stem_re, acr_re, pairs=()):
    text = clean(entry.get("title", "")) + " " + clean(entry.get("summary", ""))
    if stem_re.search(text) or acr_re.search(text):
        return True
    return any(a.search(text) and b.search(text) for a, b in pairs)


def collect(feeds, days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    seen, items, report = set(), [], []
    compiled = {}
    for name, url, profile in feeds:
        if profile not in compiled:
            compiled[profile] = compile_profile(profile)
        rules = compiled[profile]
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
            if when < cutoff or not matches(e, *rules):
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
