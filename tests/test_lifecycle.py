import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from manage import Lifecycle


SERVER = '''import signal
import time
signal.signal(signal.SIGTERM, lambda *_: exit(0))
print("INFO:     Uvicorn running on http://127.0.0.1:12345 (Press CTRL+C to quit)", flush=True)
while True:
    time.sleep(0.1)
'''


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text(SERVER)
    manager = Lifecycle(tmp_path, python=sys.executable, startup_timeout=2, shutdown_timeout=0.2)
    yield tmp_path, manager
    try:
        manager.stop()
    finally:
        if manager.child is not None and manager.child.poll() is None:
            manager.child.kill()
            manager.child.wait(timeout=2)


def test_start_duplicate_stop_and_repeated_stop(project):
    root, manager = project
    (root / "proxy.log").write_text("previous log\n")
    pid = manager.start()
    assert int((root / "proxy.pid").read_text()) == pid
    state = (root / "proxy.process.json").read_text()
    size = (root / "proxy.log").stat().st_size
    assert manager.start() == pid
    assert (root / "proxy.process.json").read_text() == state
    assert (root / "proxy.log").stat().st_size == size
    assert (root / "proxy.log").read_text().startswith("previous log\n")
    manager.stop()
    assert not (root / "proxy.pid").exists()
    assert not (root / "proxy.process.json").exists()
    manager.stop()


def test_failed_start_never_records_success(project):
    root, manager = project
    (root / "app.py").write_text('raise SystemExit(2)\n')
    with pytest.raises(RuntimeError, match="startup"):
        manager.start()
    assert not (root / "proxy.pid").exists()
    assert not (root / "proxy.process.json").exists()


def test_startup_timeout_stops_child(project):
    root, manager = project
    (root / "app.py").write_text('import time\ntime.sleep(60)\n')
    manager.startup_timeout = 0.2
    with pytest.raises(RuntimeError, match="startup"):
        manager.start()
    assert not (root / "proxy.pid").exists()
    assert not (root / "proxy.process.json").exists()


def test_dead_pid_is_cleaned_up(project):
    root, manager = project
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=2)
    (root / "proxy.pid").write_text(str(child.pid))
    manager.stop()
    assert not (root / "proxy.pid").exists()
    manager.start()
    manager.stop()


def test_unrelated_process_is_not_signalled(project):
    root, manager = project
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (root / "proxy.pid").write_text(str(child.pid))
        (root / "proxy.process.json").write_text(json.dumps({
            "pid": child.pid, "token": "a" * 32, "log_offset": 0}))
        with pytest.raises(RuntimeError, match="ownership"):
            manager.stop()
        assert child.poll() is None
        assert (root / "proxy.pid").exists()
    finally:
        child.terminate()
        child.wait(timeout=2)


def test_concurrent_starts_publish_one_pid(project):
    root, manager = project
    other = Lifecycle(root, python=sys.executable, startup_timeout=2, shutdown_timeout=0.2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(manager.start)
        second = pool.submit(other.start)
        assert first.result(timeout=5) == second.result(timeout=5)
    manager.stop()
    if other.child is not None:
        other.child.wait(timeout=2)


def test_force_shutdown_of_verified_process(project, capsys):
    root, manager = project
    (root / "app.py").write_text(SERVER.replace(
        "signal.signal(signal.SIGTERM, lambda *_: exit(0))",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)"))
    manager.start()
    started = time.monotonic()
    manager.stop()
    assert time.monotonic() - started < 3
    assert "forced" in capsys.readouterr().out
    assert not (root / "proxy.pid").exists()


def test_unverified_live_pid_without_metadata_is_preserved(project):
    root, manager = project
    (root / "proxy.pid").write_text(str(os.getpid()))
    try:
        with pytest.raises(RuntimeError, match="ownership"):
            manager.stop()
        assert (root / "proxy.pid").exists()
    finally:
        (root / "proxy.pid").unlink()


def test_process_inspection_failure_preserves_state(project, monkeypatch):
    root, manager = project
    manager.start()
    original_run = subprocess.run

    def failed_ps(command, **kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="inspection failed")
        return original_run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", failed_ps)
        with pytest.raises(RuntimeError, match="inspect"):
            manager.stop()
        assert manager.child.poll() is None
        assert (root / "proxy.pid").exists()
        assert (root / "proxy.process.json").exists()
