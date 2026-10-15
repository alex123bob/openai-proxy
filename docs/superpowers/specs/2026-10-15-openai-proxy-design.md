# OpenAI Proxy Design

## Goal
A thin pass-through proxy that forwards OpenAI API requests from coding agents to
`https://api.openai.com`, for use where direct access is blocked. It runs on a
Google Cloud server on a local port. Nginx terminates HTTPS and routes one path
prefix to it. Success: a client sets its OpenAI base URL to
`https://<domain>/<prefix>/v1` and works as if talking to OpenAI directly,
including streaming (SSE) responses.

## Decisions
- Stack: Python, FastAPI, httpx (streaming), uvicorn. Matches PP-AI-Service.
- Auth: the client sends its own OpenAI key; the proxy forwards the `Authorization`
  header untouched and stores no secrets.
- No request/response transformation, no multi-provider support (YAGNI).

## Behavior
- `GET /health` returns `{"status": "ok"}`.
- All other methods and paths are forwarded to `UPSTREAM_BASE_URL`
  (default `https://api.openai.com`), preserving method, path, query and body.
- Request headers: drop hop-by-hop headers (`host`, `content-length`, `connection`)
  and `X-Forwarded-*`/`X-Real-IP` added by nginx. Everything else is forwarded.
- Response: stream upstream body back chunk by chunk with the same status code and
  headers (minus hop-by-hop ones), plus `X-Accel-Buffering: no`.
- Upstream connection failure or timeout: respond 502 with a JSON error body.
- Never log the `Authorization` header or request/response bodies.

## Configuration (`.env`)
- `HOST` (default `127.0.0.1`), `PORT` (default `8080`)
- `UPSTREAM_BASE_URL` (default `https://api.openai.com`)

## Deployment
- `Dockerfile` and `start.sh`/`stop.sh` modeled on PP-AI-Service.
- `deploy/nginx.conf.example`: location block at the readable path
  `/openai-proxy/`, `proxy_buffering off`, long `proxy_read_timeout`,
  `proxy_http_version 1.1`, prefix stripped before reaching the proxy.
- Risk: since any caller's key is accepted, the URL is an open relay for others'
  keys (costs only bandwidth). Accepted by the owner; a token check can be added later.

## Layout
- `app.py` – FastAPI app and forwarding logic
- `tests/test_proxy.py` – pytest with mocked upstream: JSON passthrough, SSE
  streaming passthrough, header filtering, 502 on upstream failure, health check
- `requirements.txt`, `.env.example`, `Dockerfile`, `start.sh`, `stop.sh`,
  `deploy/nginx.conf.example`, `README.md`, `.gitignore`
