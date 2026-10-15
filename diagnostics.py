"""Query-free JSON request diagnostics without buffering response bodies."""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from starlette.requests import ClientDisconnect

from payloads import SSEAccumulator, render_body, summarize_request, summarize_response

logger = logging.getLogger("openai-proxy")


def configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


PARSE_CAP = 1024 * 1024  # bytes captured per body to extract metadata


def body_config():
    mode = os.getenv("LOG_BODIES", "off").lower()
    if mode not in {"off", "request", "both"}:
        raise ValueError("LOG_BODIES must be off, request, or both")
    try:
        limit = int(os.getenv("LOG_BODY_MAX_BYTES", "8192"))
    except ValueError:
        limit = 0
    if limit < 1:
        raise ValueError("LOG_BODY_MAX_BYTES must be a positive integer")
    return mode, limit


class _Capture:
    """Copies the first PARSE_CAP bytes of a body while counting all of it."""

    def __init__(self):
        self.data = bytearray()
        self.total = 0

    def add(self, chunk):
        self.total += len(chunk)
        room = PARSE_CAP - len(self.data)
        if room > 0:
            self.data += chunk[:room]

    @property
    def complete(self):
        return self.total == len(self.data)


def log_event(event, level=logging.INFO, **fields):
    logger.log(level, json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": logging.getLevelName(level), "pid": os.getpid(), "event": event,
        **fields,
    }, separators=(",", ":")))


class RequestDiagnostics:
    def __init__(self, app):
        self.app = app
        self.active_requests = 0
        self.body_mode, self.body_limit = body_config()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = time.perf_counter()
        request_headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
        fields = {
            "request_id": uuid.uuid4().hex,
            "method": scope["method"], "path": scope["path"],
            "client_ip": (request_headers.get("x-real-ip")
                          or (scope.get("client") or [None])[0]),
            "user_agent": request_headers.get("user-agent"),
            "content_type": request_headers.get("content-type"),
        }
        req_capture, resp_capture = _Capture(), _Capture()
        sse = SSEAccumulator(self.body_limit)
        metrics = {"started": started, "upstream_headers_ms": None,
                   "upstream_status": None, "error_type": None, "outcome": None}
        scope.setdefault("state", {})["diagnostics"] = metrics
        self.active_requests += 1
        log_event("request_started", **fields, active_requests=self.active_requests)
        status = None
        is_stream = False
        first_body_byte_ms = None
        response_bytes = 0
        disconnected = False
        completed = False

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            elif message["type"] == "http.request":
                req_capture.add(message.get("body", b""))
            return message

        async def observed_send(message):
            nonlocal status, is_stream, first_body_byte_ms, response_bytes, completed
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"x-proxy-request-id"]
                headers.append((b"x-proxy-request-id", fields["request_id"].encode()))
                is_stream = any(k.lower() == b"content-type" and
                                v.lower().split(b";", 1)[0].strip() == b"text/event-stream"
                                for k, v in headers)
                message = {**message, "headers": headers}
            try:
                await send(message)
            except OSError as exc:
                disconnected = True
                metrics["outcome"] = "client_disconnected"
                metrics["error_type"] = type(exc).__name__
                raise
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                response_bytes += len(body)
                if is_stream:
                    sse.feed(body)
                else:
                    resp_capture.add(body)
                if body and first_body_byte_ms is None:
                    first_body_byte_ms = (time.perf_counter() - started) * 1000
                if not message.get("more_body", False):
                    completed = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            metrics["outcome"] = metrics["outcome"] or "cancelled"
            raise
        except ClientDisconnect:
            metrics["outcome"] = "client_disconnected"
            metrics["error_type"] = "ClientDisconnect"
            raise
        except Exception as exc:
            metrics["outcome"] = metrics["outcome"] or "application_error"
            metrics["error_type"] = metrics["error_type"] or type(exc).__name__
            raise
        finally:
            self.active_requests -= 1
            outcome = metrics["outcome"]
            if disconnected and not completed and outcome in {None, "cancelled"}:
                outcome = "client_disconnected"
            if outcome is None:
                outcome = ("http_error" if status is not None and status >= 400 else
                           "completed" if completed else "incomplete")
            extra, body_events = {}, []
            try:
                extra, body_events = self._payload_logs(
                    fields["request_id"], status, is_stream, req_capture, resp_capture, sse, metrics)
            except Exception:  # logging must never break request handling
                pass
            for name, payload in body_events:
                log_event(name, request_id=fields["request_id"], **payload)
            level = logging.ERROR if outcome in {"stream_error", "application_error"} else (
                logging.WARNING if outcome != "completed" else logging.INFO)
            log_event("request_finished", level=level, **fields,
                      active_requests=self.active_requests, status=status,
                      upstream_status=metrics["upstream_status"],
                      upstream_headers_ms=metrics["upstream_headers_ms"],
                      first_body_byte_ms=first_body_byte_ms,
                      duration_ms=(time.perf_counter() - started) * 1000,
                      response_bytes=response_bytes, is_stream=is_stream,
                      outcome=outcome, error_type=metrics["error_type"],
                      request_bytes=req_capture.total,
                      upstream_request_id=metrics.get("upstream_request_id"),
                      retry_after=metrics.get("retry_after"),
                      ratelimit=metrics.get("ratelimit") or None, **extra)

    def _payload_logs(self, request_id, status, is_stream, req, resp, sse, metrics):
        extra = {**summarize_request(bytes(req.data)), "usage": None,
                 "finish_reason": None, "upstream_error": None}
        events = []
        if status is not None:
            extra.update(sse.summary() if is_stream else summarize_response(bytes(resp.data), status))
        if self.body_mode in {"request", "both"} and req.total:
            body, truncated, size = render_body(bytes(req.data), req.complete, req.total, self.body_limit)
            events.append(("request_body", {"body": body, "truncated": truncated,
                                            "original_bytes": size}))
        if self.body_mode == "both" and status is not None:
            if is_stream:
                text, truncated, size = sse.text()
                payload = {"text": text}
            elif resp.total:
                text, truncated, size = render_body(bytes(resp.data), resp.complete, resp.total,
                                                    self.body_limit)
                payload = {"body": text}
            else:
                return extra, events
            events.append(("response_body", {"status": status, **payload,
                                             "truncated": truncated, "original_bytes": size}))
        return extra, events
