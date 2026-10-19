"""A model client for any server that speaks the OpenAI chat-completions API.

That is what vLLM, SGLang and llama.cpp's server expose, so this is how the
judge reaches an open model the campaign runs on its own GPU. It is not meant
for a commercial API: contributor photos go wherever `base_url` points, so the
URL has to be a server the campaign controls (D5 in #43).

Standard library only, like the Solana client: one POST per call, no SDK.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from .prompt import ImagePart, Prompt, TextPart

#: Sends one request and returns the decoded JSON body. Swapped out in tests.
Transport = Callable[[str, dict, dict, float], dict]

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ChatClientError(RuntimeError):
    """The server did not return a usable answer. The judge holds the item."""


def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # The status only: the body may echo the request, photo included.
        raise ChatClientError(f"model server answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ChatClientError(f"model server unreachable: {type(exc).__name__}") from None


class ChatCompletionsClient:
    """Sends a judge `Prompt` as structured chat messages and returns the reply.

    The system instructions go in the system message and each prompt part in
    the user message, never spliced into one template string (the ModelClient
    rules in judge.py). Sampling is fixed per client and is part of `name`, so
    provenance shows which settings produced a verdict.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 300,
        timeout: float = 60.0,
        json_mode: bool = True,
        transport: Transport = _post,
    ):
        parts = urlsplit(base_url)
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
            # The photo and the description travel in this request.
            raise ValueError("base_url must be https, or http to this machine only")
        if not model:
            raise ValueError("model must name the served model")
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        if max_tokens < 1 or timeout <= 0:
            raise ValueError("max_tokens and timeout must be positive")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._json_mode = json_mode
        self._transport = transport
        self.name = f"{model}@temperature={temperature:g}"

    def __repr__(self) -> str:
        # Never the key.
        return f"ChatCompletionsClient(url={self._url!r}, name={self.name!r})"

    def complete(self, prompt: Prompt) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": [_part(p) for p in prompt.content]},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        answer = self._transport(self._url, headers, body, self._timeout)
        try:
            content = answer["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ChatClientError("model server reply has no message") from None
        # Anything but text (None when output was filtered) is passed on as
        # is: parse_reply turns it into a malformed reply, which is retried.
        return content


def _part(part: TextPart | ImagePart) -> dict:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    encoded = base64.b64encode(part.data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{part.mime_type};base64,{encoded}"}}
