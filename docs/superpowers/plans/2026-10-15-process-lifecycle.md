# Process Lifecycle Implementation Plan

**Goal:** Reliable repeatable `start.sh` / `stop.sh` with verified ownership.

**Architecture:** Thin shell wrappers invoke a standard-library `manage.py` helper.
File locking serializes commands; per-instance process metadata protects against
stale PIDs and process reuse. Startup inspects only the new child's log segment.

**Tech Stack:** POSIX shell, Python subprocess/signal/fcntl/pathlib, pytest.

1. Write `tests/test_lifecycle.py` using temporary projects and local child scripts
   to cover lifecycle, duplicate/concurrent starts, startup failure, stale state,
   unrelated-process protection, and forced shutdown. Run
   `venv/bin/pytest tests/test_lifecycle.py -q` and observe missing-helper failures.
2. Add `manage.py`, switch both shell wrappers to invoke it, and ignore lock/process
   metadata in `.gitignore`. Run the focused tests and fix failures individually.
3. Update `README.md` with repeated-command behavior, shutdown timeout/force behavior,
   retained logs, and stale-state recovery. Run `sh -n start.sh stop.sh`, all tests,
   `git diff --check`, and a real start/duplicate-start/stop/repeated-stop smoke test
   on an ephemeral loopback port without calling OpenAI.

Execute inline; leave changes uncommitted.
