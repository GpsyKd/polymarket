"""Minimal OpenAI-compatible chat client (works with xAI/Grok, OpenAI, etc.).

Uses httpx directly — no extra SDK dependency — so provider-specific extras like
xAI Live Search (`search_parameters`) are trivial passthroughs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def resolve_api_key(settings: Any) -> str | None:
    """LLM key precedence: explicit llm_api_key > grok_api_key > env XAI/GROK."""
    return (
        settings.llm_api_key
        or settings.grok_api_key
        or os.environ.get("XAI_API_KEY")
        or os.environ.get("GROK_API_KEY")
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort: parse the first {...} block out of a possibly-noisy reply."""
    if not text:
        return None
    match = _JSON_OBJ.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


class LLMClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete_json(
        self,
        system: str,
        user: str,
        model: str,
        *,
        live_search: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 900,
    ) -> dict[str, Any] | None:
        """One chat completion expecting a JSON object back. Returns None on any failure."""
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if live_search:
            body["search_parameters"] = {"mode": "auto"}

        content = None
        for attempt in range(3):
            try:
                r = await self._client.post("/chat/completions", json=body)
                if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # back off on rate-limit / transient 5xx
                    continue
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                break
            except httpx.HTTPStatusError as e:
                log.warning("LLM %s HTTP %s: %s", model, e.response.status_code, e.response.text[:200])
                return None
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                log.warning("LLM %s call failed: %s", model, e)
                return None
        if content is None:
            return None
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return _extract_json(content)
