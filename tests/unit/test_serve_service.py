"""Native lifecycle tests use disposable local processes, never product API/model."""

import importlib.util
import json
import socket
import subprocess
import sys

import pytest

spec = importlib.util.spec_from_file_location("serve_service", "scripts/serve_service.py")
assert spec and spec.loader
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_ready_child_has_actual_pid_owned_listener_and_verified_stop(tmp_path):
    port = free_port()
    server = tmp_path / "server.py"
    server.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200 if self.path == '/ready' else 404); self.end_headers()\n"
        f"HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()\n"
    )
    pidfile = tmp_path / "service.pid"
    receipt = service.start(
        pidfile, tmp_path / "log", port, [sys.executable, str(server)], tmp_path, 5
    )
    pid = int(pidfile.read_text())
    try:
        assert receipt["identity"] == service.identity(pid)
        assert receipt["identity"]["cwd"] == str(tmp_path)
        assert service.owns_listener(pid, port)
        assert service.stop(pidfile)
        assert service.identity(pid) is None
        assert not pidfile.exists()
        assert not service.record_path(pidfile).exists()
    finally:
        if pidfile.exists():
            service.stop(pidfile)


def test_stale_pid_without_receipt_never_signals_unrelated_process(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pidfile = tmp_path / "service.pid"
        pidfile.write_text(str(child.pid))
        with pytest.raises(RuntimeError, match="no ownership receipt"):
            service.stop(pidfile)
        assert child.poll() is None
        forged = {"identity": {**service.identity(child.pid), "start_ticks": "wrong"}}
        service.record_path(pidfile).write_text(json.dumps(forged))
        with pytest.raises(RuntimeError, match="another process"):
            service.stop(pidfile)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_dead_owned_receipt_is_removed_without_signalling(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    expected = service.identity(child.pid)
    child.wait(timeout=5)
    pidfile = tmp_path / "service.pid"
    pidfile.write_text(str(child.pid))
    service.record_path(pidfile).write_text(json.dumps({"identity": expected}))
    assert service.stop(pidfile)
    assert not pidfile.exists()


def test_busy_port_does_not_accept_someone_elses_health(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="busy"):
            service.start(
                tmp_path / "service.pid",
                tmp_path / "log",
                port,
                [sys.executable, "-c", "pass"],
                tmp_path,
                1,
            )
        assert listener.fileno() != -1
        assert not (tmp_path / "service.pid").exists()


def test_failed_startup_returns_failure_and_removes_receipt(tmp_path):
    pidfile = tmp_path / "service.pid"
    with pytest.raises(RuntimeError, match="exited"):
        service.start(
            pidfile,
            tmp_path / "log",
            free_port(),
            [sys.executable, "-c", "raise SystemExit(7)"],
            tmp_path,
            2,
        )
    assert not pidfile.exists()
    assert not service.record_path(pidfile).exists()


def test_live_child_without_listener_times_out_and_is_stopped(tmp_path):
    pidfile = tmp_path / "service.pid"
    with pytest.raises(RuntimeError, match="timed out"):
        service.start(
            pidfile,
            tmp_path / "log",
            free_port(),
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            0.2,
        )
    assert not pidfile.exists()
