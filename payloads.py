"""Pure helpers that summarise and sanitise request/response payloads for logs."""

import json
import re

SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "key", "password", "secret",
                  "token", "access_token", "client_secret"}
_SECRET = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
MAX_ERROR_CHARS = 500


def mask(text):
    return _SECRET.sub("sk-***", text)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def clip(text, limit):
    """Return (text cut to `limit` UTF-8 bytes, truncated, original byte length)."""
    raw = text.encode()
    if len(raw) <= limit:
        return text, False, len(raw)
    return raw[:limit].decode(errors="ignore"), True, len(raw)


def render_body(data, complete, total, limit):
    """Sanitised, size-limited text of a captured body: (text, truncated, original_bytes).

    `complete` says whether `data` holds the whole body; `total` is its real size."""
    text = None
    if complete:
        try:
            text = json.dumps(redact(json.loads(data)), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            pass
    if text is None:
        text = data.decode(errors="replace")
    text, truncated, _ = clip(mask(text), limit)
    return text, truncated or not complete, total


def _load_dict(data):
    try:
        body = json.loads(data)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_text(err):
    if isinstance(err, dict):
        err = f'{err.get("type")}: {err.get("message")}'
    return mask(err)[:MAX_ERROR_CHARS] if isinstance(err, str) else None


def summarize_request(data):
    body = _load_dict(data)
    items = body.get("messages", body.get("input"))
    tools = body.get("tools")
    return {
        "model": body.get("model"),
        "stream_requested": body.get("stream"),
        "message_count": len(items) if isinstance(items, list) else None,
        "tool_count": len(tools) if isinstance(tools, list) else None,
        "max_tokens": next((body[k] for k in ("max_output_tokens", "max_completion_tokens", "max_tokens")
                            if k in body), None),
    }


def summarize_response(data, status):
    body = _load_dict(data)
    choices = body.get("choices")
    finish = (choices[0].get("finish_reason")
              if isinstance(choices, list) and choices and isinstance(choices[0], dict)
              else body.get("status"))
    usage = body.get("usage")
    return {
        "usage": usage if isinstance(usage, dict) else None,
        "finish_reason": finish,
        "upstream_error": _error_text(body.get("error")) if status >= 400 else None,
    }


class SSEAccumulator:
    """Incrementally digests an SSE stream (chat completions or responses API)
    into final text, usage and finish reason, keeping only `limit` bytes of text."""

    def __init__(self, limit):
        self.limit = limit
        self.buffer = b""
        self.parts = []
        self.kept = 0
        self.text_bytes = 0
        self.usage = None
        self.finish_reason = None
        self.error = None

    def feed(self, chunk):
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload and payload != b"[DONE]":
                try:
                    self._event(json.loads(payload))
                except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                    pass

    def _add_text(self, text):
        if not isinstance(text, str) or not text:
            return
        size = len(text.encode())
        self.text_bytes += size
        if self.kept < self.limit:
            self.parts.append(text)
            self.kept += size

    def _event(self, obj):
        kind = obj.get("type")
        if kind == "response.output_text.delta":
            self._add_text(obj.get("delta"))
        response = obj.get("response")
        if isinstance(response, dict):
            self.usage = response.get("usage") or self.usage
            self.finish_reason = response.get("status") or self.finish_reason
            self.error = _error_text(response.get("error")) or self.error
        for choice in obj.get("choices") or []:
            self._add_text((choice.get("delta") or {}).get("content"))
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        if kind == "error" or isinstance(obj.get("error"), dict):
            self.error = _error_text(obj.get("error")) or self.error

    def text(self):
        text, truncated, _ = clip(mask("".join(self.parts)), self.limit)
        return text, truncated or self.text_bytes > self.kept, self.text_bytes

    def summary(self):
        return {"usage": self.usage, "finish_reason": self.finish_reason,
                "upstream_error": self.error}
