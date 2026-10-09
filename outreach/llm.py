"""Client for any OpenAI-compatible chat endpoint (e.g. freellmapi.co).

Two tiers: "fast" models for cheap analysis, "strong" models for writing.
Each tier is a fallback list: on rate limits or errors the next model is tried.
Every call's token usage is logged to the llm_usage table.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

import httpx

from .config import Settings
from .db import now


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self.s = settings
        self.conn = conn
        self._last_call = 0.0
        self.client = httpx.Client(
            base_url=settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=120,
        )

    def _throttle(self) -> None:
        wait = self.s.llm_min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def chat(self, tier: str, purpose: str, system: str, user: str, max_tokens: int = 800) -> str:
        models = self.s.llm_strong_models if tier == "strong" else self.s.llm_fast_models
        errors = []
        for model in models:
            for attempt in range(2):
                self._throttle()
                try:
                    resp = self.client.post("/chat/completions", json={
                        "model": model,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "max_tokens": max_tokens,
                        "temperature": 0.4,
                    })
                except httpx.HTTPError as exc:
                    errors.append(f"{model}: {exc}")
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    errors.append(f"{model}: HTTP {resp.status_code}")
                    time.sleep(5 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    errors.append(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
                    break  # bad request for this model, so try the next one
                data = resp.json()
                usage = data.get("usage") or {}
                self.conn.execute(
                    "INSERT INTO llm_usage (at, model, purpose, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?)",
                    (now(), model, purpose, usage.get("prompt_tokens"), usage.get("completion_tokens")),
                )
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if content.strip():
                    return content
                errors.append(f"{model}: empty response")
                break
        raise LLMError("; ".join(errors) or "no models configured")

    def chat_json(self, tier: str, purpose: str, system: str, user: str, max_tokens: int = 800) -> dict:
        text = self.chat(tier, purpose, system + "\nRespond with a single JSON object and nothing else.", user, max_tokens)
        return parse_json(text)


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences and chatter)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start = text.find("{")
    if start == -1:
        raise LLMError(f"No JSON in reply: {text[:200]}")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise LLMError(f"Unterminated JSON in reply: {text[:200]}")
