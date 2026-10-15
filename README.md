# openai-proxy

A thin pass-through proxy that forwards OpenAI API requests (including streaming)
to `https://api.openai.com`. Deploy it on a server that can reach OpenAI, put nginx
(with HTTPS) in front, and point your coding agent's base URL at it.

The client keeps its own OpenAI key and sends it in the `Authorization` header; the
proxy forwards it untouched and stores no secrets. It never logs headers; bodies are logged only if you opt in (`LOG_BODIES`).

## Run

    ./start.sh     # creates venv, installs deps, runs in background (proxy.log)
    ./stop.sh

Both commands work from any directory. Repeating `./start.sh` reports `already
running` and preserves the existing PID/log; repeating `./stop.sh` reports `already
stopped` and succeeds.

Start/stop commands are serialized by `proxy.lock`. Startup is confirmed before
publishing `proxy.pid`; a failed startup exits nonzero and cleans up its child.
`proxy.process.json` records process ownership so stale or reused PIDs cannot
cause an unrelated process to be stopped. Keep this metadata while the proxy is
running. Dead stale PID files are cleared automatically; a live legacy PID without
ownership metadata requires manual verification and shutdown once.

Stopping sends SIGTERM and waits up to 10 seconds. If necessary, it force-stops
the verified proxy with SIGKILL, waits up to 5 more seconds, and reports `(forced)`.
This may interrupt active streams. Metadata is removed only after shutdown has
been confirmed. The lock file remains between runs and should not be deleted
while lifecycle commands are running.

Or with Docker:

    docker build -t openai-proxy .
    docker run -d -p 127.0.0.1:8080:8080 --restart unless-stopped openai-proxy

## Configuration (`.env`, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `HOST` | `127.0.0.1` | bind address |
| `PORT` | `8080` | bind port |
| `UPSTREAM_BASE_URL` | `https://api.openai.com` | where requests are forwarded |
| `LOG_LEVEL` | `INFO` | diagnostic log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `LOG_BODIES` | `off` | `off`, `request`, or `both`: log request (and response) content as separate events |
| `LOG_BODY_MAX_BYTES` | `8192` | per-body size limit for logged content; longer bodies are cut and flagged `truncated` |

## Diagnostics and performance

Requests produce JSON lines in `proxy.log` when started and when finished. Each gets
a generated `request_id`, also returned in the `X-Proxy-Request-ID` response header.
Use that ID to connect a client's report to its log entries. Uvicorn's server
startup/shutdown messages retain their usual text format; its duplicate access logs
are disabled when running `python app.py` (including `start.sh` and Docker).
If launching with the Uvicorn CLI, use `uvicorn app:app --no-access-log`.

Example finished request (formatted here for readability; actual output is one line):

```json
{
  "timestamp": "2026-10-15T12:00:01.000Z",
  "level": "INFO",
  "pid": 12345,
  "event": "request_finished",
  "request_id": "ea874a28d62d44e290eef2e5d37c181f",
  "method": "POST",
  "path": "/v1/responses",
  "active_requests": 3,
  "status": 200,
  "upstream_status": 200,
  "upstream_headers_ms": 320.0,
  "first_body_byte_ms": 450.0,
  "duration_ms": 5200.0,
  "response_bytes": 8421,
  "is_stream": true,
  "outcome": "completed",
  "error_type": null
}
```

| Field | Interpretation |
|---|---|
| `timestamp`, `pid` | UTC event time and worker process ID |
| `active_requests` | Active HTTP requests **in this process**, including streams; measured after start/finish |
| `upstream_headers_ms` | From request start until upstream headers: includes upload, pool wait, connection setup, and upstream processing; null if headers never arrived |
| `first_body_byte_ms` | From request start until the first nonempty body chunk is handed to the downstream server; not necessarily a model token or receipt by the client |
| `duration_ms` | Full request/stream lifecycle, including upstream cleanup; long streams naturally have long durations |
| `response_bytes` | Body bytes successfully handed to the downstream server, excluding headers; not a client-delivery acknowledgment |
| `status`, `upstream_status` | Downstream status sent and upstream status; a stream can fail **after** sending 200 |
| `is_stream` | Whether the response content type is `text/event-stream` |
| `outcome`, `error_type` | Completion/failure category and exception class, without exception messages |

