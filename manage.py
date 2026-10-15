"""Local process lifecycle for the macOS/Linux start.sh and stop.sh wrappers."""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Lifecycle:
    def __init__(self, root, python=None, startup_timeout=15, shutdown_timeout=10):
        self.root = Path(root).resolve()
        self.python = python
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.pid_file = self.root / "proxy.pid"
        self.state_file = self.root / "proxy.process.json"
        self.log_file = self.root / "proxy.log"
        self.child = None

    @contextmanager
    def _lock(self):
        # Keep the lock file: deleting it could let concurrent commands lock
        # different inodes and defeat mutual exclusion.
        with (self.root / "proxy.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _command(self, pid):
        if self.child is not None and self.child.pid == pid:
            if self.child.poll() is not None:
                return None
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "args="],
                                capture_output=True, text=True, check=False)
        parts = result.stdout.strip().split(None, 1)
        if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
            return None
        if result.returncode or len(parts) != 2:
            raise RuntimeError("cannot inspect proxy process; lifecycle state retained for retry")
        if parts[0].startswith("Z"):
            return None
        return parts[1]

    def _owns(self, command, state):
        return (str(self.root / "app.py") in command and
                command.endswith("--proxy-instance=" + state["token"]))

    def _clear(self):
        self.pid_file.unlink(missing_ok=True)
        self.state_file.unlink(missing_ok=True)

    def _state(self):
        try:
            if self.state_file.exists():
                state = json.loads(self.state_file.read_text())
                if (not isinstance(state, dict) or type(state.get("pid")) is not int or
                        state["pid"] <= 1 or not isinstance(state.get("token"), str) or
                        not re.fullmatch(r"[0-9a-f]{32}", state["token"]) or
                        type(state.get("log_offset")) is not int or state["log_offset"] < 0):
                    raise ValueError("invalid process metadata")
                if self.pid_file.exists() and int(self.pid_file.read_text()) != state["pid"]:
                    raise ValueError("PID and process metadata disagree")
                command = self._command(state["pid"])
                if command is None:
                    self._clear()
                    return None
                if not self._owns(command, state):
                    raise RuntimeError("process ownership mismatch; no process was signalled")
                return state
            if self.pid_file.exists():
                pid = int(self.pid_file.read_text())
                if pid <= 1:
                    raise ValueError("invalid PID")
                if self._command(pid) is not None:
                    raise RuntimeError("cannot verify process ownership without proxy.process.json; "
                                       "no process was signalled")
                self._clear()
            return None
        except (ValueError, TypeError) as exc:
            raise RuntimeError("invalid lifecycle state; inspect proxy.pid and proxy.process.json") from exc

    def _write(self, path, text):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text)
        temporary.replace(path)

    def _prepare_python(self):
        if self.python is not None:
            return self.python
        python = self.root / "venv" / "bin" / "python"
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(self.root / "venv")], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-q", "-r",
                        str(self.root / "requirements.txt")], check=True, cwd=self.root)
        return str(python)

    def _wait_ready(self, state):
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            command = self._command(state["pid"])
            if command is None or not self._owns(command, state):
                raise RuntimeError("proxy startup failed; see proxy.log")
            with self.log_file.open("rb") as log:
                log.seek(state["log_offset"])
                if b"Uvicorn running on " in log.read():
                    return
            time.sleep(0.1)
        raise RuntimeError("proxy startup timed out; see proxy.log")

    def _wait_stopped(self, state, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            command = self._command(state["pid"])
            if command is None or not self._owns(command, state):
                return True
            time.sleep(0.1)
        return False

    def _stop_owned(self, state):
        command = self._command(state["pid"])
        if command is None:
            return False
        if not self._owns(command, state):
            raise RuntimeError("process ownership mismatch; no process was signalled")
        try:
            os.kill(state["pid"], signal.SIGTERM)
        except ProcessLookupError:
            return False
        if self._wait_stopped(state, self.shutdown_timeout):
            return False
        command = self._command(state["pid"])
        if command is None or not self._owns(command, state):
            return False
        try:
            os.kill(state["pid"], signal.SIGKILL)
        except ProcessLookupError:
            return False
        if not self._wait_stopped(state, 5):
            raise RuntimeError("proxy did not stop; process metadata retained for retry")
        return True

    def start(self):
        with self._lock():
            state = self._state()
            if state is not None:
                if not self.pid_file.exists():
                    # Recover an interrupted start using its already recorded ownership.
                    try:
                        self._wait_ready(state)
                    except RuntimeError:
                        self._stop_owned(state)
                        self._clear()
                        raise
                    self._write(self.pid_file, str(state["pid"]) + "\n")
                print("already running pid", state["pid"])
                return state["pid"]

            python = self._prepare_python()
            token = uuid.uuid4().hex
            with self.log_file.open("ab") as log:
                offset = log.tell()
                self.child = subprocess.Popen(
                    [python, "-u", str(self.root / "app.py"), "--proxy-instance=" + token],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            state = {"pid": self.child.pid, "token": token, "log_offset": offset}
            try:
                self._write(self.state_file, json.dumps(state) + "\n")
                self._wait_ready(state)
                self._write(self.pid_file, str(state["pid"]) + "\n")
            except BaseException:
                self._stop_owned(state)
                self._clear()
                raise
            print("started pid", state["pid"])
            return state["pid"]

    def stop(self):
        with self._lock():
            state = self._state()
            if state is None:
                print("already stopped")
                return
            forced = self._stop_owned(state)
            self._clear()
            print("stopped pid", state["pid"], "(forced)" if forced else "")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "stop"}:
        print("usage: manage.py start|stop", file=sys.stderr)
        return 2
    manager = Lifecycle(Path(__file__).resolve().parent)
    try:
        getattr(manager, sys.argv[1])()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
