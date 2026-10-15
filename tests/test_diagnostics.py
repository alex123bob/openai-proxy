import asyncio
import json
import logging

import httpx
import pytest

from app import create_app


def events(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "openai-proxy" and r.getMessage().startswith("{")]


async def test_sse_diagnostics_and_privacy(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")

    async def handler(request):
        async def chunks():
            yield b"data: secret-response\n\n"
            yield b"data: done\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=chunks())

    app = create_app(transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/v1/responses?token=secret-query",
                                     headers={"Authorization": "Bearer secret-key",
                                              "Cookie": "secret-cookie",
                                              "X-Proxy-Request-ID": "untrusted-id"},
                                     content=b"secret-body")
    records = events(caplog)
    assert [r["event"] for r in records] == ["request_started", "request_finished"]
    start, end = records
    assert start["request_id"] == end["request_id"] == response.headers["x-proxy-request-id"]
    assert start["request_id"] != "untrusted-id"
    assert end["path"] == "/v1/responses"
    assert end["status"] == end["upstream_status"] == 200
    assert end["outcome"] == "completed" and end["is_stream"] is True
    assert end["response_bytes"] == len(response.content)
    assert 0 <= end["upstream_headers_ms"] <= end["first_body_byte_ms"] <= end["duration_ms"]
    assert start["active_requests"] == 1 and end["active_requests"] == 0
    assert end["timestamp"].endswith("Z") and end["pid"] > 0
    assert "secret" not in json.dumps(records)


async def test_pool_timeout_is_correlated_and_sanitized(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")

    def handler(request):
        raise httpx.PoolTimeout("secret-url-and-key")

    app = create_app(transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/v1/models")
    end = events(caplog)[-1]
    assert response.status_code == end["status"] == 502
    assert end["request_id"] == response.headers["x-proxy-request-id"]
    assert end["outcome"] == "upstream_error" and end["error_type"] == "PoolTimeout"
    assert end["upstream_headers_ms"] is None and end["active_requests"] == 0
    assert "secret" not in json.dumps(events(caplog))


async def test_stream_failure_logged_and_closed(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    closed = asyncio.Event()

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("secret-error")

        async def aclose(self):
            closed.set()

    app = create_app(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=BrokenStream())))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                 base_url="http://test") as client:
        await client.get("/v1/responses")
    end = events(caplog)[-1]
    assert end["status"] == 200 and end["outcome"] == "stream_error"
    assert end["error_type"] == "ReadError" and end["response_bytes"] == 7
    assert end["active_requests"] == 0 and closed.is_set()
    assert len([r for r in events(caplog) if r["event"] == "request_finished"]) == 1


async def test_concurrent_requests_have_independent_ids_and_counts(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    both_entered = asyncio.Event()
    entered = 0

    async def handler(request):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        return httpx.Response(200, content=b"ok")

    app = create_app(transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        await asyncio.gather(client.get("/one"), client.get("/two"))
    records = events(caplog)
    starts = [r for r in records if r["event"] == "request_started"]
    ends = [r for r in records if r["event"] == "request_finished"]
    assert len(starts) == len(ends) == 2
    assert len({r["request_id"] for r in starts}) == 2
    assert [r["active_requests"] for r in starts] == [1, 2]
    assert sorted(r["active_requests"] for r in ends) == [0, 1]


async def test_disconnect_closes_upstream_and_logs_once(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    disconnected = asyncio.Event()
    closed = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    app = create_app(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=SlowStream())))

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            disconnected.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "GET", "scheme": "http", "path": "/v1/responses",
             "raw_path": b"/v1/responses", "query_string": b"", "headers": [],
             "server": ("test", 80), "client": ("127.0.0.1", 1234)}
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    ends = [r for r in events(caplog) if r["event"] == "request_finished"]
    assert len(ends) == 1 and ends[0]["outcome"] == "client_disconnected"
    assert ends[0]["response_bytes"] == 5 and ends[0]["active_requests"] == 0
    assert closed.is_set()


async def test_shutdown_closes_shared_client():
    closed = asyncio.Event()

    class Transport(httpx.MockTransport):
        async def aclose(self):
            closed.set()

    app = create_app(transport=Transport(lambda r: httpx.Response(200)))
    async with app.router.lifespan_context(app):
        assert not closed.is_set()
    assert closed.is_set()


async def test_upstream_429_remains_http_error(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    app = create_app(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"error": "rate limited"})))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/v1/models")
    end = events(caplog)[-1]
    assert response.status_code == end["upstream_status"] == end["status"] == 429
    assert end["outcome"] == "http_error" and end["error_type"] is None


async def test_send_disconnect_is_not_application_error(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data"

        async def aclose(self):
            closed.set()

    app = create_app(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Stream())))

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client socket closed")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "GET", "scheme": "http", "path": "/v1/responses",
             "raw_path": b"/v1/responses", "query_string": b"", "headers": [],
             "server": ("test", 80), "client": ("127.0.0.1", 1234)}
    with pytest.raises(Exception):
        await app(scope, receive, send)
    end = events(caplog)[-1]
    assert end["outcome"] == "client_disconnected" and end["active_requests"] == 0
    assert end["response_bytes"] == 0 and closed.is_set()


async def test_disconnect_during_async_eof_cleanup(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    close_started = asyncio.Event()
    close_completed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data"

        async def aclose(self):
            close_started.set()
            await asyncio.sleep(0.01)
            close_completed.set()

    app = create_app(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Stream())))

    async def receive():
        await close_started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "GET", "scheme": "http", "path": "/v1/responses",
             "raw_path": b"/v1/responses", "query_string": b"", "headers": [],
             "server": ("test", 80), "client": ("127.0.0.1", 1234)}
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    assert close_completed.is_set()


async def test_response_construction_failure_closes_upstream(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data"

        async def aclose(self):
            closed.set()

    app = create_app(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers=[(b"x-label", "☃".encode())], stream=Stream())))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                 base_url="http://test") as client:
        response = await client.get("/v1/models")
    assert closed.is_set()
    end = events(caplog)[-1]
    assert response.status_code == end["status"] == 500
    assert response.headers["x-proxy-request-id"] == end["request_id"]
    assert end["response_bytes"] == len(response.content)


async def test_upload_disconnect_is_classified(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    app = create_app(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "POST", "scheme": "http", "path": "/v1/responses",
             "raw_path": b"/v1/responses", "query_string": b"",
             "headers": [(b"content-length", b"100")],
             "server": ("test", 80), "client": ("127.0.0.1", 1234)}
    with pytest.raises(Exception):
        await app(scope, receive, send)
    end = events(caplog)[-1]
    assert end["outcome"] == "client_disconnected"
    assert end["error_type"] == "ClientDisconnect" and end["active_requests"] == 0


async def test_cancelled_request_releases_active_count(caplog):
    caplog.set_level(logging.INFO, logger="openai-proxy")
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    app = create_app(transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        task = asyncio.create_task(client.get("/v1/models"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    ends = [r for r in events(caplog) if r["event"] == "request_finished"]
    assert len(ends) == 1 and ends[0]["outcome"] == "cancelled"
    assert ends[0]["active_requests"] == 0
