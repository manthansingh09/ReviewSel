"""LaTeX escaping/sanitising and IEEE-style BibTeX generation."""
from __future__ import annotations

import re
import unicodedata

from .models import Paper

try:  # optional, better Unicode coverage
    from pylatexenc.latexencode import UnicodeToLatexEncoder
    _U2L = UnicodeToLatexEncoder(non_ascii_only=True, replacement_latex_protection="braces-all",
                                 unknown_char_policy="unihex", unknown_char_warning=False)
except Exception:  # pragma: no cover
    _U2L = None

_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}
_UNI = {
    "\u2013": "--", "\u2014": "---", "\u2018": "`", "\u2019": "'", "\u201c": "``",
    "\u201d": "''", "\u2026": r"\ldots{}", "\u00a0": "~", "\u2212": "-", "\u00d7": r"$\times$",
    "\u2264": r"$\leq$", "\u2265": r"$\geq$", "\u2248": r"$\approx$", "\u00b1": r"$\pm$",
    "\u03b1": r"$\alpha$", "\u03b2": r"$\beta$", "\u03b3": r"$\gamma$", "\u03b4": r"$\delta$",
    "\u03b5": r"$\epsilon$", "\u03bb": r"$\lambda$", "\u03bc": r"$\mu$", "\u03c3": r"$\sigma$",
    "\u2192": r"$\rightarrow$", "\u00b0": r"$^\circ$", "\u2032": "'",
}


def _unicode(s: str) -> str:
    s = "".join(_UNI.get(c, c) for c in s)
    if _U2L is not None:
        return _U2L.unicode_to_latex(s)
    out = []
    for c in s:
        if ord(c) < 128:
            out.append(c)
        else:  # strip accents as a last resort so pdflatex never chokes
            base = unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
            out.append(base)
    return "".join(out)


def escape(text: str) -> str:
    """Escape plain text for LaTeX."""
    text = "".join(_SPECIAL.get(c, c) for c in (text or ""))
    return _unicode(text)


def _escape_outside_math(seg: str) -> str:
    # escape specials that are not already escaped / not LaTeX syntax
    seg = re.sub(r"(?<!\\)([&%#])", r"\\\1", seg)
    seg = re.sub(r"(?<![\\])_", r"\\_", seg)
    seg = re.sub(r"(?<!\\)\^", r"\\textasciicircum{}", seg)
    seg = re.sub(r"(?<!\\)\$", r"\\$", seg)                   # a lone $ would open math mode
    seg = re.sub(r"(?<!\\)\\\|", r"$\\|$", seg)              # \| is math-only (e.g. k-means\|)
    return seg


def _close_braces(body: str) -> str:
    """Close \\cite{... left open (LLM output cut off at the token limit) and any other
    unbalanced '{' so one truncated section can't break the whole PDF build."""
    body = re.sub(r"(\\cite[pt]?\{[^}]*?)(\s*)(?=\n\s*\n|\Z)", r"\1}\2", body)
    depth = 0
    for m in re.finditer(r"\\.|[{}]", body):
        t = m.group()
        if t == "{":
            depth += 1
        elif t == "}":
            depth = max(depth - 1, 0)
    return body.rstrip() + "}" * depth


def sanitize_llm_latex(body: str) -> str:
    """Make LLM-produced LaTeX body text safe for pdflatex."""
    body = body.strip()
    body = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", body, flags=re.M)      # code fences
    body = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", body)                 # markdown bold
    body = re.sub(r"(?<![\\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\\emph{\1}", body)
    body = re.sub(r"^#{1,3}\s*(.+)$", r"\\subsection{\1}", body, flags=re.M)   # md headings
    body = re.sub(r"^#{4,6}\s*(.+)$", r"\\paragraph{\1}", body, flags=re.M)
    body = re.sub(r"\\section\*?\{", r"\\subsection{", body)          # sections belong to us
    body = re.sub(r"\\(documentclass|begin\{document\}|end\{document\}|bibliography\w*)[^\n]*",
                  "", body)
    # escape outside math ($...$ or \( \)) and outside \cite{...}/\ref{...} arguments
    parts = re.split(r"(\$[^$]+\$|\\\(.+?\\\)|\\(?:cite|ref|label|url)\{[^}]*\})", body)
    for i in range(0, len(parts), 2):
        parts[i] = _unicode(_escape_outside_math(parts[i]))
    body = _close_braces("".join(parts))
    # drop list environments that were opened but never closed (and vice versa)
    for env in ("itemize", "enumerate"):
        o, c = body.count(f"\\begin{{{env}}}"), body.count(f"\\end{{{env}}}")
        if o > c:
            body += f"\n\\end{{{env}}}" * (o - c)
        elif c > o:
            for _ in range(c - o):
                body = body.replace(f"\\end{{{env}}}", "", 1)
    return body


