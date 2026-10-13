"""Offline tests: parsers, dedup, keys/bib, sanitiser, and an end-to-end build on
synthetic papers (no network needed).  Run:  python -m pytest -q tests  (or python tests/test_offline.py)"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litsurvey import sources
from litsurvey.latexutil import build_bib, make_keys, sanitize_llm_latex, validate_citations
from litsurvey.models import Paper
from litsurvey.sources import deduplicate, parse_arxiv_feed, parse_openalex_item, parse_s2_item

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry>
  <id>http://arxiv.org/abs/2005.11401v4</id>
  <published>2020-05-22T17:48:15Z</published>
  <title>Retrieval-Augmented Generation for
   Knowledge-Intensive NLP Tasks</title>
  <summary>Large pre-trained language models have been shown to store factual knowledge.
  We explore a general-purpose fine-tuning recipe for retrieval-augmented generation (RAG).</summary>
  <author><name>Patrick Lewis</name></author>
  <author><name>Ethan Perez</name></author>
  <arxiv:doi>10.5555/FAKE.DOI</arxiv:doi>
  <arxiv:journal_ref>NeurIPS 2020</arxiv:journal_ref>
  <link title="pdf" href="http://arxiv.org/pdf/2005.11401v4" rel="related"/>
 </entry>
</feed>"""

S2_ITEM = {"title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
           "abstract": "Large pre-trained language models have been shown to store factual "
                       "knowledge in their parameters, and achieve state-of-the-art results.",
           "authors": [{"name": "Patrick Lewis"}, {"name": "Ethan Perez"}, {"name": "Aleksandra Piktus"}],
           "year": 2020, "venue": "Neural Information Processing Systems",
           "externalIds": {"ArXiv": "2005.11401", "DOI": "10.5555/fake.doi"},
           "citationCount": 5000, "openAccessPdf": {"url": "https://arxiv.org/pdf/2005.11401"},
           "isOpenAccess": True, "url": "https://www.semanticscholar.org/paper/x",
           "publicationTypes": ["Conference"], "journal": None,
           "publicationVenue": {"name": "Neural Information Processing Systems", "type": "conference"}}

OA_ITEM = {"display_name": "Dense Passage Retrieval for Open-Domain Question Answering",
           "publication_year": 2020, "doi": "https://doi.org/10.18653/v1/2020.emnlp-main.550",
           "abstract_inverted_index": {"Open-domain": [0], "question": [1], "answering": [2],
                                       "relies": [3], "on": [4], "retrieval.": [5]},
           "authorships": [{"author": {"display_name": "Vladimir Karpukhin"}},
                           {"author": {"display_name": "Barlas Oğuz"}}],
           "primary_location": {"source": {"display_name": "Proceedings of EMNLP 2020",
                                           "type": "conference",
                                           "host_organization_name": "ACL"}},
           "biblio": {"first_page": "6769", "last_page": "6781"},
           "open_access": {"is_oa": True, "oa_url": "https://aclanthology.org/x.pdf"},
           "cited_by_count": 3000}


def test_parsers_and_dedup():
    a = parse_arxiv_feed(ARXIV_XML)
    assert len(a) == 1 and a[0].arxiv_id == "2005.11401" and a[0].year == 2020
    assert a[0].title == "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"
    s = parse_s2_item(S2_ITEM)
    assert s.venue_type == "conference" and s.citations == 5000
    o = parse_openalex_item(OA_ITEM)
    assert o.abstract.startswith("Open-domain question answering") and o.pages == "6769--6781"
    assert o.doi == "10.18653/v1/2020.emnlp-main.550"
    merged = deduplicate(a + [s, o])
    assert len(merged) == 2
    rag = merged[0]
    assert rag.venue_type == "conference" and rag.citations == 5000
    assert rag.sources == {"arxiv", "semanticscholar"} and len(rag.authors) == 3


def test_bib_and_sanitizer():
    ps = [parse_s2_item(S2_ITEM), parse_openalex_item(OA_ITEM)]
    make_keys(ps)
    assert ps[0].key == "lewis2020retrieval" and ps[1].key == "karpukhin2020dense"
    bib = build_bib(ps)
    assert "@inproceedings{lewis2020retrieval" in bib and "booktitle" in bib
    assert bib.isascii()  # unicode names converted to LaTeX accents
    body = ("**Key idea**: RAG [x] improves 5% of R&D_tasks with $x_i^2$ "
            "\\cite{lewis2020retrieval, fake2023paper} and \\cite{made2021up}.\n"
            "\\section{Oops}\n```latex\ncode\n```")
    body = sanitize_llm_latex(body)
    body, removed = validate_citations(body, {p.key for p in ps})
    assert "\\textbf{Key idea}" in body and "5\\%" in body and "R\\&D\\_tasks" in body
    assert "$x_i^2$" in body and "\\subsection{Oops}" in body and "```" not in body
    assert "\\cite{lewis2020retrieval}" in body and set(removed) == {"fake2023paper", "made2021up"}


