from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class LLMProviderError(RuntimeError):
    """Raised when an LLM provider cannot be reached or returns a non-OK status."""


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    provider: str
    raw: dict[str, Any]


class LLMProvider(Protocol):
    name: str

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        timeout_seconds: float,
    ) -> ProviderResponse: ...


class MockProvider:
    """A deterministic in-process provider used by tests.

    The handler callable receives the system and user prompt and the model name and
    should return a string. The default handler returns a syntactically valid JSON
    response with decision="needs_human" so that nothing crashes if a test forgets
    to register a handler.
    """

    name = "mock"

    def __init__(
        self,
        handler: Callable[[str, str, str], str] | None = None,
    ) -> None:
        self._handler = handler or _default_mock_handler
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        timeout_seconds: float,
    ) -> ProviderResponse:
        text = self._handler(system_prompt, user_prompt, model)
        call = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "model": model,
            "temperature": temperature,
            "timeout_seconds": timeout_seconds,
            "response_text": text,
        }
        self.calls.append(call)
        return ProviderResponse(
            text=text,
            model=model or "mock",
            provider=self.name,
            raw={"mock_call_index": len(self.calls) - 1, "text": text},
        )


def _default_mock_handler(system_prompt: str, user_prompt: str, model: str) -> str:
    payload = {
        "decision": "needs_human",
        "category_correct": False,
        "item_type_correct": False,
        "price_correct": None,
        "is_bundle": False,
        "is_context_false_positive": False,
        "is_non_target_room": False,
        "corrected": {},
        "normalized_terms": {"facade_materials": []},
        "rationale_short": "mock default response",
        "confidence": "low",
    }
    return json.dumps(payload, ensure_ascii=False)


class LMStudioProvider:
    """Minimal OpenAI-compatible client for LM Studio (no extra deps)."""

    name = "lmstudio"

    def __init__(self, base_url: str, *, opener: Any | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._opener = opener or urllib.request

    def probe(self, *, timeout_seconds: float = 5.0) -> None:
        """Verify the server is reachable. Raises LLMProviderError if not."""
        endpoint = f"{self.base_url}/models"
        request = urllib.request.Request(endpoint, method="GET")
        try:
            with self._opener.urlopen(request, timeout=timeout_seconds) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise LLMProviderError(
                f"LM Studio HTTP {exc.code} at {endpoint}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMProviderError(
                f"LM Studio not reachable at {endpoint}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise LLMProviderError(
                f"LM Studio probe of {endpoint} timed out after {timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise LLMProviderError(
                f"LM Studio connection error at {endpoint}: {exc}"
            ) from exc

    def chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        timeout_seconds: float,
    ) -> ProviderResponse:
        endpoint = f"{self.base_url}/chat/completions"
        body = {
            "model": model or "local-model",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(temperature),
            "stream": False,
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.urlopen(request, timeout=timeout_seconds) as response:
                status = getattr(response, "status", 200)
                raw_bytes = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise LLMProviderError(
                f"LM Studio HTTP {exc.code} at {endpoint}: {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMProviderError(
                f"LM Studio not reachable at {endpoint}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise LLMProviderError(
                f"LM Studio request to {endpoint} timed out after {timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise LLMProviderError(
                f"LM Studio connection error at {endpoint}: {exc}"
            ) from exc

        if status >= 400:
            raise LLMProviderError(
                f"LM Studio returned HTTP {status} at {endpoint}"
            )

        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMProviderError(
                f"LM Studio response was not valid JSON: {exc}"
            ) from exc

        text = _extract_text_from_chat_completion(payload)
        return ProviderResponse(
            text=text,
            model=str(payload.get("model") or model or "local-model"),
            provider=self.name,
            raw=payload,
        )


def _extract_text_from_chat_completion(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProviderError("LM Studio response had no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMProviderError("LM Studio response choice was not an object")
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    parts.append(value)
        if parts:
            return "".join(parts)
    text = first.get("text")
    if isinstance(text, str):
        return text
    raise LLMProviderError("LM Studio response had no textual content")


def build_provider(
    provider_name: str,
    *,
    base_url: str,
    mock_handler: Callable[[str, str, str], str] | None = None,
) -> LLMProvider:
    if provider_name == "mock":
        return MockProvider(handler=mock_handler)
    if provider_name == "lmstudio":
        return LMStudioProvider(base_url=base_url)
    raise ValueError(f"Unknown provider: {provider_name}")
