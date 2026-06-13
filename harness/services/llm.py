"""OpenRouter HTTP transport for the chat completions endpoint.

Thin wrapper around `httpx.Client`. Returns a typed `ChatResponse` on 2xx;
raises typed `RateLimited` on 429 (so `LLMWorker` can swap models) and
`LLMTransportError` on any other transport failure (5xx, timeouts, conn
errors).

Spend logging is the LLMWorker's responsibility — this module is a transport
wrapper only. The API key is read from `OPENROUTER_API_KEY` at call time so
tests can monkeypatch the env between calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT = 60.0


@dataclass
class ChatResponse:
    """Parsed OpenRouter chat-completion response.

    `text` is the raw assistant message content (JSON envelope is parsed by
    the caller, not here). `cost_usd` is OpenRouter's `usage.total_cost` if
    present, else 0.0 (typical for `:free` models).
    """

    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    model_used: str


class RateLimited(Exception):
    """HTTP 429 from OpenRouter — model is rate-limited.

    Carries the model id so the LLMWorker can log the swap and the optional
    `Retry-After` header (seconds) when the server provides one.
    """

    def __init__(
        self,
        model: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"OpenRouter rate-limited model {model}")


class LLMTransportError(Exception):
    """Non-429 transport failure: 5xx, timeout, connection error.

    Caller (LLMWorker) converts this into a `tool_failed` alarm with
    `error_kind='transport'` and escalates.
    """


class OpenRouterClient:
    """Sync `httpx.Client` wrapper for OpenRouter's chat completions endpoint.

    `http_client` is injectable so tests can use `httpx.MockTransport` (or any
    custom client) without monkeypatching. When omitted, a fresh client is
    constructed per call so timeouts apply uniformly.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key_override = api_key
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._timeout = timeout

    def _api_key(self) -> str:
        """Resolve the API key: explicit-override > env. Read at call time."""
        if self._api_key_override is not None:
            return self._api_key_override
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise LLMTransportError(
                "OPENROUTER_API_KEY is not set; cannot call OpenRouter"
            )
        return key

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.2,
    ) -> ChatResponse:
        """POST /chat/completions.

        Raises:
          RateLimited: HTTP 429 — caller should consider swapping models.
          LLMTransportError: 5xx / timeout / connection error / missing key.

        On 2xx, returns a ChatResponse with the raw message content + usage.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://harness.local",
            "X-Title": "harness-v1",
        }
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if response_format is not None:
            body["response_format"] = response_format

        try:
            response = self._post(url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise LLMTransportError(
                f"OpenRouter transport failure for model {model}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError(
                f"OpenRouter HTTP error for model {model}: {exc}"
            ) from exc

        if response.status_code == 429:
            retry_after_raw = response.headers.get("Retry-After")
            retry_after: float | None = None
            if retry_after_raw is not None:
                try:
                    retry_after = float(retry_after_raw)
                except ValueError:
                    retry_after = None
            raise RateLimited(model, retry_after_seconds=retry_after)
        if response.status_code >= 500:
            raise LLMTransportError(
                f"OpenRouter {response.status_code} for model {model}:"
                f" {response.text[:200]}"
            )
        if response.status_code >= 400:
            # 4xx other than 429 — surface as transport error; not a swap-trigger.
            raise LLMTransportError(
                f"OpenRouter {response.status_code} for model {model}:"
                f" {response.text[:200]}"
            )

        return _parse_chat_response(response, requested_model=model)

    def _post(
        self,
        url: str,
        *,
        headers: dict,
        json: dict,
    ) -> httpx.Response:
        """Issue the POST. If a client was injected, reuse it; else one-shot."""
        if self._http_client is not None:
            return self._http_client.post(
                url, headers=headers, json=json, timeout=self._timeout
            )
        with httpx.Client(timeout=self._timeout) as client:
            return client.post(url, headers=headers, json=json)


def _parse_chat_response(
    response: httpx.Response,
    *,
    requested_model: str,
) -> ChatResponse:
    """Decode the JSON body into a ChatResponse.

    OpenRouter returns OpenAI-format with an additional `usage.total_cost`
    field for paid models. `:free` models omit it; we fall back to 0.0.
    Malformed bodies become LLMTransportError — better than a partial parse.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise LLMTransportError(
            f"OpenRouter returned non-JSON body: {response.text[:200]}"
        ) from exc

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LLMTransportError(
            f"OpenRouter response missing choices: {response.text[:200]}"
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise LLMTransportError(
            f"OpenRouter response missing message: {response.text[:200]}"
        )
    text = message.get("content")
    if not isinstance(text, str):
        raise LLMTransportError(
            f"OpenRouter response message.content not a string:"
            f" {response.text[:200]}"
        )

    usage_raw = body.get("usage")
    usage: dict = usage_raw if isinstance(usage_raw, dict) else {}
    tokens_in = int(usage.get("prompt_tokens", 0) or 0)
    tokens_out = int(usage.get("completion_tokens", 0) or 0)
    cost_raw = usage.get("total_cost")
    cost_usd = float(cost_raw) if isinstance(cost_raw, (int, float)) else 0.0

    model_used_raw = body.get("model")
    model_used = (
        model_used_raw if isinstance(model_used_raw, str) else requested_model
    )

    return ChatResponse(
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        model_used=model_used,
    )


__all__ = [
    "ChatResponse",
    "LLMTransportError",
    "OpenRouterClient",
    "RateLimited",
]
