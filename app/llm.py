"""Minimal Gemini API client (REST, no SDK version lock-in)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

_PREFERRED_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-2.0-flash",
]


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    raw_text: str


def _strip_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


class GeminiClient:
    def __init__(self, api_key: str, model: str | None = None, timeout: float = 50.0):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is empty")
        self.api_key = api_key
        self.model = (model or "").strip() or "gemini-3.5-flash-lite"
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
        self._model_checked = False
        self._candidates: list[str] | None = None
        # Lightweight, in-process counters for /stats and admin warnings.
        # Google does not expose the remaining daily quota, so 429 responses
        # are the only reliable signal that a limit is being reached.
        self.stats: dict[str, Any] = {
            "requests": 0,
            "successes": 0,
            "rate_limited": 0,
            "errors": 0,
            "last_error": None,
            "last_rate_limit": None,
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        # Send the key in a header, not in the URL query string, so it cannot
        # leak through proxy/access logs or httpx request logging.
        r = await self._client.get(
            f"{API_BASE}{path}",
            params=params,
            headers={"x-goog-api-key": self.api_key},
        )
        if r.status_code >= 400:
            raise LLMError(f"Gemini GET {path} failed: {r.status_code} {r.text[:300]}")
        return r.json()

    async def list_models(self) -> list[str]:
        data = await self._get("/models", {"pageSize": 1000})
        names = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = (m.get("name") or "").removeprefix("models/")
            if name:
                names.append(name)
        return names

    async def ensure_model(self) -> str:
        if self._model_checked:
            return self.model
        self._model_checked = True
        try:
            available = await self.list_models()
        except Exception as exc:  # network / permission issue: try the configured model
            log.warning("Could not list Gemini models: %s", exc)
            self._candidates = [self.model]
            return self.model
        if not available:
            self._candidates = [self.model]
            return self.model

        ordered: list[str] = []
        if self.model in available:
            ordered.append(self.model)
        for candidate in _PREFERRED_MODELS:
            if candidate in available and candidate not in ordered:
                ordered.append(candidate)
        for name in available:
            if (
                "flash" in name
                and name not in ordered
                and not any(x in name for x in ("embedding", "image", "tts", "vision", "thinking"))
            ):
                ordered.append(name)
        if not ordered:
            ordered = [self.model] + [m for m in available if m not in {self.model}][:2]
        # Keep failover short: each extra model costs one request on 429.
        self._candidates = ordered[:4]
        if self._candidates[0] != self.model:
            log.warning("Configured model %s unavailable; using %s", self.model, self._candidates[0])
        self.model = self._candidates[0]
        log.info("Gemini model candidates: %s", ", ".join(self._candidates))
        return self.model

    async def generate_json(
        self,
        system: str,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> LLMResult:
        await self.ensure_model()
        models = self._candidates or [self.model]
        last_error: Exception | None = None

        for model_index, model in enumerate(models):
            body: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "topP": 0.1,
                    "maxOutputTokens": max_output_tokens,
                    "responseMimeType": "application/json",
                },
            }
            url = f"{API_BASE}/models/{model}:generateContent"
            for attempt in range(2):
                try:
                    self.stats["requests"] += 1
                    r = await self._client.post(
                        url,
                        headers={"x-goog-api-key": self.api_key},
                        json=body,
                    )

                    if r.status_code == 429:
                        detail = (r.text or "")[:260].replace("\n", " ")
                        log.warning("Gemini 429 on %s: %s", model, detail)
                        self.stats["rate_limited"] += 1
                        self.stats["last_rate_limit"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        last_error = LLMError(f"rate limited on {model}")
                        # A daily quota cannot be fixed by waiting; a per-minute
                        # quota can be avoided by trying the next model.
                        break
                    if r.status_code == 404:
                        log.warning("Gemini model %s unavailable: %s", model, (r.text or "")[:200])
                        last_error = LLMError(f"model {model} unavailable")
                        break
                    if r.status_code >= 500:
                        wait = 1.5 * (attempt + 1)
                        log.warning("Gemini %s on %s; retrying in %.1fs", r.status_code, model, wait)
                        await asyncio.sleep(wait)
                        last_error = LLMError(f"Gemini server error {r.status_code} on {model}")
                        continue
                    if r.status_code >= 400:
                        detail = r.text[:500]
                        raise LLMError(f"Gemini HTTP {r.status_code} on {model}: {detail}")

                    data = r.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        block = (data.get("promptFeedback") or {}).get("blockReason")
                        raise LLMError(f"Gemini returned no candidates (blockReason={block})")
                    cand = candidates[0]
                    finish = cand.get("finishReason")
                    parts = (cand.get("content") or {}).get("parts") or []
                    text = "\n".join(p.get("text", "") for p in parts if p.get("text"))
                    if not text.strip():
                        raise LLMError(f"Gemini returned empty text (finishReason={finish}, model={model})")
                    parsed = json.loads(_strip_json(text))
                    if not isinstance(parsed, dict):
                        raise LLMError("Gemini JSON is not an object")
                    if model_index > 0:
                        log.info("Gemini answered with fallback model %s", model)
                        self.model = model
                    self.stats["successes"] += 1
                    return LLMResult(data=parsed, model=model, raw_text=text)

                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = exc
                    log.warning("Gemini network error on %s (attempt %s): %s", model, attempt + 1, exc)
                    await asyncio.sleep(1.0 * (attempt + 1))
                except json.JSONDecodeError as exc:
                    last_error = exc
                    log.warning("Gemini returned invalid JSON on %s (attempt %s): %s", model, attempt + 1, exc)
                    # One repair attempt with a stricter instruction.
                    body["contents"][0]["parts"][0]["text"] = (
                        prompt + "\n\nIMPORTANT: Return ONLY a valid JSON object. No markdown, no prose."
                    )
                    await asyncio.sleep(0.5)

        self.stats["errors"] += 1
        self.stats["last_error"] = str(last_error)[:300]
        raise LLMError(f"Gemini failed after retries: {last_error}")
