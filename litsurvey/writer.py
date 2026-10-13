"""Survey composition: query planning, theme naming, section writing (LLM or offline),
and assembly of the IEEEtran document."""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date

from .latexutil import escape, sanitize_llm_latex, validate_citations
from .llm import LLM
from .models import Paper

log = logging.getLogger("litsurvey")

SYSTEM = (
    "You are an expert academic writer producing an IEEE-style literature survey in LaTeX. "
    "You write precise, technical, well-organised prose. You ONLY make claims about specific "
    "works that are supported by the metadata/abstracts you are given, and you cite them with "
    "\\cite{key} using ONLY the keys provided. Never invent papers, keys, numbers or results. "
    "Output LaTeX body text only: no preamble, no \\section commands, no markdown, no code fences. "
    "Escape LaTeX special characters (&, %, #, _) in prose."
)


# ------------------------------------------------------------------ helpers
def author_label(p: Paper) -> str:
    if not p.authors:
        return "The authors"
    last = lambda a: a.split()[-1] if a.split() else a
    if len(p.authors) == 1:
        return escape(last(p.authors[0]))
    if len(p.authors) == 2:
        return f"{escape(last(p.authors[0]))} and {escape(last(p.authors[1]))}"
    return f"{escape(last(p.authors[0]))} \\textit{{et al.}}"


_ABBR = r"(e\.g|i\.e|et al|etc|vs|Fig|Eq|Ref|approx|cf|resp|No|Sec)\."


def split_sentences(text: str) -> list[str]:
    protected = re.sub(_ABBR, lambda m: m.group(0).replace(".", "<DOT>"), text.strip())
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", protected)
    return [s.replace("<DOT>", ".") for s in sents if s.strip()]


def first_sentences(text: str, n: int = 1, max_words: int = 45) -> str:
    """First n sentences of plain text, truncated and LaTeX-escaped."""
    sents = split_sentences(text)
    out = " ".join(sents[:n]).split()
    s = " ".join(out[:max_words])
    if len(out) > max_words:
        return escape(s.rstrip(",;:")) + "\\ldots"
    return escape(s)


def voiced_sentence(p: Paper, max_words: int = 60) -> str:
    """'Chen et al. [k] (2021) propose ...' — re-voices the contribution sentence."""
    sents = split_sentences(p.abstract)
    pat = r"^(in this (paper|work|study|article|letter),?\s*|here,?\s*)?(we|this (paper|work|study|article))\s+"
    idx = next((i for i, s in enumerate(sents) if re.match(pat, s, re.I)), None)
    head = f"{author_label(p)}~\\cite{{{p.key}}} ({p.year or 'n.d.'})"
    if idx is not None:
        m = re.match(pat, sents[idx], re.I)
        rest = sents[idx][m.end():]
        if not m.group(3).lower().startswith("we"):  # "This paper proposes" keeps singular verb
            head = f"The work in~\\cite{{{p.key}}} ({p.year or 'n.d.'})"
        elif len(p.authors) < 2:  # keep plural verb agreement for single-author papers
            head = f"In~\\cite{{{p.key}}} ({p.year or 'n.d.'}), the authors"
        extra = " ".join(sents[idx + 1: idx + 2])
        words = (rest + " " + extra).split()
        txt = " ".join(words[:max_words]).rstrip(".,;:")
        for a, b in ((r"\bOur\b", "Their"), (r"\bour\b", "their"), (r"\bWe\b", "They"),
                     (r"\bwe\b", "they"), (r"\bus\b", "them")):
            txt = re.sub(a, b, txt)
        end = "\\ldots" if len(words) > max_words else "."
        return f"{head} {escape(txt)}{end}"
    return f"{head} study the following problem: {first_sentences(p.abstract, 2, max_words)}"


