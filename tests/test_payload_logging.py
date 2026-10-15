import json
import logging

import httpx
import pytest

from app import create_app
from payloads import SSEAccumulator

PROMPT = {"model": "gpt-5", "stream": False, "max_output_tokens": 99,
          "input": [{"role": "user", "content": "hello-prompt"}],
          "tools": [{"type": "function"}, {"type": "function"}],
          "api_key": "sk-leakyleakyleaky"}


def events(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "openai-proxy" and r.getMessage().startswith("{")]


async def call(handler, body=PROMPT, **headers):
    app = create_app(transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return await client.post("/v1/responses", json=body, headers={
            "Authorization": "Bearer sk-secretsecretsecret", "User-Agent": "opencode/1.0", **headers})


def json_handler(payload, status=200, headers=None):
    return lambda request: httpx.Response(status, json=payload, headers=headers)


async def test_metadata_always_logged_but_content_is_not(caplog, monkeypatch):
    monkeypatch.delenv("LOG_BODIES", raising=False)
    caplog.set_level(logging.INFO, logger="openai-proxy")
    reply = {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 7},
             "output": [{"text": "hello-answer"}]}
    await call(json_handler(reply, headers={"x-request-id": "req_up1"}))
    records = events(caplog)
    assert [r["event"] for r in records] == ["request_started", "request_finished"]
    end = records[-1]
    assert end["model"] == "gpt-5" and end["stream_requested"] is False
    assert end["message_count"] == 1 and end["tool_count"] == 2 and end["max_tokens"] == 99
    assert end["request_bytes"] > 0 and end["user_agent"] == "opencode/1.0"
    assert end["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert end["finish_reason"] == "completed" and end["upstream_request_id"] == "req_up1"
    dump = json.dumps(records)
    assert "hello-prompt" not in dump and "hello-answer" not in dump and "sk-" not in dump


async def test_bodies_logged_redacted_and_ordered(caplog, monkeypatch):
    monkeypatch.setenv("LOG_BODIES", "both")
    caplog.set_level(logging.INFO, logger="openai-proxy")
    await call(json_handler({"status": "completed", "output": [{"text": "hello-answer"}]}))
    records = events(caplog)
    assert [r["event"] for r in records] == [
        "request_started", "request_body", "response_body", "request_finished"]
    assert len({r["request_id"] for r in records}) == 1
    req, resp = records[1], records[2]
    assert "hello-prompt" in req["body"] and req["truncated"] is False
    assert "[REDACTED]" in req["body"] and "sk-" not in req["body"]
    assert "hello-answer" in resp["body"] and resp["status"] == 200
    assert "Bearer" not in json.dumps(records) and "secretsecret" not in json.dumps(records)


async def test_request_mode_skips_response_body(caplog, monkeypatch):
    monkeypatch.setenv("LOG_BODIES", "request")
    caplog.set_level(logging.INFO, logger="openai-proxy")
    await call(json_handler({"status": "completed"}))
    assert [r["event"] for r in events(caplog)] == [
        "request_started", "request_body", "request_finished"]


async def test_bodies_are_truncated(caplog, monkeypatch):
    monkeypatch.setenv("LOG_BODIES", "request")
    monkeypatch.setenv("LOG_BODY_MAX_BYTES", "40")
    caplog.set_level(logging.INFO, logger="openai-proxy")
    await call(json_handler({}))
    req = events(caplog)[1]
    assert req["truncated"] is True and len(req["body"].encode()) <= 40
    assert req["original_bytes"] > 40


async def test_sse_usage_text_and_upstream_error(caplog, monkeypatch):
    monkeypatch.setenv("LOG_BODIES", "both")
    caplog.set_level(logging.INFO, logger="openai-proxy")
    sse = (b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
           b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
           b'data: {"choices":[],"usage":{"total_tokens":12}}\n\n'
           b"data: [DONE]\n\n")
    await call(lambda r: httpx.Response(200, content=sse,
                                        headers={"content-type": "text/event-stream"}))
    records = events(caplog)
    assert records[2]["text"] == "Hello"
    assert records[-1]["usage"] == {"total_tokens": 12}
    assert records[-1]["finish_reason"] == "stop" and records[-1]["is_stream"] is True

    caplog.clear()
    err = {"error": {"type": "rate_limit_exceeded", "message": "slow down sk-abcdefghijkl"}}
    await call(json_handler(err, 429, {"retry-after": "3", "x-ratelimit-remaining-requests": "0"}))
    end = events(caplog)[-1]
    assert end["status"] == 429 and end["retry_after"] == "3"
    assert end["ratelimit"] == {"x-ratelimit-remaining-requests": "0"}
    assert end["upstream_error"] == "rate_limit_exceeded: slow down sk-***"


async def test_unparseable_bodies_do_not_break_logging(caplog, monkeypatch):
    monkeypatch.setenv("LOG_BODIES", "both")
    caplog.set_level(logging.INFO, logger="openai-proxy")
    app = create_app(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xff\xfe")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/v1/x", content=b"not json")
    assert response.content == b"\xff\xfe"
    end = events(caplog)[-1]
    assert end["event"] == "request_finished" and end["model"] is None


def test_invalid_config_rejected(monkeypatch):
    monkeypatch.setenv("LOG_BODIES", "everything")
    with pytest.raises(ValueError, match="LOG_BODIES"):
        create_app()
    monkeypatch.setenv("LOG_BODIES", "off")
    monkeypatch.setenv("LOG_BODY_MAX_BYTES", "0")
    with pytest.raises(ValueError, match="LOG_BODY_MAX_BYTES"):
        create_app()


def test_sse_accumulator_handles_split_chunks_and_responses_api():
    acc = SSEAccumulator(limit=1000)
    raw = (b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
           b'data: {"type":"response.completed","response":{"status":"completed","usage":{"total_tokens":3}}}\n\n')
    for i in range(0, len(raw), 7):
        acc.feed(raw[i:i + 7])
    assert acc.text()[0] == "Hi"
    assert acc.usage == {"total_tokens": 3} and acc.finish_reason == "completed"
