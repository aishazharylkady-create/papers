#!/usr/bin/env python3
"""Literature search + bibliography builder for the paper-orchestra skill.

No LLM API keys needed. Backends, in order:
  1. Semantic Scholar (uses SEMANTIC_SCHOLAR_API_KEY if set; works without a key
     but is heavily rate-limited)
  2. arXiv API
  3. Crossref API

Subcommands
-----------
search   Run one or more queries, append unique hits to a JSON pool file.
    python lit_search.py search --pool work/lit_pool.json --cutoff 2024-11 \
        --tag related_work "diffusion video editing" "DDIM inversion failure"
    python lit_search.py search --pool work/lit_pool.json --queries-file q.json
        (q.json: [{"query": "...", "tag": "intro"}, ...])

title    Look up one exact paper title (for citation hints such as
         "Vaswani et al. (Attention Is All You Need)").
    python lit_search.py title --pool work/lit_pool.json "Attention Is All You Need"

bib      Build references.bib + citation_map.json from the pool. Only entries
         whose key is listed in --keys (or with "selected": true) are written.
    python lit_search.py bib --pool work/lit_pool.json --keys keys.txt \
        --bib out/references.bib --map work/citation_map.json
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

UA = "paper-orchestra-skill/1.0 (literature search)"
S2_FIELDS = "title,authors,venue,year,abstract,citationCount,externalIds,publicationDate,journal"


def _get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _before_cutoff(year, date, cutoff):
    if not cutoff:
        return True
    cy, cm = map(int, cutoff.split("-"))
    if date:
        try:
            return dt.datetime.strptime(date[:10], "%Y-%m-%d") < dt.datetime(cy, cm, 1)
        except ValueError:
            pass
    if year:
        return int(year) < cy or (int(year) == cy and cm > 1)
    return True


# ---------------------------------------------------------------- backends
STOP = {"a", "an", "the", "of", "for", "and", "on", "in", "to", "with", "via", "is", "are", "towards", "toward", "all", "you", "need", "using", "based", "from", "by", "we", "our"}


def search_s2(query, limit):
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    headers = {"x-api-key": key} if key else {}
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(
        {"query": query, "limit": limit, "fields": S2_FIELDS})
    for attempt in range(3 if key else 2):
        try:
            data = json.loads(_get(url, headers))
            out = []
            for p in data.get("data", []) or []:
                ext = p.get("externalIds") or {}
                out.append({
                    "title": p.get("title") or "",
                    "authors": [a.get("name", "") for a in p.get("authors") or []],
                    "year": p.get("year"),
                    "date": p.get("publicationDate"),
                    "venue": p.get("venue") or (p.get("journal") or {}).get("name") or "",
                    "abstract": p.get("abstract") or "",
                    "citation_count": p.get("citationCount") or 0,
                    "doi": ext.get("DOI"),
                    "arxiv": ext.get("ArXiv"),
                    "source": "semantic_scholar",
                })
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + 3 * attempt)
                continue
            return None
        except Exception:
            return None
    return None


_last_arxiv = [0.0]


def _arxiv_fetch(q, limit):
    wait = 3.1 - (time.time() - _last_arxiv[0])
    if wait > 0:
        time.sleep(wait)
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": q, "max_results": limit, "sortBy": "relevance"})
    try:
        return ET.fromstring(_get(url, timeout=30))
    except Exception:
        return None
    finally:
        _last_arxiv[0] = time.time()


def search_arxiv(query, limit, title=False):
    if title:
        phrase = re.sub(r'["\\]', " ", query).strip()
        attempts = [f'ti:"{phrase}"']
    else:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", query)
                 if len(w) > 1 and w.lower() not in STOP]
        attempts = [" AND ".join(f"all:{w}" for w in words[:n]) for n in (6, 4, 3, 2) if len(words) >= n or n == 2]
        attempts = [a for a in dict.fromkeys(attempts) if a] or [f"all:{query}"]
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out, seen = [], set()
    for q in attempts:
        root = _arxiv_fetch(q, limit)
        if root is None:
            continue
        for e in root.findall("a:entry", ns):
            aid = re.sub(r"v\d+$", "", (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1])
            if aid in seen:
                continue
            seen.add(aid)
            pub = e.findtext("a:published", "", ns) or ""
            out.append({
                "title": re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip(),
                "authors": [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
                "year": int(pub[:4]) if pub[:4].isdigit() else None,
                "date": pub[:10],
                "venue": "arXiv",
                "abstract": re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip(),
                "citation_count": 0,
                "doi": None,
                "arxiv": aid,
                "source": "arxiv",
            })
        if title or len(out) >= limit:
            break
    return out[:limit]


def search_crossref(query, limit, title=False):
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(
        {("query.title" if title else "query.bibliographic"): query, "rows": limit,
         "select": "title,author,issued,container-title,abstract,DOI,is-referenced-by-count,type"})
    try:
        items = json.loads(_get(url, timeout=30))["message"]["items"]
    except Exception:
        return []
    out = []
    for it in items:
        parts = (it.get("issued") or {}).get("date-parts") or [[None]]
        year = parts[0][0] if parts and parts[0] else None
        abstract = re.sub(r"<[^>]+>", "", it.get("abstract") or "").strip()
        out.append({
            "title": (it.get("title") or [""])[0],
            "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip()
                        for a in it.get("author") or []],
            "year": year,
            "date": None,
            "venue": (it.get("container-title") or [""])[0],
            "abstract": abstract,
            "citation_count": it.get("is-referenced-by-count") or 0,
            "doi": it.get("DOI"),
            "arxiv": None,
            "source": "crossref",
        })
    return out


def run_query(query, limit, cutoff, title=False):
    hits = search_s2(query, limit)
    if not hits:
        hits = search_arxiv(query, limit, title)
        if len(hits) < max(2, limit // 2):
            hits += search_crossref(query, limit, title)
    return [h for h in hits if h["title"] and h["authors"]
            and _before_cutoff(h["year"], h["date"], cutoff)]


# ---------------------------------------------------------------- pool
def load_pool(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def save_pool(path, pool):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)




def make_key(entry, taken):
    last = _norm((entry["authors"] or ["anon"])[0].split()[-1]).replace(" ", "") or "anon"
    words = [w for w in _norm(entry["title"]).split() if w not in STOP][:2]
    base = last.capitalize() + str(entry.get("year") or "") + "".join(w.capitalize() for w in words)
    key, i = base, 1
    while key in taken:
        i += 1
        key = f"{base}{chr(96 + i)}"
    return key


def add_to_pool(pool, hits, tag, query):
    by_title = {_norm(p["title"]): p for p in pool}
    taken = {p["key"] for p in pool}
    added = []
    for h in hits:
        nt = _norm(h["title"])
        old = by_title.get(nt)
        if old:
            if tag and tag not in old["tags"]:
                old["tags"].append(tag)
            if not old.get("abstract") and h.get("abstract"):
                old["abstract"] = h["abstract"]
            continue
        h = dict(h, key=make_key(h, taken), tags=[tag] if tag else [], query=query, selected=False)
        taken.add(h["key"])
        by_title[nt] = h
        pool.append(h)
        added.append(h)
    return added


def show(entries):
    for e in entries:
        abst = (e.get("abstract") or "")[:220].replace("\n", " ")
        print(f"- [{e['key']}] {e['title']} ({e.get('year')}, {e.get('venue') or e['source']}, "
              f"cites={e.get('citation_count', 0)})\n    {abst}")


# ---------------------------------------------------------------- bibtex
def _tex_escape(s):
    return (s or "").replace("&", r"\&").replace("%", r"\%").replace("#", r"\#").replace("_", r"\_")


def to_bibtex(e):
    fields = {
        "title": "{" + _tex_escape(e["title"]) + "}",
        "author": " and ".join(_tex_escape(a) for a in e["authors"][:15]) + (" and others" if len(e["authors"]) > 15 else ""),
        "year": str(e.get("year") or ""),
    }
    venue = e.get("venue") or ""
    if e.get("arxiv") and (not venue or venue.lower() == "arxiv"):
        kind = "article"
        fields["journal"] = f"arXiv preprint arXiv:{e['arxiv']}"
    elif re.search(r"conference|proceedings|workshop|symposium|cvpr|iccv|eccv|neurips|nips|icml|iclr|aaai|acl|emnlp|naacl|kdd|sigir|ijcai|wacv|bmvc|interspeech|icassp", venue, re.I):
        kind = "inproceedings"
        fields["booktitle"] = _tex_escape(venue)
    elif venue:
        kind = "article"
        fields["journal"] = _tex_escape(venue)
    else:
        kind = "misc"
    if e.get("doi"):
        fields["doi"] = e["doi"]
    body = ",\n".join(f"  {k} = {{{v}}}" if not v.startswith("{") else f"  {k} = {v}"
                      for k, v in fields.items() if v)
    return f"@{kind}{{{e['key']},\n{body}\n}}\n"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("queries", nargs="*")
    s.add_argument("--queries-file")
    s.add_argument("--pool", required=True)
    s.add_argument("--tag", default="")
    s.add_argument("--cutoff", default="")
    s.add_argument("--limit", type=int, default=8)

    t = sub.add_parser("title")
    t.add_argument("titles", nargs="+")
    t.add_argument("--pool", required=True)
    t.add_argument("--tag", default="method")
    t.add_argument("--cutoff", default="")

    b = sub.add_parser("bib")
    b.add_argument("--pool", required=True)
    b.add_argument("--keys", help="file with one key per line (default: entries marked selected)")
    b.add_argument("--all", action="store_true", help="write every pool entry")
    b.add_argument("--bib", required=True)
    b.add_argument("--map", required=True)

    a = ap.parse_args()
    pool = load_pool(a.pool)

    if a.cmd == "search":
        jobs = [{"query": q, "tag": a.tag} for q in a.queries]
        if a.queries_file:
            with open(a.queries_file) as f:
                jobs += json.load(f)
        for j in jobs:
            hits = run_query(j["query"], a.limit, a.cutoff)
            added = add_to_pool(pool, hits, j.get("tag", a.tag), j["query"])
            print(f"\n## {j['query']}  ->  {len(hits)} hits, {len(added)} new")
            show(added)
            save_pool(a.pool, pool)
            time.sleep(1.0)
        print(f"\nPool size: {len(pool)}  ({a.pool})")

    elif a.cmd == "title":
        for title in a.titles:
            hits = run_query(title, 5, a.cutoff, title=True)
            best = max(hits, key=lambda h: SequenceMatcher(None, _norm(h["title"]), _norm(title)).ratio(), default=None)
            score = SequenceMatcher(None, _norm(best["title"]), _norm(title)).ratio() if best else 0
            if not best or score < 0.75:
                print(f"\n## {title}  ->  NOT FOUND (best match ratio {score:.2f}); do not cite it.")
                continue
            add_to_pool(pool, [best], a.tag, title)
            entry = next(p for p in pool if _norm(p["title"]) == _norm(best["title"]))
            print(f"\n## {title}  ->  match {score:.2f}")
            show([entry])
            save_pool(a.pool, pool)
            time.sleep(1.0)

    elif a.cmd == "bib":
        if a.all:
            chosen = pool
        elif a.keys:
            with open(a.keys) as f:
                wanted = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            idx = {p["key"]: p for p in pool}
            missing = [k for k in wanted if k not in idx]
            if missing:
                print("WARNING: keys not in pool (skipped):", ", ".join(missing), file=sys.stderr)
            chosen = [idx[k] for k in wanted if k in idx]
        else:
            chosen = [p for p in pool if p.get("selected")]
        os.makedirs(os.path.dirname(os.path.abspath(a.bib)), exist_ok=True)
        with open(a.bib, "w") as f:
            f.write("\n".join(to_bibtex(e) for e in chosen))
        cmap = {e["key"]: {"title": e["title"], "authors": e["authors"][:6], "year": e.get("year"),
                           "venue": e.get("venue"), "abstract": e.get("abstract", ""), "tags": e.get("tags", [])}
                for e in chosen}
        os.makedirs(os.path.dirname(os.path.abspath(a.map)), exist_ok=True)
        with open(a.map, "w") as f:
            json.dump(cmap, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(chosen)} entries -> {a.bib}, {a.map}")


if __name__ == "__main__":
    main()