def extractive_summary(p: Paper, max_words: int = 28) -> str:
    """Offline one-liner: the abstract's contribution sentence, re-voiced (LaTeX-escaped)."""
    sents = split_sentences(p.abstract)
    pick = next((s for s in sents if re.search(
        r"\b(we|this (paper|work|study|article)|propose|present|introduce|develop)\b", s, re.I)),
        sents[0] if sents else p.title)
    s = re.sub(r"^(in this (paper|work|study|article|letter),?\s*)", "", pick, flags=re.I)
    s = re.sub(r"^(here,?\s*)", "", s, flags=re.I)
    s = re.sub(r"^we\s+", "", s, flags=re.I)
    s = re.sub(r"^this (paper|work|study|article)\s+", "", s, flags=re.I)
    words = s.split()
    s = " ".join(words[:max_words]).rstrip(".,;:")
    s = s[:1].upper() + s[1:] if s else p.title
    return escape(s) + ("\\ldots" if len(words) > max_words else "")


def paper_block(p: Paper, abstract_chars: int = 1400) -> str:
    venue = p.venue if p.venue_type != "preprint" else "arXiv preprint"
    ab = p.abstract[:abstract_chars] + ("..." if len(p.abstract) > abstract_chars else "")
    return (f"[key: {p.key}] {p.title} — {', '.join(p.authors[:4])}"
            f"{' et al.' if len(p.authors) > 4 else ''} ({p.year or 'n.d.'}), {venue}; "
            f"citations: {p.citations}\nAbstract: {ab}")


# ---------------------------------------------------------------- planning
def plan_queries(llm: LLM, topic: str, n: int = 6) -> list[str]:
    base = [topic]
    if not llm.enabled:
        return base + [f"{topic} survey", f"{topic} review"]
    try:
        qs = llm.complete_json(
            "You design literature search strategies for academic databases.",
            f"Topic: {topic}\nProduce {n} diverse, short (2-6 words) keyword search queries "
            "for arXiv/Semantic Scholar/OpenAlex that together cover the topic's core methods, "
            "sub-problems, applications and evaluation. Use precise technical terms and common "
            "synonyms. JSON list of strings.", max_tokens=600)
        qs = [q.strip() for q in qs if isinstance(q, str) and q.strip()]
        return list(dict.fromkeys(base + qs))[: n + 1]
    except Exception as e:
        log.warning("query planning failed (%s); using topic only", e)
        return base + [f"{topic} survey"]


def name_themes(llm: LLM, topic: str, themes, papers: list[Paper]) -> dict[int, str]:
    names = {}
    for t, terms in themes:
        chosen: list[str] = []
        for w in sorted(terms, key=lambda x: -len(x.split())):
            if not any(set(w.split()) & set(c.split()) for c in chosen):
                chosen.append(w)
            if len(chosen) == 2:
                break
        names[t] = " and ".join(w.title() for w in chosen) or f"Theme {t + 1}"
    if not llm.enabled:
        return names
    listing = []
    for t, terms in themes:
        titles = [p.title for p in papers if p.theme == t][:12]
        listing.append({"id": t, "keywords": terms, "titles": titles})
    try:
        out = llm.complete_json(
            SYSTEM,
            f"Survey topic: {topic}\nThese are clusters of papers. Give each cluster a concise, "
            "distinct section title (3-8 words, Title Case) suitable for a survey taxonomy. "
            f"Clusters:\n{listing}\nJSON object mapping id (as string) to title.",
            max_tokens=800)
        for k, v in out.items():
            if str(k).isdigit() and int(k) in names and isinstance(v, str):
                names[int(k)] = v.strip()
    except Exception as e:
        log.warning("theme naming failed (%s); using keyword labels", e)
    return names


def summarize_papers(llm: LLM, papers: list[Paper], batch: int = 15) -> None:
    for p in papers:
        p.summary = extractive_summary(p)
    if not llm.enabled:
        return
    for i in range(0, len(papers), batch):
        chunk = papers[i:i + batch]
        try:
            out = llm.complete_json(
                SYSTEM,
                "For each paper write ONE plain-text phrase (max 20 words, no citation, no "
                "LaTeX) stating its main contribution/method, based strictly on the abstract.\n\n"
                + "\n\n".join(paper_block(p, 900) for p in chunk)
                + "\n\nJSON object mapping key to phrase.", max_tokens=2000)
            for p in chunk:
                if isinstance(out.get(p.key), str) and out[p.key].strip():
                    p.summary = escape(out[p.key].strip().rstrip("."))
        except Exception as e:
            log.warning("summary batch failed (%s); keeping extractive summaries", e)


