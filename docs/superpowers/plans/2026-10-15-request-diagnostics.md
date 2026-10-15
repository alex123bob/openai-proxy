# Request Diagnostics Implementation Plan

**Goal:** Add useful request tracing and streaming performance diagnostics.

**Architecture:** Pure ASGI middleware records downstream lifecycle; forwarding code
records upstream headers/errors in request state. A response subclass guarantees
upstream cleanup across completion, failures, and disconnects.

**Tech Stack:** FastAPI, Starlette, httpx, AnyIO, standard-library JSON/logging, pytest.

## Tasks
1. Add `tests/test_diagnostics.py` with successful SSE and secret-redaction assertions,
   pre-header timeout, post-header error, concurrency, disconnect, and shutdown checks.
   Run `venv/bin/pytest tests/test_diagnostics.py -q` and confirm missing diagnostics
   cause failures before production edits.
2. Create `diagnostics.py` for JSON events and ASGI lifecycle tracking. Update `app.py`
   to register middleware, record upstream metrics, and guarantee response cleanup.
   Run the focused tests, then `venv/bin/pytest -q` for forwarding regressions.
3. Add `LOG_LEVEL` to `.env.example`; document fields, units, timing interpretation,
   privacy limits, restart instructions, and troubleshooting patterns in `README.md`.
   Copy the diagnostics module into Docker. Inspect a real formatted event and run
   `git diff --check` and the complete test suite.

Execute inline; leave changes uncommitted for user review.
