# Request diagnostics design

## Goal
Make concurrent-client latency and stream failures diagnosable in `proxy.log`,
preserving pass-through behavior and excluding secrets and payloads.

## Design
- A dedicated diagnostics module owns JSON-line formatting and pure ASGI middleware.
  A small FastAPI subclass wraps its full ASGI entry point so even framework-generated
  500 responses receive a request ID and have their actual status/bytes recorded.
- Each HTTP request gets a generated ID, returned as `X-Proxy-Request-ID`.
- Emit `request_started` and exactly one `request_finished` event, with UTC timestamp,
  level, process ID, method, query-free path, and per-process active request count.
- Completion includes downstream status, upstream status, upstream-header latency,
  first nonempty downstream body latency, total duration, sent body bytes, SSE flag,
  outcome, and sanitized exception class when applicable.
- Timings use a monotonic clock. Upstream-header latency includes upload, connection
  pool wait, connection setup, and upstream processing; it is not token-generation time.
- Observe ASGI send/receive without buffering bodies. Mark completion only after the
  final body send succeeds. Distinguish HTTP errors, pre-header upstream failures,
  mid-stream failures, client disconnects, and cancellation.
- Close each upstream response in a shielded `finally` surrounding response delivery;
  shield httpx's automatic EOF stream close too, and cover response-construction
  failures. Close the shared HTTP client on application shutdown.
- Configure `LOG_LEVEL` (INFO default). Disable Uvicorn access logs in the supported
  Python entry point to avoid duplicate entries and query-string exposure. Server
  startup/shutdown messages retain Uvicorn's normal format.
- Never log headers, query strings, request/response bodies, upstream URLs, or exception
  messages. Paths can contain caller-provided identifiers; document this boundary.

## Verification
Use mocked upstreams for successful SSE, concurrent held-open requests, pool timeout,
mid-stream failure, and direct ASGI disconnect. Assert one terminal event, nonnegative
ordered timings, byte counts, generated IDs, active counts returning to zero, and
upstream cleanup. Verify no secrets appear in captured diagnostic output. Retain all
existing forwarding tests. No performance capacity increase is implied by logging.