# ------------------------------------------------------------ section text
class Writer:
    def __init__(self, llm: LLM, topic: str, papers: list[Paper], theme_names: dict[int, str],
                 critic: bool = False):
        self.llm, self.topic, self.papers = llm, topic, papers
        self.theme_names = theme_names
        self.keys = {p.key for p in papers}
        self.critic = critic
        self.removed_keys: list[str] = []

    def _llm_body(self, prompt: str, max_tokens: int = 4096, evidence: str = "") -> str:
        txt = self.llm.complete(SYSTEM, prompt, max_tokens=max_tokens)
        if self.critic and evidence:
            txt = self.llm.complete(
                SYSTEM,
                "You are a strict reviewer. Check the draft survey text below against the "
                "evidence. Remove or correct any claim about a cited work that the evidence does "
                "not support, fix wrong citations, and remove citation keys not in the evidence. "
                "Keep everything else, including structure and length. Return ONLY the corrected "
                f"LaTeX body.\n\nEVIDENCE:\n{evidence}\n\nDRAFT:\n{txt}",
                max_tokens=max_tokens)
        body = sanitize_llm_latex(txt)
        body, removed = validate_citations(body, self.keys)
        if removed:
            log.warning("removed %d unknown citation key(s): %s", len(removed), removed[:5])
            self.removed_keys += removed
        return body

    def _overview(self, max_items: int = 150) -> str:
        lines = []
        for t, name in sorted(self.theme_names.items()):
            lines.append(f"\n## {name}")
            for p in [p for p in self.papers if p.theme == t][:max_items]:
                lines.append(f"- [{p.key}] ({p.year}) {p.title}: {p.summary}")
        return "\n".join(lines)

    # ---- sections
    def abstract(self) -> str:
        n, yrs = len(self.papers), [p.year for p in self.papers if p.year]
        span = f"{min(yrs)}--{max(yrs)}" if yrs else ""
        themes = ", ".join(self.theme_names[t] for t in sorted(self.theme_names))
        if self.llm.enabled:
            txt = self.llm.complete(
                SYSTEM,
                f"Write the abstract (170-230 words, single paragraph, no citations) for a "
                f"survey on \"{self.topic}\" covering {n} works ({span}) organised into these "
                f"themes: {themes}. Mention scope, taxonomy, key findings/trends, and open "
                f"challenges.\n\nPaper overview:{self._overview(40)}", max_tokens=700)
            return validate_citations(sanitize_llm_latex(txt), set())[0]
        return (f"This paper presents a systematic literature survey on {escape(self.topic)}. "
                f"We review {n} openly accessible works published between {span}, retrieved from "
                f"arXiv, Semantic Scholar and OpenAlex, and organise them into a taxonomy of "
                f"{len(self.theme_names)} themes: {escape(themes)}. For each theme we summarise "
                f"the representative approaches and compare their contributions. We further "
                f"analyse publication trends and discuss open challenges and directions for "
                f"future research.")

    def keywords(self) -> str:
        if self.llm.enabled:
            try:
                kw = self.llm.complete_json(
                    SYSTEM, f"5-7 IEEE-style index terms for a survey on {self.topic}. "
                            "JSON list of strings.", max_tokens=200)
                return ", ".join(escape(k) for k in kw if isinstance(k, str))
            except Exception:
                pass
        return escape(self.topic) + ", literature survey, " + ", ".join(
            escape(self.theme_names[t]).lower() for t in sorted(self.theme_names)[:4])

    def introduction(self) -> str:
        top = sorted(self.papers, key=lambda p: -p.citations)[:25]
        if self.llm.enabled:
            ev = "\n\n".join(paper_block(p, 600) for p in top)
            return self._llm_body(
                f"Write the Introduction of a comprehensive survey on \"{self.topic}\" "
                "(5-7 paragraphs). Cover: motivation and importance; the problem landscape; why "
                "a survey is needed now; scope of this survey; a contributions list as an "
                "itemize environment; and a final paragraph on paper organisation that refers to "
                "Section~\\ref{sec:method} (methodology), Section~\\ref{sec:background} "
                "(background), the thematic sections, Section~\\ref{sec:discussion} "
                "(discussion), Section~\\ref{sec:challenges} (open challenges) and "
                "Section~\\ref{sec:conclusion}. Cite the most relevant works below where they "
                f"support a statement.\n\nTaxonomy:{self._overview(8)}\n\nKey works:\n{ev}",
                max_tokens=3500, evidence=ev)
        cites = ",".join(p.key for p in top[:6])
        themes = "; ".join(escape(self.theme_names[t]) for t in sorted(self.theme_names))
        return (
            f"Research on {escape(self.topic)} has grown rapidly, with a substantial body of "
            f"openly available work \\cite{{{cites}}}. The breadth of this literature makes it "
            f"difficult for newcomers and practitioners to identify the main lines of research, "
            f"their relative strengths and the problems that remain open.\n\n"
            f"This survey reviews {len(self.papers)} works and organises them into the following "
            f"themes: {themes}. The main contributions of this survey are:\n"
            "\\begin{itemize}\n"
            "\\item a reproducible search and selection methodology over open scholarly indexes;\n"
            "\\item a thematic taxonomy of the literature with per-theme comparative tables;\n"
            "\\item an analysis of publication trends; and\n"
            "\\item a discussion of open challenges and future research directions.\n"
            "\\end{itemize}\n\n"
            "The remainder of the paper is organised as follows. Section~\\ref{sec:method} "
            "describes the survey methodology and Section~\\ref{sec:background} provides "
            "background. The thematic sections review each research direction, "
            "Section~\\ref{sec:discussion} discusses cross-cutting observations, "
            "Section~\\ref{sec:challenges} outlines open challenges, and "
            "Section~\\ref{sec:conclusion} concludes the paper.")

    def background(self) -> str:
        found = [p for p in self.papers if re.search(r"\b(survey|review|tutorial|overview)\b",
                                                      p.title, re.I)]
        found = (found + sorted(self.papers, key=lambda p: -p.citations))[:20]
        found = list({p.key: p for p in found}.values())
        if self.llm.enabled:
            ev = "\n\n".join(paper_block(p, 900) for p in found)
            return self._llm_body(
                f"Write the Background section of a survey on \"{self.topic}\" (4-6 paragraphs, "
                "may use \\subsection). Define core concepts and terminology, give the standard "
                "problem formulation (with equations in $...$ or equation environments where "
                "useful), and position existing surveys/reviews below, noting how this survey "
                f"differs. Cite supporting works.\n\nEvidence:\n{ev}",
                max_tokens=3500, evidence=ev)
        prior = [p for p in found if re.search(r"survey|review|overview", p.title, re.I)]
        txt = (f"This section introduces the concepts underlying {escape(self.topic)} and "
               "positions this survey relative to prior reviews.\n\n")
        if prior:
            txt += "Several prior surveys and reviews exist. " + " ".join(
                f"{author_label(p)}~\\cite{{{p.key}}} ({p.year}): {p.summary}." for p in prior[:8])
            txt += (" In contrast to these, the present survey is generated from an up-to-date, "
                    "reproducible search over open-access indexes and covers the full set of "
                    "themes identified in Section~\\ref{sec:method}.")
        else:
            txt += "Highly cited foundational works include " + ", ".join(
                f"\\cite{{{p.key}}}" for p in found[:8]) + "."
        return txt

    def theme_section(self, t: int) -> str:
        ps = sorted([p for p in self.papers if p.theme == t], key=lambda p: (p.year or 0))
        name = self.theme_names[t]
        if self.llm.enabled:
            ev = "\n\n".join(paper_block(p) for p in ps)
            n_par = max(3, min(14, len(ps) // 2 + 2))
            return self._llm_body(
                f"Write the survey section titled \"{name}\" for a survey on \"{self.topic}\". "
                f"Aim for about {n_par} substantial paragraphs; for 8+ papers organise them into "
                "2-4 \\subsection{...} groupings of closely related approaches. Open with a short "
                "framing paragraph. Critically synthesise and COMPARE methods (assumptions, "
                "techniques, datasets, reported results, limitations) rather than listing papers "
                "one by one; trace how the line of work evolved. Every paper below must be cited "
                "at least once with \\cite{key}. End with a short paragraph summarising the "
                f"theme's state and gaps.\n\nPapers ({len(ps)}):\n{ev}",
                max_tokens=min(8000, 1500 + 350 * len(ps)), evidence=ev)
        txt = (f"This section reviews {len(ps)} works on {escape(name.lower())}; "
               f"Table~\\ref{{tab:theme{t}}} summarises them.\n\n")
        for i, p in enumerate(ps):
            lead = ["", "Subsequently, ", "Building on this line of work, ", "In addition, ",
                    "Similarly, "][i if i < 5 else 1 + i % 4]
            sent = voiced_sentence(p)
            if lead and re.match(r"(In|The)[~ ]", sent):
                sent = sent[0].lower() + sent[1:]
            txt += f"{lead}{sent}\n\n"
        return txt

    def discussion(self, trend_note: str) -> str:
        if self.llm.enabled:
            return self._llm_body(
                f"Write the Discussion section of the survey on \"{self.topic}\" (4-6 "
                "paragraphs, may use \\subsection). Compare the themes against each other, "
                "identify cross-cutting trends, dominant techniques, datasets/benchmarks and "
                "evaluation practices, and reproducibility. Refer to "
                f"Fig.~\\ref{{fig:years}} for the publication trend ({trend_note}). Cite works "
                f"from the overview.\n\nOverview:{self._overview(30)}", max_tokens=3500)
        counts = Counter(p.theme for p in self.papers)
        biggest = self.theme_names[counts.most_common(1)[0][0]]
        return (f"Fig.~\\ref{{fig:years}} shows the distribution of the surveyed works by year "
                f"({escape(trend_note)}). The largest theme in the reviewed literature is "
                f"``{escape(biggest)}'' with {counts.most_common(1)[0][1]} works. " +
                " ".join(f"The theme ``{escape(self.theme_names[t])}'' comprises {c} works."
                         for t, c in counts.most_common()[1:]))

    def challenges(self) -> str:
        if self.llm.enabled:
            limits = []
            for p in self.papers:
                s = [x for x in split_sentences(p.abstract)
                     if re.search(r"limit|challeng|future|remain|open|however|lack", x, re.I)]
                if s:
                    limits.append(f"[{p.key}] {' '.join(s[:2])}")
            return self._llm_body(
                f"Write the section \"Open Challenges and Future Directions\" for the survey on "
                f"\"{self.topic}\". Use 5-8 \\subsection{{...}} items, each a concrete challenge "
                "with a paragraph explaining why it is open and promising directions, citing "
                "works whose statements support it.\n\nStatements about limitations/challenges "
                f"from abstracts:\n" + "\n".join(limits[:80]) +
                f"\n\nOverview:{self._overview(15)}", max_tokens=4000,
                evidence="\n".join(limits[:80]))
        lim = []
        for p in self.papers:
            for s in split_sentences(p.abstract):
                if re.search(r"\b(limitation|challeng\w*|remains? (an )?open|future work)\b", s, re.I):
                    lim.append(f"{escape(s.strip().rstrip('.'))}~\\cite{{{p.key}}}.")
                    break
        if not lim:
            return ("The reviewed works leave several questions open, including robustness, "
                    "scalability, evaluation standardisation and reproducibility.")
        return ("The following challenges are explicitly identified in the surveyed literature:\n"
                "\\begin{itemize}\n" + "\n".join(f"\\item {l}" for l in lim[:15]) +
                "\n\\end{itemize}")

    def conclusion(self) -> str:
        if self.llm.enabled:
            return self._llm_body(
                f"Write the Conclusion (2-3 paragraphs, no new citations needed) of the survey "
                f"on \"{self.topic}\" summarising the taxonomy, main findings and the most "
                f"important future directions.\n\nTaxonomy:{self._overview(5)}", max_tokens=1200)
        return (f"This survey reviewed {len(self.papers)} works on {escape(self.topic)}, "
                f"organised into {len(self.theme_names)} themes. We summarised the approaches in "
                "each theme, analysed publication trends, and outlined open challenges. We hope "
                "that this survey serves as a starting point for researchers entering the field.")


# ----------------------------------------------------------------- tables
def theme_tables(t: int, name: str, papers: list[Paper], rows_per_table: int = 14) -> str:
    ps = sorted([p for p in papers if p.theme == t], key=lambda p: (p.year or 0))
    out = []
    for i in range(0, len(ps), rows_per_table):
        chunk = ps[i:i + rows_per_table]
        part = f" (part {i // rows_per_table + 1})" if len(ps) > rows_per_table else ""
        label = f"\\label{{tab:theme{t}}}" if i == 0 else ""
        rows = []
        for p in chunk:
            venue = "arXiv" if p.venue_type == "preprint" or not p.venue else p.venue
            venue = escape(venue[:38] + ("..." if len(venue) > 38 else ""))
            rows.append(f"\\cite{{{p.key}}} & {p.year or '--'} & {venue} & {p.summary} \\\\")
        out.append(
            "\\begin{table*}[!t]\n\\centering\n\\renewcommand{\\arraystretch}{1.2}\n"
            f"\\caption{{Summary of works on {escape(name)}{part}}}{label}\n"
            "\\begin{tabularx}{\\textwidth}{@{}l c p{3.6cm} X@{}}\n\\toprule\n"
            "\\textbf{Ref.} & \\textbf{Year} & \\textbf{Venue} & \\textbf{Main contribution} \\\\\n"
            "\\midrule\n" + "\n".join(rows) + "\n\\bottomrule\n\\end{tabularx}\n\\end{table*}")
    return "\n\n".join(out)


def year_chart(papers: list[Paper]) -> tuple[str, str]:
    yrs = Counter(p.year for p in papers if p.year)
    if not yrs:
        return "", "no year data"
    lo, hi = min(yrs), max(yrs)
    coords = " ".join(f"({y},{yrs.get(y, 0)})" for y in range(lo, hi + 1))
    peak = max(yrs, key=yrs.get)
    recent = sum(v for y, v in yrs.items() if y >= hi - 2)
    note = (f"{len(papers)} works spanning {lo}--{hi}, peaking in {peak}; "
            f"{100 * recent // max(1, sum(yrs.values()))}\\% published in the last three years")
    tick = 1 if hi - lo <= 12 else (2 if hi - lo <= 24 else 5)
    chart = (
        "\\begin{figure}[!t]\n\\centering\n\\begin{tikzpicture}\n\\begin{axis}[\n"
        "  ybar, width=\\columnwidth, height=5cm, bar width=4pt,\n"
        f"  xmin={lo - 0.7}, xmax={hi + 0.7}, ymin=0,\n"
        "  xlabel={Year}, ylabel={Number of works},\n"
        f"  xtick distance={tick}, x tick label style={{/pgf/number format/1000 sep=, rotate=45, anchor=east, font=\\scriptsize}},\n"
        "  ymajorgrids, grid style={dashed,gray!30}, enlarge x limits=false]\n"
        f"\\addplot[fill=blue!55, draw=blue!70!black] coordinates {{{coords}}};\n"
        "\\end{axis}\n\\end{tikzpicture}\n"
        "\\caption{Distribution of the surveyed works by publication year.}\n"
        "\\label{fig:years}\n\\end{figure}")
    return chart, note.replace("\\%", "%")


def methodology(topic: str, queries: list[str], sources: list[str], stats: dict,
                n_final: int, papers: list[Paper], year_from, max_papers, open_only) -> str:
    names = {"arxiv": "arXiv", "semanticscholar": "Semantic Scholar", "openalex": "OpenAlex",
             "scopus": "Scopus"}
    src_rows = "\n".join(f"{names.get(s, s)} & {stats.get(s, 0)} \\\\" for s in sources)
    types = Counter(p.venue_type or "other" for p in papers)
    qlist = "\n".join(f"\\item \\texttt{{{escape(q)}}}" for q in queries)
    return (
        f"The literature was collected on {date.today():%B %d, %Y} from "
        f"{', '.join(names.get(s, s) for s in sources)}, which index "
        + ("peer-reviewed and openly accessible" if "scopus" in sources else "openly accessible")
        + " scholarly works. The following search queries were issued to every source:\n"
        f"\\begin{{itemize}}\n{qlist}\n\\end{{itemize}}\n\n"
        "Records of the same work returned by different sources or queries were merged using "
        "DOI, arXiv identifier and normalised title; when a preprint had a published version, "
        "the version of record was retained for citation. "
        + ("Only works with an open-access full text were kept. " if open_only else "")
        + (f"Works published before {year_from} were excluded. " if year_from else "")
        + "The remaining works were ranked by a weighted score combining TF-IDF relevance to "
        "the topic and queries (70\\%), log-scaled citation count (20\\%) and recency (10\\%); "
        "works without abstracts or with negligible relevance were discarded"
        + (f", and at most the {max_papers} highest-scoring works were retained. "
           if max_papers else ". ")
        + "Selected works were grouped into themes by $k$-means clustering of their TF-IDF "
        "representations. Table~\\ref{tab:prisma} summarises the selection process.\n\n"
        "\\begin{table}[!t]\n\\centering\n\\caption{Search and selection statistics}"
        "\\label{tab:prisma}\n\\begin{tabular}{@{}lr@{}}\n\\toprule\n"
        "\\textbf{Stage} & \\textbf{Records} \\\\\n\\midrule\n"
        f"{src_rows}\n\\midrule\n"
        f"Total retrieved & {stats.get('total_retrieved', 0)} \\\\\n"
        f"After de-duplication & {stats.get('after_dedup', 0)} \\\\\n"
        f"After open-access/date filtering & {stats.get('after_open_filter', 0)} \\\\\n"
        f"Included in survey & {n_final} \\\\\n\\midrule\n"
        f"\\quad journal articles & {types.get('journal', 0)} \\\\\n"
        f"\\quad conference papers & {types.get('conference', 0)} \\\\\n"
        f"\\quad preprints & {types.get('preprint', 0)} \\\\\n"
        f"\\quad other & {types.get('other', 0)} \\\\\n"
        "\\bottomrule\n\\end{tabular}\n\\end{table}")


# ----------------------------------------------------------------- document
PREAMBLE = r"""\documentclass[journal]{IEEEtran}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{cite}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage{array}
\usepackage{pgfplots}
\pgfplotsset{compat=1.17}
\usepackage[hidelinks]{hyperref}
\usepackage{url}
\hyphenation{op-tical net-works semi-conduc-tor}
"""


def assemble(title: str, author: str, sections: dict, theme_blocks: list[str], bib: str,
             embed_bib: bool = True) -> str:
    doc = []
    if embed_bib:
        doc.append("% The bibliography is embedded below so this single file compiles on its own "
                   "(e.g. on Overleaf).\n\\begin{filecontents*}[overwrite]{refs.bib}\n"
                   + bib + "\\end{filecontents*}\n")
    doc.append(PREAMBLE)
    doc.append(f"\\begin{{document}}\n\\title{{{escape(title)}}}\n"
               f"\\author{{{escape(author)}}}\n\\markboth{{Literature Survey}}"
               f"{{{escape(title)}}}\n\\maketitle\n")
    doc.append(f"\\begin{{abstract}}\n{sections['abstract']}\n\\end{{abstract}}\n")
    doc.append(f"\\begin{{IEEEkeywords}}\n{sections['keywords']}\n\\end{{IEEEkeywords}}\n")
    intro = sections["intro"].lstrip()
    m = re.match(r"([A-Za-z])([A-Za-z]*)(\s.*)", intro, re.S)
    if m:  # IEEE drop cap on the first word
        intro = f"\\IEEEPARstart{{{m.group(1).upper()}}}{{{m.group(2).upper()}}}{m.group(3)}"
    doc.append(f"\\section{{Introduction}}\\label{{sec:intro}}\n{intro}\n")
    doc.append(f"\\section{{Survey Methodology}}\\label{{sec:method}}\n{sections['method']}\n")
    doc.append(f"\\section{{Background}}\\label{{sec:background}}\n{sections['background']}\n")
    doc.extend(theme_blocks)
    doc.append(f"\\section{{Discussion}}\\label{{sec:discussion}}\n{sections['chart']}\n"
               f"{sections['discussion']}\n")
    doc.append(f"\\section{{Open Challenges and Future Directions}}\\label{{sec:challenges}}\n"
               f"{sections['challenges']}\n")
    doc.append(f"\\section{{Conclusion}}\\label{{sec:conclusion}}\n{sections['conclusion']}\n")
    doc.append("\\bibliographystyle{IEEEtran}\n\\bibliography{refs}\n\\end{document}\n")
    return "\n".join(doc)
