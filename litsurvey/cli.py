"""Command-line entry point:  python -m litsurvey "your topic" [options]"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import shutil
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .latexutil import build_bib, escape, make_keys
from .llm import LLM
from .models import Paper
from .ranking import cluster, rank
from .sources import SOURCES, Http, collect, scopus_key
from .writer import (Writer, assemble, methodology, name_themes, plan_queries,
                     summarize_papers, theme_tables, year_chart)

log = logging.getLogger("litsurvey")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from .env into the environment (real env vars win), so API keys
    don't have to be typed into every new terminal."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.removeprefix("export ").split("=", 1)
        key, val = key.strip(), val.strip().strip("\"'")
        if key and val and not os.environ.get(key):
            os.environ[key] = val


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:60] or "survey"


def load_papers(path: str) -> tuple[list[Paper], dict, list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    papers = []
    for d in data["papers"]:
        d["sources"] = set(d.get("sources", []))
        papers.append(Paper(**{k: v for k, v in d.items() if k in Paper.__dataclass_fields__}))
    return papers, data.get("stats", {}), data.get("queries", [])


def save_papers(path: Path, papers, stats, queries):
    rows = []
    for p in papers:
        d = asdict(p)
        d["sources"] = sorted(p.sources)
        rows.append(d)
    path.write_text(json.dumps({"queries": queries, "stats": stats, "papers": rows},
                               indent=1, ensure_ascii=False), encoding="utf-8")


def _tex_errors(log_path: Path, n: int = 5) -> list[str]:
    """First few '! ...' error lines from a LaTeX log, with their line numbers."""
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    errs = []
    for i, line in enumerate(lines):
        if line.startswith("!"):
            where = next((l.strip() for l in lines[i + 1:i + 8] if re.match(r"l\.\d+", l)), "")
            errs.append(f"{line} {where[:80]}".strip())
            if len(errs) >= n:
                break
    return errs


def compile_pdf(tex: Path) -> bool:
    """Build <tex>.pdf with pdflatex+bibtex (TeX Live / MiKTeX) or, failing that, tectonic."""
    cwd, stem = tex.parent.resolve(), tex.stem
    pdf = cwd / f"{stem}.pdf"

    def run(cmd):
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=600, stdin=subprocess.DEVNULL)
            return r.returncode
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("%s failed to run: %s", cmd[0], e)
            return -1

    pdflatex, bibtex, tectonic = (shutil.which(x) for x in ("pdflatex", "bibtex", "tectonic"))
    if not (pdflatex and bibtex) and not tectonic:
        log.warning("PDF not built: no LaTeX found on PATH (need pdflatex + bibtex, or tectonic).\n"
                    "  Windows: install MiKTeX (https://miktex.org/download) or TeX Live, then open a "
                    "NEW terminal so PATH updates.\n"
                    "  Linux: sudo apt install texlive-latex-extra texlive-publishers "
                    "texlive-pictures  |  macOS: MacTeX\n"
                    "  Or upload %s to Overleaf (refs.bib is embedded in it).\n"
                    "  Re-run just the PDF step later with:  --pdf-only", tex)
        return False
    if pdf.exists():
        pdf.unlink()  # so a stale PDF is never mistaken for a fresh build
    if pdflatex and bibtex:
        cmd = [pdflatex, "-interaction=nonstopmode", "-file-line-error"]
        ver = subprocess.run([pdflatex, "--version"], capture_output=True, text=True,
                             errors="replace", stdin=subprocess.DEVNULL).stdout
        if "MiKTeX" in ver:  # let MiKTeX fetch IEEEtran/pgfplots without a GUI prompt
            cmd.append("--enable-installer")
        elif shutil.which("kpsewhich") and not subprocess.run(
                ["kpsewhich", "IEEEtran.cls"], capture_output=True, text=True).stdout.strip():
            log.warning("IEEEtran.cls not found; install texlive-publishers (TeX Live) "
                        "or the 'ieeetran' package (tlmgr install ieeetran)")
        log.info("building PDF with pdflatex + bibtex ...")
        run(cmd + [tex.name])
        if run([bibtex, stem]) not in (0, 1):  # 1 = bibtex warnings only
            log.warning("bibtex reported errors; see %s", cwd / f"{stem}.blg")
        run(cmd + [tex.name])
        run(cmd + [tex.name])
    else:
        log.info("building PDF with tectonic ...")
        run([tectonic, "--keep-logs", tex.name])
    errors = _tex_errors(cwd / f"{stem}.log")
    if not pdf.exists():
        log.warning("PDF build failed; see %s", cwd / f"{stem}.log")
        for e in errors:
            log.warning("  %s", e)
        return False
    if errors:
        log.warning("PDF built, but LaTeX reported errors (check those spots in the PDF):")
        for e in errors:
            log.warning("  %s", e)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="litsurvey",
        description="Generate an IEEE-format LaTeX literature survey from open-access papers "
                    "(arXiv, Semantic Scholar, OpenAlex).")
    ap.add_argument("topic", help="survey topic, e.g. \"retrieval-augmented generation\"")
    ap.add_argument("-o", "--out", default=None, help="output directory")
    ap.add_argument("--title", default=None)
    ap.add_argument("--author", default="Author Name")
    ap.add_argument("--sources", default="arxiv,semanticscholar,openalex",
                    help="comma list of: arxiv, semanticscholar, openalex, scopus "
                         "(scopus needs SCOPUS_API_KEY)")
    ap.add_argument("--queries", default=None,
                    help="';'-separated search queries (default: planned automatically)")
    ap.add_argument("--n-queries", type=int, default=6, help="queries to plan with the LLM")
    ap.add_argument("--per-query", type=int, default=60, help="results per query per source")
    ap.add_argument("--max-papers", type=int, default=100,
                    help="papers to include (0 = all relevant ones; no page limit either way)")
    ap.add_argument("--min-relevance", type=float, default=0.08)
    ap.add_argument("--themes", type=int, default=None, help="number of themes (default: auto)")
    ap.add_argument("--year-from", type=int, default=None)
    ap.add_argument("--include-closed", action="store_true",
                    help="also keep works without an open-access copy")
    ap.add_argument("--llm", default="none", choices=["none", "anthropic", "openai", "ollama"],
                    help="writing backend (none = offline extractive draft)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint URL")
    ap.add_argument("--critic", action="store_true",
                    help="extra LLM pass that checks every section against the abstracts")
    ap.add_argument("--from-json", default=None, help="reuse papers.json from a previous run")
    ap.add_argument("--no-embed-bib", action="store_true",
                    help="don't embed refs.bib inside the .tex (a separate refs.bib is always written)")
    ap.add_argument("--pdf", action="store_true",
                    help="compile with pdflatex+bibtex (or tectonic) if available")
    ap.add_argument("--pdf-only", action="store_true",
                    help="only (re)build the PDF from an existing <out>/survey.tex and exit")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    load_dotenv()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    out = Path(a.out or f"survey_{slug(a.topic)}")
    if a.pdf_only:
        tex_path = out / "survey.tex"
        if not tex_path.exists():
            raise SystemExit(f"{tex_path} not found; run without --pdf-only first.")
        if not compile_pdf(tex_path):
            raise SystemExit(1)
        log.info("wrote %s", out / "survey.pdf")
        return
    sources = [s.strip().lower() for s in a.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        ap.error(f"unknown source(s) {unknown}; choose from {', '.join(SOURCES)}")
    if "scopus" in sources and not scopus_key():
        ap.error("--sources includes scopus but SCOPUS_API_KEY is not set "
                 "(get a key at https://dev.elsevier.com)")
    out.mkdir(parents=True, exist_ok=True)
    llm = LLM(a.llm, model=a.model, base_url=a.base_url)
    if llm.enabled:
        log.info("checking %s access (model %s) ...", a.llm, llm.model)
        llm.check()
    open_only = not a.include_closed

    # 1. plan + search
    if a.from_json:
        papers, stats, queries = load_papers(a.from_json)
        log.info("loaded %d papers from %s", len(papers), a.from_json)
    else:
        queries = ([q.strip() for q in a.queries.split(";") if q.strip()] if a.queries
                   else plan_queries(llm, a.topic, a.n_queries))
        log.info("queries: %s", queries)
        http = Http(None if a.no_cache else str(out / ".cache"))
        papers, stats = collect(queries, sources, a.per_query, http, a.year_from, open_only)
        save_papers(out / "papers_all.json", papers, stats, queries)
    log.info("%d unique candidate papers", len(papers))
    if not papers:
        raise SystemExit("No papers found. Check network access or broaden the topic.")

    # 2. rank + select + cluster
    papers = rank(papers, a.topic, queries, a.max_papers, a.min_relevance)
    if len(papers) < 3:
        raise SystemExit(f"Only {len(papers)} relevant papers; lower --min-relevance or "
                         "broaden the topic/queries.")
    make_keys(papers)
    themes = cluster(papers, a.themes)
    log.info("selected %d papers in %d themes", len(papers), len(themes))

    # 3. write
    theme_names = name_themes(llm, a.topic, themes, papers)
    summarize_papers(llm, papers)
    w = Writer(llm, a.topic, papers, theme_names, critic=a.critic)
    chart, trend = year_chart(papers)
    sections = {}
    log.info("writing abstract/introduction/background ...")
    sections["abstract"] = w.abstract()
    sections["keywords"] = w.keywords()
    sections["intro"] = w.introduction()
    sections["method"] = methodology(a.topic, queries, sources, stats, len(papers), papers,
                                     a.year_from, a.max_papers, open_only)
    sections["background"] = w.background()
    blocks = []
    for t, _ in themes:
        log.info("writing theme %d/%d: %s", t + 1, len(themes), theme_names[t])
        blocks.append(f"\\section{{{escape(theme_names[t])}}}\\label{{sec:theme{t}}}\n"
                      f"{theme_tables(t, theme_names[t], papers)}\n\n{w.theme_section(t)}\n")
    log.info("writing discussion/challenges/conclusion ...")
    sections["chart"] = chart
    sections["discussion"] = w.discussion(trend)
    sections["challenges"] = w.challenges()
    sections["conclusion"] = w.conclusion()

    # 4. output
    title = a.title or f"A Survey on {a.topic[:1].upper() + a.topic[1:]}"
    bib = build_bib(papers)
    (out / "refs.bib").write_text(bib, encoding="utf-8")
    tex_path = out / "survey.tex"
    tex = assemble(title, a.author, sections, blocks, bib, embed_bib=not a.no_embed_bib)
    tex_path.write_text(tex, encoding="utf-8")
    save_papers(out / "papers.json", papers, stats, queries)

    cited = set(re.findall(r"\\cite\{([^}]*)\}", tex.split("\\begin{document}")[-1]))
    cited = {k.strip() for c in cited for k in c.split(",")}
    report = {
        "topic": a.topic, "queries": queries, "stats": stats, "papers_included": len(papers),
        "themes": {theme_names[t]: sum(p.theme == t for p in papers) for t, _ in themes},
        "uncited_papers": sorted({p.key for p in papers} - cited),
        "hallucinated_citations_removed": sorted(set(w.removed_keys)),
        "llm": f"{a.llm}:{llm.model}" if llm.enabled else "none (extractive draft)",
    }
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("wrote %s and %s", tex_path, out / "refs.bib")
    if a.pdf and compile_pdf(tex_path):
        log.info("wrote %s", out / "survey.pdf")
    print(f"\nDone: {tex_path}  ({len(papers)} papers, {len(themes)} themes)")


if __name__ == "__main__":
    main()
