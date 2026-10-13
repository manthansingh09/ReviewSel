# litsurvey — IEEE-format literature surveys from open-access papers

Give it a topic; it searches **arXiv, Semantic Scholar, OpenAlex** and optionally **Scopus**, keeps open-access works,
merges duplicates, ranks by relevance, groups papers into themes, and writes a complete
**IEEEtran `.tex` survey with a `refs.bib` in IEEE citation style**. There is no page limit —
the survey grows with the number of papers you let it include.

```
pip install -r requirements.txt
python -m litsurvey "retrieval-augmented generation" --llm anthropic --pdf
```

Prefer a browser? Start the local web UI (no extra installs) and it opens at
<http://localhost:8000>:

```
python -m litsurvey.web            # --port 8080 to change the port, --no-browser to not open a tab
```

Fill in the topic, pick the writing backend and sources (including Scopus), and press **Generate
survey**. The page shows live progress and the log, and lists every `survey_*` folder with
**View PDF**, downloads for `survey.tex` / `refs.bib`, and a **Build PDF** button for surveys
that don't have one yet. Keys come from your `.env`; ones typed into the page are used for that
run only and never saved. The server listens on `127.0.0.1` only, so other machines can't reach it.

Output (in `survey_<topic>/`):

| File | What it is |
|---|---|
| `survey.tex` | The survey. `refs.bib` is embedded via `filecontents`, so this one file compiles on Overleaf as-is |
| `refs.bib` | IEEE-ready BibTeX (published version preferred over the arXiv preprint; DOIs; arXiv IDs) |
| `survey.pdf` | With `--pdf` and a local TeX install (see [Building the PDF](#building-the-pdf)) |
| `papers.json` / `papers_all.json` | Selected / all retrieved records, for reproducibility and re-runs |
| `report.json` | Stats, themes, uncited papers, and any hallucinated citation keys that were removed |

## API keys

Each service has its own key, and you can use them together, e.g. Claude for writing and Scopus
for searching. Put them in a `.env` file in the folder you run from; it's loaded automatically
and git-ignored:

```
copy .env.example .env      # Windows   (cp .env.example .env on Linux/macOS)
```
```
ANTHROPIC_API_KEY=sk-ant-...
SCOPUS_API_KEY=...
```
```
python -m litsurvey "k-means clustering" --llm anthropic --sources scopus,openalex,semanticscholar --pdf
```

Variables already set in the shell (`$env:ANTHROPIC_API_KEY = "..."` in PowerShell, `export` in
bash) take precedence over `.env`. Note that `$env:` only lasts for that one terminal window.

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
--sources arxiv,openalex,scopus
--include-closed        also keep works without an open-access copy
--title "..." --author "..."
--from-json out/papers_all.json   re-write without searching again
--pdf-only              only (re)build survey.pdf from an existing survey.tex
--min-relevance 0.05    loosen/tighten the relevance cut-off
```

Optional environment variables: `SCOPUS_API_KEY` (enables `--sources scopus`), `S2_API_KEY` (higher Semantic Scholar rate limit),
`OPENALEX_EMAIL` (polite pool), `OPENALEX_API_KEY`.

## Scopus

Scopus is not open, so it needs an Elsevier API key (free at <https://dev.elsevier.com>):

```
# PowerShell
$env:SCOPUS_API_KEY = "your-elsevier-key"
# bash
export SCOPUS_API_KEY=your-elsevier-key

python -m litsurvey "k-means clustering" --sources scopus,openalex,semanticscholar --llm anthropic --pdf
```

- Queries are sent as `TITLE-ABS-KEY(...)`, with `PUBYEAR > N` for `--year-from` and
  `OPENACCESS(1)` unless `--include-closed` is given.
- Elsevier only serves Scopus to subscribing institutions: run on your campus network/VPN, or
  set `SCOPUS_INSTTOKEN` if your library issued an institutional token.
- Keys without the `COMPLETE` view get no abstracts or full author lists; litsurvey then falls
  back to the `STANDARD` view and fills missing abstracts from OpenAlex by DOI (papers with no
  abstract anywhere are dropped by the ranking step). Keep `openalex` in `--sources` for best results.
- `ELSEVIER_API_KEY` is accepted as an alias for `SCOPUS_API_KEY`.

## Building the PDF

`--pdf` runs `pdflatex → bibtex → pdflatex ×2` (or `tectonic` if that is what you have). You need
a LaTeX distribution on `PATH`:

- **Windows:** install [MiKTeX](https://miktex.org/download) (or TeX Live), then open a *new*
  terminal so `pdflatex` is on `PATH`. MiKTeX downloads `IEEEtran`/`pgfplots` on first use.
- **Linux:** `sudo apt install texlive-latex-extra texlive-publishers texlive-pictures`
- **macOS:** MacTeX.

If LaTeX is missing the run says so and still writes `survey.tex`; after installing, rebuild only
the PDF (no searching or LLM calls) with:

```
python -m litsurvey "k-means clustering" --pdf-only
```

Any LaTeX errors are printed with their line numbers. Without a local install, upload
`survey.tex` to Overleaf — `refs.bib` is embedded in it.

## Notes

- API responses are cached in `<out>/.cache`, so re-runs are fast and rate-limit friendly.
  arXiv is queried at ≤1 request / 3 s as its terms require.
- Clustering is TF-IDF + k-means; LLM mode names the themes. Adjust `--themes` if two
  sections look like they should merge or one looks too broad.
- Always read the generated text before submitting it anywhere: the tool guarantees
  citations point at real, retrieved papers, but you remain the author.
- Tests (offline, no network): `python tests/test_offline.py`
