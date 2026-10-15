# OpenAI Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A thin FastAPI pass-through proxy that forwards OpenAI API requests (including SSE streams) to api.openai.com.

**Architecture:** One FastAPI app with a `/health` route and a catch-all route. The catch-all builds an httpx request to `UPSTREAM_BASE_URL`, streams the response back, and filters hop-by-hop/forwarded headers. Nginx (outside this repo) terminates HTTPS and strips an unguessable prefix.

**Tech Stack:** Python 3, FastAPI, httpx, uvicorn, pytest, respx-free mocking via `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-10-15-openai-proxy-design.md`

## Global Constraints

- Client's `Authorization` header is forwarded untouched; no secrets stored on server.
- Never log `Authorization` header or request/response bodies.
- Drop `host`, `content-length`, `connection` and `X-Forwarded-*`/`X-Real-IP` from requests.
- Upstream failure/timeout -> 502 with JSON error body.
- Responses carry `X-Accel-Buffering: no`.
- Defaults: `HOST=127.0.0.1`, `PORT=8080`, `UPSTREAM_BASE_URL=https://api.openai.com`.

## Review Focus

- Large/binary request bodies (audio uploads for Whisper, multipart): forwarded byte-for-byte.
- Query strings (e.g. `?limit=10`) are preserved.
- Upstream non-2xx (401, 429) status and body pass through unchanged, not turned into 502.
- Client `Accept-Encoding: gzip`: response body must not be double-decoded/corrupted.
- Upstream timeout on long streams: read timeout must be long (no truncation of slow generations).

---

### Task 1: Proxy app, tests, and project files

**Files:**
- Create: `app.py`, `tests/test_proxy.py`, `requirements.txt`, `.env.example`, `.gitignore`

**Interfaces:**
- Produces: `app.create_app(transport: httpx.AsyncBaseTransport | None = None, upstream: str | None = None) -> FastAPI`; module-level `app = create_app()`.

- [ ] **Step 1: Create `requirements.txt`, `.gitignore`, `.env.example` and install**

`requirements.txt`:
```
fastapi==0.115.0
uvicorn==0.30.6
httpx==0.27.2
python-dotenv==1.0.1
pytest==8.3.3
pytest-asyncio==0.24.0
```
`.gitignore`:
```
.env
venv/
__pycache__/
.pytest_cache/
```
`.env.example`:
```
HOST=127.0.0.1
PORT=8080
UPSTREAM_BASE_URL=https://api.openai.com
```
Run: `python3 -m venv venv && venv/bin/pip install -r requirements.txt`

- [ ] **Step 2: Write the failing tests** in `tests/test_proxy.py`

```python
import httpx
import pytest
from app import create_app

seen = {}


def upstream_handler(request: httpx.Request) -> httpx.Response:
    seen["request"] = request
    if request.url.path == "/v1/stream":
        def gen():
            yield b"data: one\n\n"
            yield b"data: two\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
    if request.url.path == "/v1/limited":
        return httpx.Response(429, json={"error": "rate"})
    if request.url.path == "/v1/boom":
        raise httpx.ConnectError("down")
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
def client():
    app = create_app(transport=httpx.MockTransport(upstream_handler), upstream="https://up.test")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_sse_streaming_passthrough(client):
    r = await client.get("/v1/stream")
    assert r.text == "data: one\n\ndata: two\n\n"
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"


@pytest.mark.asyncio
async def test_upstream_error_status_passes_through(client):
    r = await client.get("/v1/limited")
    assert r.status_code == 429 and r.json() == {"error": "rate"}


@pytest.mark.asyncio
async def test_upstream_failure_returns_502(client):
    r = await client.get("/v1/boom")
    assert r.status_code == 502 and "error" in r.json()


@pytest.mark.asyncio
async def test_binary_body_forwarded_unchanged(client):
    blob = bytes(range(256)) * 100
    await client.post("/v1/audio/transcriptions", content=blob)
    assert seen["request"].content == blob
```

Also create `pytest.ini`:
```
[pytest]
asyncio_mode = auto
pythonpath = .
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/bin/pytest -v`
Expected: FAIL (`ImportError: cannot import name 'create_app'`)

- [ ] **Step 4: Implement `app.py`**

```python
import logging
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

load_dotenv()
logger = logging.getLogger("openai-proxy")

DEFAULT_UPSTREAM = "https://api.openai.com"
HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
              "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
              "x-real-ip"}
# Response headers httpx already decoded or that the ASGI server recomputes.
RESPONSE_DROP = HOP_BY_HOP | {"content-encoding"}


def _filter_request_headers(headers) -> dict:
    return {k: v for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-forwarded-")}


def create_app(transport=None, upstream=None) -> FastAPI:
    upstream = (upstream or os.getenv("UPSTREAM_BASE_URL", DEFAULT_UPSTREAM)).rstrip("/")
    app = FastAPI()
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10),
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def forward(path: str, request: Request):
        headers = _filter_request_headers(request.headers)
        # Ask upstream for identity so bytes we relay match the headers we relay.
        headers["accept-encoding"] = "identity"
        url = f"{upstream}/{path}"
        try:
            req = client.build_request(
                request.method, url, params=request.query_params.multi_items(),
                headers=headers, content=request.stream(),
            )
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream error: %s", type(exc).__name__)
            return JSONResponse({"error": f"upstream request failed: {type(exc).__name__}"}, status_code=502)

        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in RESPONSE_DROP}
        out_headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code,
                                 headers=out_headers, background=_close(resp))

    return app


def _close(resp):
    from starlette.background import BackgroundTask
    return BackgroundTask(resp.aclose)


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/pytest -v`
Expected: 6 passed. If the mock-transport streaming request body (`content=request.stream()`) fails, read the body with `await request.body()` instead and re-run.

- [ ] **Step 6: Commit**

```bash
git add app.py tests pytest.ini requirements.txt .env.example .gitignore
git commit -m "feat: add OpenAI pass-through proxy"
```

### Task 2: Deployment files and README

**Files:**
- Create: `Dockerfile`, `start.sh`, `stop.sh`, `deploy/nginx.conf.example`, `README.md`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
```

- [ ] **Step 2: Write `start.sh` / `stop.sh`** (then `chmod +x`)

`start.sh`:
```sh
#!/bin/sh
cd "$(dirname "$0")"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
nohup venv/bin/python app.py > proxy.log 2>&1 &
echo $! > proxy.pid
echo "started pid $(cat proxy.pid)"
```
`stop.sh`:
```sh
#!/bin/sh
cd "$(dirname "$0")"
[ -f proxy.pid ] && kill "$(cat proxy.pid)" && rm proxy.pid && echo stopped
```
Add `proxy.log` and `proxy.pid` to `.gitignore`.

- [ ] **Step 3: Write `deploy/nginx.conf.example`**

```nginx
location /openai-CHANGE-ME/ {
    proxy_pass http://127.0.0.1:8080/;   # trailing slash strips the prefix
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    client_max_body_size 100m;
}
```

- [ ] **Step 4: Write `README.md`** covering: what it is, local run (`./start.sh`), config table, nginx setup, client usage (`OPENAI_BASE_URL=https://<domain>/openai-CHANGE-ME/v1`, key stays on the client), the open-relay caveat, and a curl smoke test: `curl https://<domain>/openai-CHANGE-ME/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"`.

- [ ] **Step 5: Smoke test locally**

Run: `./start.sh && sleep 2 && curl -s localhost:8080/health && ./stop.sh`
Expected: `{"status":"ok"}` then `stopped`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile start.sh stop.sh deploy README.md .gitignore
git commit -m "feat: add deployment files and README"
```
