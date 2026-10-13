# litsurvey: IEEE-format literature surveys from open-access papers

Give it a topic. litsurvey searches **arXiv, Semantic Scholar and OpenAlex**, plus **Scopus** if
you have a key. It keeps open-access papers, merges duplicates, ranks them by relevance and groups
them into themes. Then it writes a complete **IEEEtran survey (`survey.tex` + `refs.bib`)**, and a
**PDF** if LaTeX is installed. There's no page limit: the survey grows with the number of papers
you include.

You can use it from a **web page on your own computer** or from the **command line**.

---

## Quick start (Windows)

### 1. Install the prerequisites (one time)

| What | Why | Get it |
|---|---|---|
| Python 3.10 or newer | runs litsurvey | <https://www.python.org/downloads/> (tick **"Add python.exe to PATH"**) |
| MiKTeX | turns the `.tex` into a PDF | <https://miktex.org/download> (during setup choose **"Install missing packages on the fly: Yes"**) |
| Git *(optional)* | to download the repo | <https://git-scm.com/download/win> (or use **Code → Download ZIP** on GitHub) |

After installing them, **close and reopen your terminal** so the new programs are on `PATH`.
Check that everything is found:

```powershell
python --version
pdflatex --version
```

### 2. Download litsurvey and install its packages

```powershell
git clone https://github.com/manthansingh09/ReviewSel.git
cd ReviewSel
pip install -r requirements.txt
```

### 3. Add your API keys

```powershell
copy .env.example .env
notepad .env
```

Fill in the keys you have, then save:

```
ANTHROPIC_API_KEY=sk-ant-...      # for Claude writing the survey  -> console.anthropic.com > API keys
SCOPUS_API_KEY=...                # for searching Scopus           -> dev.elsevier.com
```

`.env` is git-ignored, so your keys are never committed. Both keys are optional:

- Without `ANTHROPIC_API_KEY`, pick the **None** backend for an offline draft built from the abstracts.
- Without `SCOPUS_API_KEY`, leave Scopus unticked.

> Never paste keys into screenshots, issues or chat. If a key is exposed, revoke it and create a new one.

### 4a. Run the web app (easiest)

```powershell
python -m litsurvey.web
```

Your browser opens at **<http://localhost:8000>**. Then:

1. Type a **topic**, e.g. `k-means clustering`.
2. Choose the **writing backend**: Claude for full prose, or None for an offline draft.
3. Tick the **sources** you want. Scopus needs its key.
4. Leave **Build PDF** ticked and press **Generate survey**.

You'll see a progress bar and the live log. A run with Claude and 100 papers usually takes
10–20 minutes. When it finishes, the survey appears under **Your surveys**. From there you can:

- **View PDF** in the page, or **Download PDF**
- Download `survey.tex` / `refs.bib`
- Open the run report
- **Build PDF** / **Rebuild PDF** for a survey (for example, after installing MiKTeX)

Press **Ctrl+C** in the terminal to stop the server. Options: `--port 8080` uses another port,
and `--no-browser` stops it opening a tab.

The server only listens on `127.0.0.1`, so nobody else on your network can open it. Keys typed
into the page's **API keys** box are used for that one run and are never saved.

### 4b. …or use the command line

```powershell
python -m litsurvey "k-means clustering" --llm anthropic --pdf
python -m litsurvey "k-means clustering" --llm anthropic --sources scopus,openalex,semanticscholar --pdf
python -m litsurvey "k-means clustering"                       # offline draft, no API key needed
```

### 5. Find your results

Everything is written to a folder named after the topic, e.g. `survey_k_means_clustering\`:

| File | What it is |
|---|---|
| `survey.pdf` | The finished survey (if LaTeX is installed) |
| `survey.tex` | The LaTeX source. `refs.bib` is embedded in it, so this one file also compiles on **Overleaf** as-is |
| `refs.bib` | IEEE-ready BibTeX (published version preferred over the arXiv preprint; DOIs; arXiv IDs) |
| `papers.json` / `papers_all.json` | Selected / all retrieved papers, for reproducibility and re-runs |
| `report.json` | Stats, themes, uncited papers, and any made-up citation keys that were removed |

Survey folders are git-ignored, so your outputs stay local.

---

## Quick start (Linux / macOS)

```bash
git clone https://github.com/manthansingh09/ReviewSel.git && cd ReviewSel
pip install -r requirements.txt
# LaTeX for the PDF:
#   Ubuntu/Debian: sudo apt install texlive-latex-extra texlive-publishers texlive-pictures
#   macOS:         install MacTeX (https://tug.org/mactex/)
cp .env.example .env && nano .env      # add your keys
python -m litsurvey.web                # or: python -m litsurvey "your topic" --llm anthropic --pdf
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `PDF not built: no LaTeX found on PATH` | Install MiKTeX / TeX Live, **open a new terminal**, then rebuild only the PDF (no searching or API calls): `python -m litsurvey "k-means clustering" --pdf-only` (or press **Build PDF** in the web app) |
| `PDF built, but LaTeX reported errors` | The PDF exists; the listed lines show where the formatting broke. Open `survey.tex` at that line, or upload it to Overleaf |
| `ANTHROPIC_API_KEY is not set` | Add it to `.env` in the folder you run from, or type it in the web app's **API keys** box |
| `LLM request rejected (HTTP 401)` | Key mistyped or revoked: create a new one at console.anthropic.com |
| `credit balance` error | Add credits at console.anthropic.com → Billing |
| `Scopus refused the API key (HTTP 401/403)` | Scopus only works from a subscribing institution: use your campus network/VPN, or set `SCOPUS_INSTTOKEN` |
| `No papers found` | Check your internet connection, broaden the topic, or add more sources |
| `Only N relevant papers` | Lower `--min-relevance` (e.g. `0.04`) or give your own `--queries` |
| `python` / `pdflatex` not recognized | The installer didn't add it to PATH, or the terminal is old: reopen the terminal, or reinstall with "Add to PATH" ticked |
| Port 8000 already in use | `python -m litsurvey.web --port 8080` |