def validate_citations(body: str, valid_keys: set[str]) -> tuple[str, list[str]]:
    """Drop any \\cite key not in the bibliography (hallucination guard)."""
    removed: list[str] = []

    def fix(m):
        keys = [k.strip() for k in m.group(1).split(",")]
        good = [k for k in keys if k in valid_keys]
        removed.extend(k for k in keys if k not in valid_keys)
        return f"\\cite{{{','.join(good)}}}" if good else ""

    body = re.sub(r"\\cite[pt]?\{([^}]*)\}", fix, body)
    body = re.sub(r"[ \t]+([.,;])", r"\1", body)
    return body, removed


# ------------------------------------------------------------------ BibTeX
_STOP = {"a", "an", "the", "on", "of", "for", "in", "and", "to", "with", "towards", "toward",
         "via", "from", "using", "is", "are", "by", "at"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def make_keys(papers: list[Paper]) -> None:
    used: set[str] = set()
    for p in papers:
        last = _ascii(p.authors[0].split()[-1]) if p.authors else "anon"
        last = re.sub(r"[^a-z]", "", last.lower()) or "anon"
        word = next((w for w in re.findall(r"[a-z]+", _ascii(p.title).lower())
                     if w not in _STOP and len(w) > 2), "paper")
        base = f"{last}{p.year or 'nd'}{word}"
        key, i = base, 0
        while key in used:
            i += 1
            key = base + chr(ord("a") + i)
        used.add(key)
        p.key = key


def _title_field(title: str) -> str:
    # double braces keep capitalisation (acronyms like CNN, BERT) intact
    return "{" + escape(title) + "}"


def _authors_field(authors: list[str]) -> str:
    authors = [a for a in authors if a.strip()]
    if not authors:
        return "{Anonymous}"
    if len(authors) > 30:  # IEEEtran truncates with "et al." anyway
        authors = authors[:30] + ["others"]
    return " and ".join(escape(a) if a != "others" else a for a in authors)


def to_bibtex(p: Paper) -> str:
    f: dict[str, str] = {"author": _authors_field(p.authors), "title": _title_field(p.title)}
    if p.year:
        f["year"] = str(p.year)
    if p.venue_type == "journal" and p.venue:
        etype = "article"
        f["journal"] = escape(p.venue)
        if p.volume: f["volume"] = escape(p.volume)
        if p.issue: f["number"] = escape(p.issue)
        if p.pages: f["pages"] = escape(p.pages).replace(r"\_", "")
    elif p.venue_type == "conference" and p.venue:
        etype = "inproceedings"
        bt = p.venue
        if not re.search(r"proc|conference|workshop|symposium", bt, re.I):
            bt = "Proc. " + bt
        f["booktitle"] = escape(bt)
        if p.pages: f["pages"] = escape(p.pages)
    elif p.arxiv_id:
        etype = "misc"
        f["howpublished"] = f"arXiv preprint arXiv:{p.arxiv_id}"
        if p.note:
            f["note"] = escape(p.note)
    else:
        etype = "misc"
        if p.venue:
            f["howpublished"] = escape(p.venue)
    if p.publisher and etype != "misc":
        f["publisher"] = escape(p.publisher)
    if p.doi:
        f["doi"] = p.doi
    elif p.arxiv_id:
        f["doi"] = f"10.48550/arXiv.{p.arxiv_id}"
    url = f"https://arxiv.org/abs/{p.arxiv_id}" if p.arxiv_id else (p.url or p.pdf_url)
    if url and not p.doi:
        f["url"] = url
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in f.items())
    return f"@{etype}{{{p.key},\n{body}\n}}"


def build_bib(papers: list[Paper]) -> str:
    header = "% Generated by litsurvey. Style: IEEEtran.bst\n\n"
    return header + "\n\n".join(to_bibtex(p) for p in papers) + "\n"
