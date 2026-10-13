"""Paper search backends: arXiv, Semantic Scholar, OpenAlex (all free/open APIs)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus, urlencode, urlparse

import requests

from .models import Paper, clean_arxiv_id, clean_doi

log = logging.getLogger("litsurvey")

UA = "litsurvey/1.0 (academic literature survey tool)"


class Http:
    """requests wrapper with on-disk cache, per-host politeness delay, retry that honours
    Retry-After, and a per-host circuit breaker so one rate-limited source can't stall a run."""

    MIN_INTERVAL = {  # seconds between requests per host
        "export.arxiv.org": 3.5,
        "api.semanticscholar.org": 1.5,
        "api.openalex.org": 0.2,
    }
    BACKOFF = {"export.arxiv.org": 10.0, "api.semanticscholar.org": 5.0}  # first back-off (s)
    TIMEOUT = {"export.arxiv.org": 90}
    MAX_WAIT = 120          # never sleep longer than this on a single back-off
    MAX_FAILURES = 2        # give-ups per host before that source is skipped for the run

    def __init__(self, cache_dir: str | None = ".litsurvey_cache", timeout: int = 45):
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._last: dict[str, float] = {}
        self.failures: dict[str, int] = {}

    def disabled(self, host: str) -> bool:
        return self.failures.get(host, 0) >= self.MAX_FAILURES

    def get(self, url: str, headers: dict | None = None, as_json: bool = True, retries: int = 5):
        cpath = None
        if self.cache:
            cpath = self.cache / (hashlib.sha1(url.encode()).hexdigest() + ".cache")
            if cpath.exists():
                raw = cpath.read_text(encoding="utf-8")
                return json.loads(raw) if as_json else raw
        host = urlparse(url).netloc
        if self.disabled(host):
            return None
        delay = self.BACKOFF.get(host, 2.0)
        for attempt in range(retries):
            wait = self.MIN_INTERVAL.get(host, 0.5) - (time.time() - self._last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()
            try:
                r = self.session.get(url, headers=headers or {},
                                     timeout=self.TIMEOUT.get(host, self.timeout))
            except requests.RequestException as e:
                log.warning("%s: %s (retry in %.0fs)", host, type(e).__name__, delay)
                time.sleep(delay)
                delay = min(delay * 2, self.MAX_WAIT)
                continue
            if r.status_code == 200:
                self.failures[host] = 0
                if cpath:
                    cpath.write_text(r.text, encoding="utf-8")
                return r.json() if as_json else r.text
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After", "")
                pause = min(float(ra), self.MAX_WAIT) if ra.replace(".", "").isdigit() else delay
                log.info("%s returned %s (rate limit/busy), waiting %.0fs", host, r.status_code, pause)
                time.sleep(pause)
                delay = min(delay * 2, self.MAX_WAIT)
                continue
            log.warning("%s returned HTTP %s for %s", host, r.status_code, url[:120])
            return None
        self.failures[host] = self.failures.get(host, 0) + 1
        if self.disabled(host):
            log.warning("%s keeps refusing requests; skipping it for the rest of this run "
                        "(results from the other sources are still used)", host)
        else:
            log.warning("giving up on this %s request", host)
        return None


# --------------------------------------------------------------------- arXiv
ATOM = "{http://www.w3.org/2005/Atom}"
ARX = "{http://arxiv.org/schemas/atom}"


_QSTOP = {"a", "an", "the", "of", "for", "in", "on", "and", "or", "to", "with", "using", "use",
          "usage", "uses", "based", "via", "by", "from", "at", "its", "their", "study", "studies",
          "application", "applications", "approach", "approaches", "method", "methods"}


def _arxiv_query(q: str) -> str:
    """Content words AND-ed over all fields; quoted phrases stay phrases; stop-words dropped
    (arXiv matches them literally, which both narrows results and slows the query)."""
    import shlex
    try:
        parts = shlex.split(q.replace("'", " "))
    except ValueError:
        parts = q.replace('"', " ").split()
    terms = []
    for part in parts:
        part = re.sub(r"[^\w\s-]", " ", part).strip()
        if not part:
            continue
        if " " in part:
            terms.append(f'all:"{part}"')
        elif part.lower() not in _QSTOP:
            terms.append(f"all:{part}")
    terms = terms[:6]
    return " AND ".join(terms) if terms else f'all:"{q}"'


def parse_arxiv_feed(xml_text: str) -> list[Paper]:
    papers = []
    root = ET.fromstring(xml_text)
    for e in root.findall(f"{ATOM}entry"):
        title = " ".join((e.findtext(f"{ATOM}title") or "").split())
        if not title or title.lower() == "error":
            continue
        aid = clean_arxiv_id(e.findtext(f"{ATOM}id") or "")
        pub = e.findtext(f"{ATOM}published") or ""
        journal_ref = (e.findtext(f"{ARX}journal_ref") or "").strip()
        doi = clean_doi(e.findtext(f"{ARX}doi") or "")
        pdf = ""
        for link in e.findall(f"{ATOM}link"):
            if link.get("title") == "pdf":
                pdf = link.get("href", "")
        p = Paper(
            title=title,
            authors=[" ".join((a.findtext(f"{ATOM}name") or "").split())
                     for a in e.findall(f"{ATOM}author")],
            year=int(pub[:4]) if pub[:4].isdigit() else None,
            abstract=" ".join((e.findtext(f"{ATOM}summary") or "").split()),
            arxiv_id=aid,
            doi=doi,
            url=f"https://arxiv.org/abs/{aid}",
            pdf_url=pdf or f"https://arxiv.org/pdf/{aid}",
            venue="arXiv",
            venue_type="preprint",
            sources={"arxiv"},
        )
        if journal_ref:
            p.note = journal_ref
        papers.append(p)
    return papers


def search_arxiv(http: Http, query: str, limit: int = 50) -> list[Paper]:
    out: list[Paper] = []
    page = 100
    for start in range(0, limit, page):
        params = {
            "search_query": _arxiv_query(query),
            "start": start,
            "max_results": min(page, limit - start),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        url = "https://export.arxiv.org/api/query?" + urlencode(params, quote_via=quote_plus)
        txt = http.get(url, as_json=False)
        if not txt:
            break
        try:
            batch = parse_arxiv_feed(txt)
        except ET.ParseError as e:
            log.warning("arXiv parse error: %s", e)
            break
        out.extend(batch)
        if len(batch) < params["max_results"]:
            break
    return out


# ---------------------------------------------------------- Semantic Scholar
S2_FIELDS = ("title,abstract,authors,year,venue,externalIds,citationCount,"
             "openAccessPdf,isOpenAccess,url,publicationTypes,journal,publicationVenue")


def parse_s2_item(it: dict) -> Paper | None:
    if not it or not it.get("title"):
        return None
    ext = it.get("externalIds") or {}
    types = [t.lower() for t in (it.get("publicationTypes") or [])]
    journal = it.get("journal") or {}
    pv = it.get("publicationVenue") or {}
    venue = journal.get("name") or pv.get("name") or it.get("venue") or ""
    vtype = "other"
    pvt = (pv.get("type") or "").lower()
    if "conference" in types or pvt == "conference":
        vtype = "conference"
    elif "journalarticle" in types or pvt == "journal" or journal.get("name"):
        vtype = "journal"
    arxiv_id = clean_arxiv_id(ext.get("ArXiv") or "")
    if venue.lower() in ("arxiv", "arxiv.org") or (not venue and arxiv_id):
        venue, vtype = "arXiv", "preprint"
    oap = (it.get("openAccessPdf") or {}).get("url") or ""
    return Paper(
        title=" ".join(it["title"].split()),
        authors=[a.get("name", "") for a in (it.get("authors") or []) if a.get("name")],
        year=it.get("year"),
        abstract=" ".join((it.get("abstract") or "").split()),
        venue=venue,
        venue_type=vtype,
        doi=clean_doi(ext.get("DOI") or ""),
        arxiv_id=arxiv_id,
        url=it.get("url") or "",
        pdf_url=oap,
        volume=str(journal.get("volume") or "").strip(),
        pages=str(journal.get("pages") or "").strip().replace(" ", ""),
        citations=it.get("citationCount") or 0,
        is_open=bool(oap or arxiv_id or it.get("isOpenAccess")),
        sources={"semanticscholar"},
    )


def search_semantic_scholar(http: Http, query: str, limit: int = 50,
                            year_from: int | None = None) -> list[Paper]:
    headers = {}
    if os.getenv("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    out: list[Paper] = []
    for offset in range(0, limit, 100):
        params = {"query": query, "limit": min(100, limit - offset), "offset": offset,
                  "fields": S2_FIELDS}
        if year_from:
            params["year"] = f"{year_from}-"
        url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urlencode(params)
        data = http.get(url, headers=headers)
        if not data or not data.get("data"):
            break
        out.extend(p for p in map(parse_s2_item, data["data"]) if p)
        if data.get("next") is None:
            break
    return out


# ------------------------------------------------------------------ OpenAlex
def _undo_inverted_index(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def parse_openalex_item(w: dict) -> Paper | None:
    title = w.get("display_name") or w.get("title")
    if not title:
        return None
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    stype = (src.get("type") or "").lower()
    venue = src.get("display_name") or ""
    vtype = {"journal": "journal", "conference": "conference",
             "repository": "preprint"}.get(stype, "other")
    ids = w.get("ids") or {}
    arxiv_id = ""
    for l in [loc] + (w.get("locations") or []):
        lu = (l or {}).get("landing_page_url") or ""
        if "arxiv.org/abs/" in lu:
            arxiv_id = clean_arxiv_id(lu.split("arxiv.org/abs/")[-1])
            break
    if vtype == "preprint" and "arxiv" in venue.lower():
        venue = "arXiv"
    b = w.get("biblio") or {}
    pages = ""
    if b.get("first_page"):
        pages = str(b["first_page"])
        if b.get("last_page") and b["last_page"] != b["first_page"]:
            pages += "--" + str(b["last_page"])
    oa = w.get("open_access") or {}
    return Paper(
        title=" ".join(title.split()),
        authors=[(a.get("author") or {}).get("display_name", "")
                 for a in (w.get("authorships") or []) if (a.get("author") or {}).get("display_name")],
        year=w.get("publication_year"),
        abstract=_undo_inverted_index(w.get("abstract_inverted_index")),
        venue=venue,
        venue_type=vtype,
        doi=clean_doi(w.get("doi") or ids.get("doi") or ""),
        arxiv_id=arxiv_id,
        url=w.get("doi") or w.get("id") or "",
        pdf_url=oa.get("oa_url") or "",
        volume=str(b.get("volume") or ""),
        issue=str(b.get("issue") or ""),
        pages=pages,
        publisher=src.get("host_organization_name") or "",
        citations=w.get("cited_by_count") or 0,
        is_open=bool(oa.get("is_oa")),
        sources={"openalex"},
    )


def search_openalex(http: Http, query: str, limit: int = 50, year_from: int | None = None,
                    open_only: bool = True) -> list[Paper]:
    filters = ["type:article|preprint|proceedings-article|review"]
    if open_only:
        filters.append("open_access.is_oa:true")
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    out: list[Paper] = []
    page = 1
    while len(out) < limit:
        params = {"search": query, "filter": ",".join(filters),
                  "per-page": min(100, limit - len(out)), "page": page}
        if os.getenv("OPENALEX_EMAIL"):
            params["mailto"] = os.environ["OPENALEX_EMAIL"]
        if os.getenv("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        url = "https://api.openalex.org/works?" + urlencode(params)
        data = http.get(url)
        if not data or not data.get("results"):
            break
        out.extend(p for p in map(parse_openalex_item, data["results"]) if p)
        if len(data["results"]) < params["per-page"]:
            break
        page += 1
    return out


# ------------------------------------------------------------- aggregation
def deduplicate(papers: list[Paper]) -> list[Paper]:
    """Merge records of the same work found via different sources/queries."""
    merged: list[Paper] = []
    by_doi, by_arx, by_title = {}, {}, {}
    for p in papers:
        if len(p.norm_title) < 8:
            continue
        hit = ((p.doi and by_doi.get(p.doi)) or (p.arxiv_id and by_arx.get(p.arxiv_id))
               or by_title.get(p.norm_title))
        if hit is not None:
            hit.merge(p)
            target = hit
        else:
            merged.append(p)
            target = p
        if target.doi:
            by_doi[target.doi] = target
        if target.arxiv_id:
            by_arx[target.arxiv_id] = target
        by_title[target.norm_title] = target
    return merged


SOURCE_HOSTS = {"arxiv": "export.arxiv.org",
                "semanticscholar": "api.semanticscholar.org",
                "openalex": "api.openalex.org"}

SOURCES = {
    "arxiv": lambda http, q, n, yf, oo: search_arxiv(http, q, n),
    "semanticscholar": lambda http, q, n, yf, oo: search_semantic_scholar(http, q, n, yf),
    "openalex": lambda http, q, n, yf, oo: search_openalex(http, q, n, yf, oo),
}


def collect(queries: list[str], sources: list[str], per_query: int, http: Http,
            year_from: int | None = None, open_only: bool = True) -> tuple[list[Paper], dict]:
    raw: list[Paper] = []
    stats = {s: 0 for s in sources}
    for q in queries:
        for s in sources:
            if http.disabled(SOURCE_HOSTS[s]):
                continue
            log.info("searching %-16s | %s", s, q)
            try:
                got = SOURCES[s](http, q, per_query, year_from, open_only)
            except Exception as e:  # never let one backend kill the run
                log.warning("%s failed for %r: %s", s, q, e)
                got = []
            stats[s] += len(got)
            raw.extend(got)
    stats["total_retrieved"] = len(raw)
    papers = deduplicate(raw)
    stats["after_dedup"] = len(papers)
    if year_from:
        papers = [p for p in papers if not p.year or p.year >= year_from]
    if open_only:
        papers = [p for p in papers if p.is_open]
    stats["after_open_filter"] = len(papers)
    return papers, stats