def synthetic_corpus(n_per=12, seed=0):
    rnd = random.Random(seed)
    topics = {
        "dense retrieval": ["dense passage retrieval", "bi-encoder", "contrastive training",
                            "embedding index", "hard negatives"],
        "retrieval-augmented generation": ["generator conditioned on retrieved documents",
                                           "knowledge-intensive tasks", "hallucination reduction",
                                           "fusion-in-decoder", "open-domain QA"],
        "evaluation of RAG": ["faithfulness metrics", "benchmark for attribution",
                              "answer correctness", "human evaluation", "citation accuracy"],
    }
    first = ["Ana", "Wei", "Ravi", "Sofia", "Jonas", "Priya", "Kenji", "Lena", "Omar", "Élise"]
    last = ["Chen", "Sharma", "García", "Müller", "Tanaka", "Iyer", "Smith", "Nguyen", "Rossi"]
    venues = [("IEEE Transactions on Knowledge and Data Engineering", "journal"),
              ("Proceedings of ACL", "conference"), ("arXiv", "preprint"),
              ("Information Processing & Management", "journal")]
    papers = []
    i = 0
    for t, kws in topics.items():
        for _ in range(n_per):
            i += 1
            k = rnd.sample(kws, 3)
            v, vt = rnd.choice(venues)
            papers.append(Paper(
                title=f"{k[0].title()} with {k[1]} for {t}: a study #{i}",
                authors=[f"{rnd.choice(first)} {rnd.choice(last)}" for _ in range(rnd.randint(1, 6))],
                year=rnd.randint(2017, 2026),
                abstract=(f"In this paper, we propose a method for {t} based on {k[0]} and {k[1]}. "
                          f"Our approach uses {k[2]} to improve {t} by 12% on standard benchmarks "
                          f"(e.g. NQ & TriviaQA). However, a limitation remains in {k[2]} for "
                          f"long documents, which we leave as future work."),
                venue=v, venue_type=vt, arxiv_id=f"2{i:03d}.{i:05d}" if vt == "preprint" else "",
                doi=f"10.1000/test.{i}" if vt != "preprint" else "",
                citations=rnd.randint(0, 900), sources={"openalex"}))
    return papers


def test_end_to_end(tmp=Path(__file__).parent / "_out", fake_llm=False):
    from dataclasses import asdict
    from litsurvey import cli
    tmp.mkdir(exist_ok=True)
    ps = synthetic_corpus()
    rows = [dict(asdict(p), sources=sorted(p.sources)) for p in ps]
    j = tmp / "in.json"
    j.write_text(json.dumps({"queries": ["retrieval-augmented generation", "dense retrieval"],
                             "stats": {"arxiv": 20, "semanticscholar": 30, "openalex": 40,
                                       "total_retrieved": 90, "after_dedup": 50,
                                       "after_open_filter": 36},
                             "papers": rows}))
    cli.main(["retrieval-augmented generation", "--from-json", str(j), "-o", str(tmp / "survey"),
              "--sources", "arxiv,semanticscholar,openalex", "--themes", "3",
              "--author", "Test Author"])
    tex = (tmp / "survey" / "survey.tex").read_text()
    assert "\\bibliographystyle{IEEEtran}" in tex and "\\begin{table*}" in tex
    rep = json.loads((tmp / "survey" / "report.json").read_text())
    assert rep["uncited_papers"] == [], rep["uncited_papers"]


def test_llm_path(tmp=Path(__file__).parent / "_out_llm"):
    """Exercise the LLM code path with a fake model that misbehaves (markdown, a fake
    citation, unescaped specials) to check the guards."""
    import os
    import re
    from litsurvey import cli, llm as llm_mod
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")

    def fake_complete(self, system, prompt, max_tokens=None):
        if "search queries" in prompt:
            return '["dense retrieval", "RAG evaluation"]'
        if "section title" in prompt:
            return '{"0": "Retriever Architectures", "1": "Generation with Retrieved Context", "2": "Evaluating RAG Systems"}'
        if "ONE plain-text phrase" in prompt:
            keys = re.findall(r"\[key: (\w+)\]", prompt)
            return json.dumps({k: f"Method & results for {k} (5% gain)" for k in keys})
        if "index terms" in prompt:
            return '["retrieval", "RAG", "LLMs"]'
        keys = re.findall(r"\[key: (\w+)\]|\[(\w+)\] \(", prompt)
        keys = [a or b for a, b in keys][:6]
        cites = ",".join(keys) or "none"
        return (f"**Overview.** Methods improve R&D by 10% \\cite{{{cites}}} while "
                f"\\cite{{ghost2099paper}} does not exist.\n\n## A grouping\nMore text $x_i$.")

    llm_mod.LLM.complete = fake_complete
    tmp.mkdir(exist_ok=True)
    from dataclasses import asdict
    rows = [dict(asdict(p), sources=sorted(p.sources)) for p in synthetic_corpus()]
    (tmp / "in.json").write_text(json.dumps({"queries": ["rag"], "stats": {}, "papers": rows}))
    cli.main(["retrieval-augmented generation", "--from-json", str(tmp / "in.json"),
              "-o", str(tmp / "s"), "--llm", "anthropic", "--critic", "--themes", "3"])
    rep = json.loads((tmp / "s" / "report.json").read_text())
    assert "ghost2099paper" in rep["hallucinated_citations_removed"]
    assert rep["uncited_papers"] == []
    tex = (tmp / "s" / "survey.tex").read_text()
    assert "ghost2099paper" not in tex and "Retriever Architectures" in tex


if __name__ == "__main__":
    test_parsers_and_dedup()
    test_bib_and_sanitizer()
    test_end_to_end()
    test_llm_path()
    print("all tests passed")
