import httpx
import pytest
from app import create_app

seen = {}


def upstream_handler(request: httpx.Request) -> httpx.Response:
    seen["request"] = request
    if request.url.path == "/v1/stream":
        async def gen():
            yield b"data: one\n\n"
            yield b"data: two\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
    if request.url.path == "/v1/limited":
        return httpx.Response(429, json={"error": "rate"})
    if request.url.path == "/v1/cookie":
        return httpx.Response(200, headers={"set-cookie": "__cf_bm=abc; Path=/"}, json={})
    if request.url.path == "/v1/boom":
        raise httpx.ConnectError("down")
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
def client():
    app = create_app(transport=httpx.MockTransport(upstream_handler), upstream="https://up.test")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_json_passthrough_forwards_auth_body_query(client):
    r = await client.post("/v1/chat/completions?x=1", content=b'{"a":1}',
                          headers={"Authorization": "Bearer sk-test", "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    req = seen["request"]
    assert req.headers["authorization"] == "Bearer sk-test"
    assert "x-forwarded-for" not in req.headers
    assert req.url.query == b"x=1"
    assert req.content == b'{"a":1}'
    assert req.url.host == "up.test"


async def test_sse_streaming_passthrough(client):
    r = await client.get("/v1/stream")
    assert r.text == "data: one\n\ndata: two\n\n"
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"


async def test_upstream_error_status_passes_through(client):
    r = await client.get("/v1/limited")
    assert r.status_code == 429 and r.json() == {"error": "rate"}


async def test_upstream_failure_returns_502(client):
    r = await client.get("/v1/boom")
    assert r.status_code == 502 and "error" in r.json()


async def test_binary_body_forwarded_unchanged(client):
    blob = bytes(range(256)) * 100
    await client.post("/v1/audio/transcriptions", content=blob)
    assert seen["request"].content == blob


async def test_upstream_cookies_not_shared_between_callers(client):
    await client.get("/v1/cookie")
    client.cookies.clear()  # a different caller: it holds no cookies of its own
    await client.get("/v1/models")
    assert "cookie" not in seen["request"].headers


async def test_encoded_path_and_raw_query_forwarded_verbatim(client):
    await client.get("/v1/files/ab%2Fc?flag&a=1&a=2&q=x+y")
    assert seen["request"].url.raw_path == b"/v1/files/ab%2Fc?flag&a=1&a=2&q=x+y"


async def test_bodiless_get_is_not_chunked(client):
    await client.get("/v1/models")
    assert "transfer-encoding" not in seen["request"].headers


async def test_post_keeps_content_length_not_chunked(client):
    await client.post("/v1/chat/completions", content=b'{"a":1}')
    h = seen["request"].headers
    assert h["content-length"] == "7" and "transfer-encoding" not in h
