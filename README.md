# litsurvey — IEEE-format literature surveys from open-access papers

Give it a topic; it searches **arXiv, Semantic Scholar and OpenAlex**, keeps open-access works,
merges duplicates, ranks by relevance, groups papers into themes, and writes a complete
**IEEEtran `.tex` survey with a `refs.bib` in IEEE citation style**. There is no page limit —
the survey grows with the number of papers you let it include.

```
pip install -r requirements.txt
python -m litsurvey "retrieval-augmented generation" --llm anthropic --pdf
```

Output (in `survey_<topic>/`):

| File | What it is |
|---|---|
| `survey.tex` | The survey. `refs.bib` is embedded via `filecontents`, so this one file compiles on Overleaf as-is |
| `refs.bib` | IEEE-ready BibTeX (published version preferred over the arXiv preprint; DOIs; arXiv IDs) |
| `survey.pdf` | With `--pdf` and a local TeX install (needs the `IEEEtran` class — `texlive-publishers`) |
| `papers.json` / `papers_all.json` | Selected / all retrieved records, for reproducibility and re-runs |
| `report.json` | Stats, themes, uncited papers, and any hallucinated citation keys that were removed |

## Survey structure

Abstract · Index Terms · I Introduction (with contributions list) · II Survey Methodology
(queries, sources, dedup/filter/ranking, PRISMA-style counts table) · III Background ·
one section **per theme** (comparison table of every paper + synthesised discussion) ·
Discussion (publication-year chart) · Open Challenges & Future Directions · Conclusion ·
References (`IEEEtran.bst`).

## Writing backends

| `--llm` | Result | Needs |
|---|---|---|
| `none` (default) | Offline draft: structure, tables, methodology, chart and citations are complete; prose is re-voiced from abstracts | nothing |
| `anthropic` | Full synthesised survey prose | `ANTHROPIC_API_KEY` (default model `claude-sonnet-5`, change with `--model`) |
| `openai` | Same, any OpenAI-compatible API (OpenAI, Groq, OpenRouter, Together, Gemini's OpenAI endpoint…) | `OPENAI_API_KEY`, optionally `--base-url` |
| `ollama` | Same, fully local | Ollama running; e.g. `--model llama3.1` |

**Citation safety.** The model only sees the papers you retrieved, is told to cite only their
keys, and every `\cite{}` is then checked against the bibliography — unknown keys are stripped
and logged in `report.json`. Every included paper is guaranteed at least one citation (its row
in the theme table). `--critic` adds a reviewer pass that checks each section against the
abstracts and removes unsupported claims (roughly doubles LLM calls).

## Useful options

```
--max-papers 150        papers to include (0 = every relevant one)
--per-query 80          results per query per source
--queries "a; b; c"     your own search queries instead of planned ones
--themes 6              fix the number of themed sections (default: auto)
--year-from 2019        exclude older work
--sources arxiv,openalex
--include-closed        also keep works without an open-access copy
--title "..." --author "..."
--from-json out/papers_all.json   re-write without searching again
--min-relevance 0.05    loosen/tighten the relevance cut-off
```

Optional environment variables: `S2_API_KEY` (higher Semantic Scholar rate limit),
`OPENALEX_EMAIL` (polite pool), `OPENALEX_API_KEY`.

## Notes

- API responses are cached in `<out>/.cache`, so re-runs are fast and rate-limit friendly.
  arXiv is queried at ≤1 request / 3 s as its terms require.
- Clustering is TF-IDF + k-means; LLM mode names the themes. Adjust `--themes` if two
  sections look like they should merge or one looks too broad.
- Always read the generated text before submitting it anywhere: the tool guarantees
  citations point at real, retrieved papers, but you remain the author.
- Tests (offline, no network): `python tests/test_offline.py`
