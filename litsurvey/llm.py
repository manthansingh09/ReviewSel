"""Minimal LLM backends (no SDKs needed): Anthropic, any OpenAI-compatible API
(OpenAI, Groq, Together, OpenRouter, Gemini's OpenAI endpoint, local Ollama/vLLM/LM Studio),
or 'none' for a fully offline extractive draft."""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("litsurvey")


class LLMConfigError(SystemExit):
    """Key / model / account problem: retrying or falling back won't help, so stop the run."""


def _is_config_error(body: str) -> bool:
    b = body.lower()
    return any(s in b for s in ("api key", "api_key", "x-api-key", "workspace", "authentication",
                                "permission", "credit balance", "model:", "not_found",
                                "invalid model", "does not exist"))


def _explain(code: int, body: str) -> str:
    b = body.lower()
    if "workspace" in b:
        hint = ("Your key is an organization-level key not scoped to a workspace. Either create a "
                "key inside a workspace (console.anthropic.com > Settings > Workspaces > pick one "
                "> API keys), or set ANTHROPIC_WORKSPACE_ID to the workspace's ID (wrkspc_...).")
    elif "credit" in b:
        hint = "Your API account has no credits. Add them at console.anthropic.com > Billing."
    elif code == 401 or "api key" in b or "authentication" in b:
        hint = "ANTHROPIC_API_KEY / OPENAI_API_KEY is missing, mistyped or revoked."
    elif "model" in b or code == 404:
        hint = "The model name isn't available to this key; pass a valid one with --model."
    else:
        hint = "Check your key, account and --model."
    return f"\nLLM request rejected (HTTP {code}).\n{hint}\nServer said: {body[:300]}"


class LLM:
    def __init__(self, provider: str = "none", model: str | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 temperature: float | None = None, max_tokens: int = 4096):
        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._dropped: set[str] = set()
        self._max_key = "max_tokens"
        if provider == "anthropic":
            self.model = model or "claude-sonnet-5"
            self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
            self.base_url = base_url or "https://api.anthropic.com"
            # needed only for org-level keys that aren't scoped to a workspace
            self.workspace_id = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
        elif provider == "openai":
            self.model = model or "gpt-4o-mini"
            self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
            self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                             or "https://api.openai.com/v1").rstrip("/")
        elif provider == "ollama":
            self.provider = "openai"
            self.model = model or "llama3.1"
            self.api_key = "ollama"
            self.base_url = (base_url or "http://localhost:11434/v1").rstrip("/")
        elif provider == "none":
            self.model = None
        else:
            raise ValueError(f"unknown provider {provider}")
        if self.provider == "anthropic" and not self.api_key:
            raise SystemExit("ANTHROPIC_API_KEY is not set (put it in a .env file -- see .env.example)")

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    def check(self) -> None:
        """One tiny request up front so key/model problems surface before the search."""
        if self.enabled:
            self.complete("Reply with OK.", "OK?", max_tokens=16)

    # optional request settings; any a model rejects are dropped for the rest of the run
    _OPTIONAL = ("temperature", "top_p")

    def _payload(self, system: str, prompt: str, max_tokens: int) -> dict:
        if self.provider == "anthropic":
            body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": prompt}]}
        else:
            body = {"model": self.model, self._max_key: max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt}]}
        if self.temperature is not None and "temperature" not in self._dropped:
            body["temperature"] = self.temperature
        return body

    def _adapt(self, body: dict, err: str) -> bool:
        """If a 400 complains about a setting we sent, drop/rename it and return True."""
        e = err.lower()
        for k in self._OPTIONAL:
            if k in body and k in e:
                log.info("model doesn't accept '%s'; continuing without it", k)
                self._dropped.add(k)
                return True
        if self.provider != "anthropic" and "max_tokens" in e and self._max_key == "max_tokens":
            self._max_key = "max_completion_tokens"  # newer OpenAI reasoning models
            return True
        return False

    def complete(self, system: str, prompt: str, max_tokens: int | None = None) -> str:
        max_tokens = max_tokens or self.max_tokens
        attempt = 0
        while attempt < 5:
            body = self._payload(system, prompt, max_tokens)
            try:
                if self.provider == "anthropic":
                    headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"}
                    if self.workspace_id:
                        headers["anthropic-workspace-id"] = self.workspace_id
                    r = requests.post(f"{self.base_url}/v1/messages", headers=headers,
                                      json=body, timeout=300)
                    if r.status_code == 200:
                        return "".join(b.get("text", "") for b in r.json()["content"]
                                       if b.get("type") == "text")
                else:
                    r = requests.post(f"{self.base_url}/chat/completions",
                                      headers={"Authorization": f"Bearer {self.api_key}"},
                                      json=body, timeout=300)
                    if r.status_code == 200:
                        return r.json()["choices"][0]["message"]["content"] or ""
            except requests.RequestException as e:
                log.warning("LLM request error: %s", e)
                attempt += 1
                time.sleep(5 * attempt)
                continue
            if r.status_code == 400 and self._adapt(body, r.text):
                continue  # retry at once with the adjusted request
            if r.status_code in (429, 500, 502, 503, 529):
                attempt += 1
                time.sleep(5 * attempt)
                continue
            if r.status_code in (400, 401, 403, 404) and _is_config_error(r.text):
                raise LLMConfigError(_explain(r.status_code, r.text))
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError("LLM failed after retries")

    def complete_json(self, system: str, prompt: str, max_tokens: int | None = None):
        txt = self.complete(system, prompt + "\n\nReturn ONLY valid JSON, no prose.", max_tokens)
        m = re.search(r"(\{.*\}|\[.*\])", txt, re.S)
        if not m:
            raise ValueError("no JSON in LLM output")
        return json.loads(m.group(1))