Useful troubleshooting patterns:

- `upstream_error` with `PoolTimeout`: investigate connection-pool saturation;
  httpx defaults to 100 upstream connections per process and streams occupy them.
- `http_error` with upstream 429: investigate upstream API rate limits.
- `stream_error` with `ReadError`/`ReadTimeout`: upstream failed after headers;
  the previously sent HTTP status may still be 200.
- `client_disconnected`: downstream disconnected before completion. `cancelled`
  indicates task cancellation without an observed client disconnect.
- `application_error` or `incomplete`: unexpected failure or response ending without
  a successful final body send.
- Growing header latency alongside active requests suggests contention; these
  timings do **not** separately measure DNS, TLS, or connection-pool wait.

`request_finished` also carries `client_ip`, `user_agent`, `content_type`, `request_bytes`,
`model`, `stream_requested`, `message_count`, `tool_count`, `max_tokens`, `usage` (token
counts), `finish_reason`, `upstream_request_id`, `retry_after`, `ratelimit`, and a short
`upstream_error` on failures. Content never appears unless `LOG_BODIES` is set.

With `LOG_BODIES=request|both`, extra `request_body` / `response_body` events (same
`request_id`) hold the prompt and reply. Values under keys like `api_key`, `token`, and
`password` are redacted and `sk-…` strings masked, but prompts may still contain anything
you typed, so protect `proxy.log`. Streaming responses are logged as the reassembled
`text`, not per chunk. Capture copies bytes as they pass, so streaming is not delayed.

Headers (including `Authorization`), query strings, upstream URLs, and exception
messages are excluded from diagnostic events. Paths are logged and may contain
resource IDs, so avoid embedding secrets in paths. Separate nginx access logs have
their own configuration. Keep `LOG_LEVEL=INFO` for timings; `WARNING` suppresses
successful request entries. `DEBUG` currently has the same request events as INFO.

Apply changes with `./stop.sh` then `./start.sh`, or rebuild/recreate the Docker
container. `start.sh` appends to `proxy.log`, preserving previous entries across
restarts. Configure external log rotation for long-running
deployments. Logging provides evidence for tuning; it does not increase connection
capacity by itself.

## nginx

Copy `deploy/nginx.conf.example` into your HTTPS `server` block. It serves the proxy
at `/openai-proxy/`. Note that the proxy accepts any caller's OpenAI key, so anyone who
finds that path can relay requests through your server (this costs you bandwidth, not
money, and never exposes your key). If that becomes a problem, add an access token check
or switch to an unguessable path.

## Client usage

    export OPENAI_BASE_URL=https://lijialab.com/openai-proxy/v1
    export OPENAI_API_KEY=sk-...

Smoke test:

    curl https://lijialab.com/openai-proxy/v1/models \
      -H "Authorization: Bearer $OPENAI_API_KEY"

### opencode

Add to `~/.config/opencode/opencode.json` (or `opencode.json` in a project root):

    {
      "$schema": "https://opencode.ai/config.json",
      "provider": {
        "openai": {
          "options": {
            "baseURL": "https://lijialab.com/openai-proxy/v1",
            "apiKey": "{env:OPENAI_API_KEY}"
          }
        }
      }
    }

Export `OPENAI_API_KEY` in your shell (or run `opencode auth login`). This works with
API keys only; ChatGPT account login uses a different endpoint the proxy does not cover.
`baseURL` must end in `/v1` with no trailing slash. If replies arrive all at once instead
of streaming, nginx is buffering: add `gzip off;` to the location block.

## Tests

    venv/bin/pytest