---

## Reference

### Writing backends (`--llm`)

| `--llm` | Result | Needs |
|---|---|---|
| `none` (default) | Offline draft: structure, tables, methodology, chart and citations are complete; prose is re-voiced from abstracts | nothing |
| `anthropic` | Full synthesised survey prose | `ANTHROPIC_API_KEY` (default model `claude-sonnet-5`, change with `--model`) |
| `openai` | Same, any OpenAI-compatible API (OpenAI, Groq, OpenRouter, Together, Gemini's OpenAI endpoint…) | `OPENAI_API_KEY`, optionally `--base-url` |
| `ollama` | Same, fully local | Ollama running; e.g. `--model llama3.1` |

**Citation safety.** The model only sees the papers you retrieved and is told to cite only their
keys. Every `\cite{}` is then checked against the bibliography: unknown keys are stripped and
logged in `report.json`. Every included paper is guaranteed at least one citation (its row in the
theme table). `--critic` adds a reviewer pass that checks each section against the abstracts and
removes unsupported claims (roughly doubles the LLM calls).

### Command-line options

```
--sources arxiv,semanticscholar,openalex,scopus   where to search (default: the first three)
--max-papers 150        papers to include (0 = every relevant one)
--per-query 80          results per query per source
--queries "a; b; c"     your own search queries instead of planned ones
--themes 6              fix the number of themed sections (default: auto)
--year-from 2019        exclude older work
--include-closed        also keep works without an open-access copy
--title "..." --author "..."
--pdf                   build survey.pdf (needs LaTeX)
--pdf-only              only (re)build survey.pdf from an existing survey.tex, then exit
--from-json survey_x/papers_all.json   re-write without searching again
--min-relevance 0.05    loosen/tighten the relevance cut-off
--critic                extra fact-checking pass (≈2× LLM calls)
-o OUT                  output folder (default: survey_<topic>)
```

### Environment variables (`.env` or shell)

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | `--llm anthropic` |
| `ANTHROPIC_WORKSPACE_ID` | only for organisation-level keys not scoped to a workspace |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | `--llm openai` |
| `SCOPUS_API_KEY` (alias `ELSEVIER_API_KEY`) | `--sources …,scopus` |
| `SCOPUS_INSTTOKEN` | Scopus institutional token for off-campus access |
| `S2_API_KEY` | higher Semantic Scholar rate limit |
| `OPENALEX_EMAIL`, `OPENALEX_API_KEY` | OpenAlex polite pool |

Variables set in the shell (`$env:NAME = "..."` in PowerShell, `export NAME=...` in bash) take
precedence over `.env`. `$env:` only lasts for that one terminal window.

### Scopus details

- Queries are sent as `TITLE-ABS-KEY(...)`, with `PUBYEAR > N` for `--year-from`, and
  `OPENACCESS(1)` unless `--include-closed` is given.
- Elsevier only serves Scopus to subscribing institutions. Run on your campus network or VPN, or
  set `SCOPUS_INSTTOKEN`.
- Keys without the `COMPLETE` view get no abstracts and only the first author. litsurvey then
  switches to the `STANDARD` view and fills missing abstracts from OpenAlex by DOI. Papers with no
  abstract anywhere are dropped by the ranking step, so keep `openalex` in `--sources`.

### Building the PDF

`--pdf` runs `pdflatex → bibtex → pdflatex ×2`, or `tectonic` if that's what you have installed.
MiKTeX downloads `IEEEtran`/`pgfplots` automatically on first use. LaTeX errors are printed with
their line numbers. Without a local install, upload `survey.tex` to Overleaf; `refs.bib` is
embedded in it.

### Survey structure

Abstract · Index Terms · I Introduction (with contributions list) · II Survey Methodology
(queries, sources, dedup/filter/ranking, PRISMA-style counts table) · III Background ·
one section **per theme** (comparison table of every paper + synthesised discussion) ·
Discussion (publication-year chart) · Open Challenges & Future Directions · Conclusion ·
References (`IEEEtran.bst`).

### Notes

- API responses are cached in `<out>/.cache`, so re-runs are fast and rate-limit friendly.
  arXiv is queried at most once every 3 s, as its terms require.
- Clustering is TF-IDF + k-means, and LLM mode names the themes. Adjust `--themes` if two
  sections look like they should merge or one looks too broad.
- Always read the generated text before submitting it anywhere. The tool guarantees that
  citations point at real, retrieved papers, but you remain the author.
- Tests (offline, no network): `python tests/test_offline.py`
