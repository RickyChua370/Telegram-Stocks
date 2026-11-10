"""LLM client with a provider fallback chain (Gemini -> Groq -> OpenAI by default).

Design goals:
- Free tiers first. If one provider is rate-limited or errors, the next one is tried automatically.
- Two tiers: "fast" (bulk classification) and "smart" (final plain-English summaries).
- Everything returns parsed JSON; callers never see provider differences.
- If no provider works, `LLMUnavailable` is raised and sections fall back to raw (non-AI) output.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import requests

from .config import Settings

log = logging.getLogger("stockbot.llm")


class LLMUnavailable(Exception):
    """No configured provider could answer."""


@dataclass
class Provider:
    name: str
    kind: str                 # "gemini" | "openai"
    api_key: str
    fast_model: str
    smart_model: str
    base_url: str = ""
    max_input_chars: int = 100_000
    min_interval: float = 0.0  # seconds between calls, to stay inside free-tier RPM limits


def _build_providers(s: Settings) -> list[Provider]:
    catalogue = {
        "gemini": Provider("gemini", "gemini", s.gemini_key, s.gemini_fast, s.gemini_smart,
                           max_input_chars=120_000, min_interval=6.5),
        "groq": Provider("groq", "openai", s.groq_key, s.groq_model, s.groq_model,
                         base_url="https://api.groq.com/openai/v1", max_input_chars=16_000, min_interval=3.0),
        "openai": Provider("openai", "openai", s.openai_key, s.openai_model, s.openai_model,
                           base_url="https://api.openai.com/v1", max_input_chars=100_000, min_interval=0.5),
    }
    return [catalogue[n] for n in s.provider_order if n in catalogue and catalogue[n].api_key]


def parse_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class LLMClient:
    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self._last_call: dict[str, float] = {}
        self._gemini_models: dict[str, str] = {}
        self.calls = 0
        self.failures: list[str] = []

    @classmethod
    def from_settings(cls, s: Settings) -> "LLMClient":
        return cls(_build_providers(s))

    @property
    def available(self) -> bool:
        return bool(self.providers)

    # ------------------------------------------------------------------
    def complete_json(self, system: str, user: str, *, tier: str = "fast", max_tokens: int = 4096):
        """Return parsed JSON from the first provider that works."""
        if not self.providers:
            raise LLMUnavailable("no LLM API key configured")
        errors: list[str] = []
        for prov in self.providers:
            try:
                raw = self._call_with_retry(prov, system, user, tier, max_tokens)
                try:
                    return parse_json(raw)
                except (json.JSONDecodeError, ValueError):
                    fixed = self._call_with_retry(
                        prov, system,
                        "Your previous reply was not valid JSON. Reply again with ONLY the valid JSON object, "
                        "no commentary.\n\nPrevious reply:\n" + (raw or "")[:6000],
                        tier, max_tokens)
                    return parse_json(fixed)
            except Exception as exc:  # noqa: BLE001 - we deliberately fall through to the next provider
                msg = f"{prov.name}: {type(exc).__name__}: {str(exc)[:160]}"
                log.warning("LLM provider failed -> %s", msg)
                errors.append(msg)
        self.failures.extend(errors)
        raise LLMUnavailable("; ".join(errors))

    # ------------------------------------------------------------------
    def _call_with_retry(self, prov: Provider, system: str, user: str, tier: str, max_tokens: int) -> str:
        if len(user) > prov.max_input_chars:
            log.info("%s: truncating prompt %d -> %d chars", prov.name, len(user), prov.max_input_chars)
            user = user[: prov.max_input_chars]
        last: Exception | None = None
        for attempt in range(3):
            self._pace(prov)
            try:
                self.calls += 1
                if prov.kind == "gemini":
                    return self._gemini(prov, system, user, tier, max_tokens)
                return self._openai(prov, system, user, tier, max_tokens)
            except _Retryable as exc:
                last = exc
                time.sleep(min(exc.wait or 8 * (attempt + 1), 30))
        raise last or RuntimeError("retries exhausted")

    def _pace(self, prov: Provider) -> None:
        wait = prov.min_interval - (time.monotonic() - self._last_call.get(prov.name, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_call[prov.name] = time.monotonic()

    # ---- Gemini ---------------------------------------------------------
    def _gemini(self, prov: Provider, system: str, user: str, tier: str, max_tokens: int) -> str:
        model = self._gemini_models.get(tier) or (prov.smart_model if tier == "smart" else prov.fast_model)
        for _ in range(2):  # second pass after model auto-discovery
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": max(max_tokens, 2048),
                                     "responseMimeType": "application/json"},
            }
            resp = _post(url, body, headers={"x-goog-api-key": prov.api_key})
            if resp.status_code == 404:
                found = self._discover_gemini_model(prov, tier)
                if found and found != model:
                    log.warning("Gemini model %s unavailable; using %s", model, found)
                    model = found
                    self._gemini_models[tier] = found
                    continue
            _raise_for_llm(resp)
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                raise RuntimeError(f"empty response: {str(data.get('promptFeedback'))[:120]}")
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        raise RuntimeError("no usable Gemini model found")

    def _discover_gemini_model(self, prov: Provider, tier: str) -> str | None:
        names = list_gemini_models(prov.api_key)
        flash = [n for n in names if "flash" in n and not any(x in n for x in ("image", "tts", "live", "audio", "preview-"))]
        if tier == "fast":
            flash.sort(key=lambda n: (0 if "lite" in n else 1, n))
        else:
            flash.sort(key=lambda n: (1 if "lite" in n else 0, n))
        return flash[0] if flash else None

    # ---- OpenAI-compatible (Groq, OpenAI) -------------------------------
    def _openai(self, prov: Provider, system: str, user: str, tier: str, max_tokens: int) -> str:
        url = prov.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": prov.smart_model if tier == "smart" else prov.fast_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {prov.api_key}"}
        resp = _post(url, body, headers=headers)
        if resp.status_code == 400 and "response_format" in resp.text:
            body.pop("response_format")
            resp = _post(url, body, headers=headers)
        _raise_for_llm(resp)
        return resp.json()["choices"][0]["message"]["content"] or ""


class _Retryable(Exception):
    def __init__(self, msg: str, wait: float = 0.0):
        super().__init__(msg)
        self.wait = wait


def _post(url: str, body: dict, headers: dict) -> requests.Response:
    try:
        return requests.post(url, json=body, headers=headers, timeout=90)
    except requests.RequestException as exc:
        raise _Retryable(f"network error: {type(exc).__name__}") from exc


def _raise_for_llm(resp: requests.Response) -> None:
    if resp.status_code < 400:
        return
    detail = resp.text[:200].replace("\n", " ")
    if resp.status_code == 429 or resp.status_code >= 500:
        try:
            wait = float(resp.headers.get("retry-after", 0))
        except ValueError:
            wait = 0.0
        raise _Retryable(f"HTTP {resp.status_code}: {detail}", wait)
    raise RuntimeError(f"HTTP {resp.status_code}: {detail}")


def list_gemini_models(api_key: str) -> list[str]:
    resp = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                        headers={"x-goog-api-key": api_key}, params={"pageSize": 200}, timeout=30)
    resp.raise_for_status()
    out = []
    for m in resp.json().get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            out.append(m["name"].split("/", 1)[-1])
    return out
