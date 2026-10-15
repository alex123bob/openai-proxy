# Reliable local process lifecycle

## Approved behavior
`start.sh` and `stop.sh` remain the public commands. A standard-library Python
helper serializes both commands with a portable macOS/Linux `flock`, validates
process ownership, and waits for startup/shutdown.

## Process ownership and startup
- Launch Python with the absolute project `app.py` path and a random instance
  argument. Record PID and instance token in `proxy.process.json`; retain the
  plain `proxy.pid` for convenience after successful startup.
- Check the OS process command and instance token before signaling. Treat exited
  and zombie processes as stopped. Never signal a live PID with missing/mismatched
  ownership information. Handle dead stale PID files automatically.
- Record pending ownership immediately after launch so a failed/interrupted start
  can be stopped later. Successful startup requires this child's Uvicorn listening
  message in its newly appended log segment and a live process, within 15 seconds.
  This uses the project's pinned Uvicorn version and avoids health checks against
  an unrelated server sharing a port.
- Duplicate starts return success with `already running` without modifying state
  or logs. Create/install the virtual environment only after the duplicate check.
- Append to `proxy.log`; don't truncate a live process's log.

## Shutdown
- Missing or dead state returns success with `already stopped` and clears dead
  metadata. Send SIGTERM to a verified owned process and wait up to 10 seconds.
- If still owned/running, send SIGKILL and wait up to 5 more seconds, reporting
  forced shutdown. Remove state only after confirming process exit.
- Report failures with a nonzero exit code; preserve ownership on shutdown failure.

## Tests
Use temporary projects and actual local child processes without network/upstream
access. Test successful start/stop, duplicate and concurrent starts, failed startup,
stale PID cleanup, unrelated process protection, and forced shutdown. Finally run
the real scripts with a private ephemeral port, stop them, and verify no managed
process remains.
