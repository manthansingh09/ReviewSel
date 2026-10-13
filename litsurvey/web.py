"""Local web front end:  python -m litsurvey.web  ->  http://localhost:8000

Runs the normal CLI (python -m litsurvey ...) in the background for each job, streams its log
to the browser, and lists the survey_* folders with their .tex / .bib / .pdf files.
Standard library only; listens on 127.0.0.1, so it is reachable from this computer only."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .cli import load_dotenv, slug
from .sources import SOURCES

STATIC = Path(__file__).with_name("web_static")
ROOT = Path.cwd()                                   # surveys are read/written here
SHOWN = (".pdf", ".tex", ".bib", ".json")           # files offered for download
KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SCOPUS_API_KEY", "SCOPUS_INSTTOKEN",
            "S2_API_KEY", "ANTHROPIC_WORKSPACE_ID")

JOBS: dict[str, "Job"] = {}


class Job:
    def __init__(self, argv: list[str], env: dict, out: str, label: str):
        self.id = uuid.uuid4().hex[:10]
        self.argv, self.out, self.label = argv, out, label
        self.lines: list[str] = []
        self.status = "running"
        self.started = time.time()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "litsurvey", *argv], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
        code = self.proc.wait()
        if self.status != "cancelled":
            self.status = "done" if code == 0 else "failed"

    def cancel(self):
        if self.status == "running":
            self.status = "cancelled"
            self.proc.terminate()

    def view(self, since: int = 0) -> dict:
        return {"id": self.id, "label": self.label, "out": self.out, "status": self.status,
                "started": self.started, "lines": self.lines[since:], "next": len(self.lines)}


# ------------------------------------------------------------------ helpers
def _int(v, lo=0, hi=100000):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return None


def build_argv(f: dict) -> tuple[list[str], str]:
    """Form fields -> CLI arguments (whitelisted; nothing is passed to a shell)."""
    topic = " ".join(str(f.get("topic", "")).split())[:300]
    if not topic:
        raise ValueError("Topic is required.")
    out = f"survey_{slug(topic)}"
    argv = [topic, "-o", out]
    llm = f.get("llm", "none")
    if llm not in ("none", "anthropic", "openai", "ollama"):
        raise ValueError("Unknown writing backend.")
    argv += ["--llm", llm]
    sources = [s for s in f.get("sources", []) if s in SOURCES]
    if not sources:
        raise ValueError("Pick at least one source.")
    argv += ["--sources", ",".join(sources)]
    for name, flag, lo, hi in (("max_papers", "--max-papers", 0, 2000),
                               ("per_query", "--per-query", 5, 500),
                               ("themes", "--themes", 2, 30),
                               ("year_from", "--year-from", 1900, 2100)):
        v = _int(f.get(name), lo, hi) if str(f.get(name, "")).strip() else None
        if v is not None:
            argv += [flag, str(v)]
    for name, flag in (("model", "--model"), ("title", "--title"), ("author", "--author"),
                       ("queries", "--queries"), ("base_url", "--base-url")):
        v = str(f.get(name, "")).strip()
        if v:
            argv += [flag, v[:1000]]
    for name, flag in (("include_closed", "--include-closed"), ("critic", "--critic"),
                       ("pdf", "--pdf")):
        if f.get(name):
            argv.append(flag)
    return argv, out


def job_env(keys: dict) -> dict:
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for k in KEY_VARS:  # keys typed in the page are used for this job only, never saved
        v = str((keys or {}).get(k, "")).strip()
        if v:
            env[k] = v
    return env


def list_surveys() -> list[dict]:
    rows = []
    for d in sorted(ROOT.glob("survey_*/"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not (d / "survey.tex").exists():
            continue
        rep = {}
        try:
            rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        rows.append({
            "dir": d.name, "topic": rep.get("topic") or d.name[7:].replace("_", " "),
            "papers": rep.get("papers_included"), "themes": list((rep.get("themes") or {}).keys()),
            "llm": rep.get("llm"), "updated": (d / "survey.tex").stat().st_mtime,
            "files": [p.name for p in sorted(d.iterdir()) if p.suffix in SHOWN],
            "running": any(j.out == d.name and j.status == "running" for j in JOBS.values()),
        })
    return rows


def status() -> dict:
    return {
        "keys": {k: bool(os.getenv(k)) for k in KEY_VARS},
        "latex": bool((shutil.which("pdflatex") and shutil.which("bibtex"))
                      or shutil.which("tectonic")),
        "sources": list(SOURCES), "root": str(ROOT),
    }


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "litsurvey"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # localhost only: reject other Host headers (DNS rebinding) and cross-site POSTs
    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("localhost", "127.0.0.1", "::1"):
            self._send(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin and urlparse(origin).hostname not in (
                "localhost", "127.0.0.1", "::1"):
            self._send(403, {"error": "cross-origin request refused"})
            return False
        return True

    def _send(self, code: int, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self) -> dict:
        n = _int(self.headers.get("Content-Length"), 0, 1_000_000) or 0
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if not self._local():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, status())
        if path == "/api/surveys":
            return self._send(200, list_surveys())
        if path == "/api/jobs":
            return self._send(200, [j.view(len(j.lines)) for j in JOBS.values()])
        m = re.fullmatch(r"/api/jobs/(\w+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._send(404, {"error": "no such job"})
            q = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&") if "=" in p)
            return self._send(200, job.view(_int(q.get("since"), 0) or 0))
        m = re.fullmatch(r"/files/(survey_[\w-]+)/([\w.-]+)", path)
        if m:
            f = (ROOT / m.group(1) / unquote(m.group(2))).resolve()
            if f.parent.parent != ROOT.resolve() or f.suffix not in SHOWN or not f.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(f.name)[0] or "text/plain"
            if f.suffix in (".tex", ".bib"):
                ctype = "text/plain"
            disp = "inline" if f.suffix in (".pdf", ".json") else "attachment"
            return self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if "text" in ctype
                                                             or "json" in ctype else ""),
                              {"Content-Disposition": f'{disp}; filename="{f.name}"'})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local():
            return
        path = urlparse(self.path).path
        body = self._json()
        if path == "/api/jobs":
            try:
                argv, out = build_argv(body.get("form") or {})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if any(j.out == out and j.status == "running" for j in JOBS.values()):
                return self._send(409, {"error": f"A job for {out} is already running."})
            job = Job(argv, job_env(body.get("keys")), out, argv[0])
            JOBS[job.id] = job
            return self._send(200, job.view())
        if path == "/api/pdf":
            d = str(body.get("dir", ""))
            if not re.fullmatch(r"survey_[\w-]+", d) or not (ROOT / d / "survey.tex").exists():
                return self._send(400, {"error": "unknown survey folder"})
            job = Job(["pdf", "-o", d, "--pdf-only"], job_env({}), d, f"PDF: {d}")
            JOBS[job.id] = job
            return self._send(200, job.view())
        m = re.fullmatch(r"/api/jobs/(\w+)/cancel", path)
        if m and m.group(1) in JOBS:
            JOBS[m.group(1)].cancel()
            return self._send(200, JOBS[m.group(1)].view(len(JOBS[m.group(1)].lines)))
        self._send(404, {"error": "not found"})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="litsurvey.web", description="litsurvey local web UI")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    a = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://localhost:{a.port}"
    print(f"litsurvey UI running at {url}  (surveys folder: {ROOT})  -- Ctrl+C to stop")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        for j in JOBS.values():
            j.cancel()
        srv.server_close()


if __name__ == "__main__":
    main()
