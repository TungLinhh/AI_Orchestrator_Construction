"""Manage the native API using recorded Linux process and listener identities."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path


def identity(pid: int) -> dict | None:
    try:
        proc = Path(f"/proc/{pid}")
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": stat[19],
            "cwd": str((proc / "cwd").resolve(strict=True)),
            "argv": (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0"),
            "exe": str((proc / "exe").resolve(strict=True)),
        }
    except OSError, ValueError, UnicodeError:
        return None


def owns_listener(pid: int, port: int) -> bool:
    try:
        inodes = set()
        for table in ("tcp", "tcp6"):
            for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and int(fields[1].split(":")[1], 16) == port:
                    inodes.add(f"socket:[{fields[9]}]")
        return any(str(fd.readlink()) in inodes for fd in Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def record_path(pidfile: Path) -> Path:
    return pidfile.with_suffix(".json")


def stop(pidfile: Path, timeout: float = 10) -> bool:
    """Never signal a PID unless its complete recorded identity still matches."""
    receipt = record_path(pidfile)
    if not receipt.exists():
        if pidfile.exists():
            raise RuntimeError("PID file has no ownership receipt; refusing to signal it")
        return False
    try:
        record = json.loads(receipt.read_text())
        pid = int(pidfile.read_text().strip())
        expected = record["identity"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Invalid service ownership receipt; refusing to signal") from exc
    current = identity(pid)
    if current is not None and (pid != expected.get("pid") or current != expected):
        raise RuntimeError("PID belongs to another process; refusing to signal it")
    if current is not None:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and identity(pid) == expected:
            time.sleep(0.05)
        if identity(pid) == expected:
            raise RuntimeError("Owned API did not stop; no forced kill was attempted")
    pidfile.unlink(missing_ok=True)
    receipt.unlink(missing_ok=True)
    return True


def start(
    pidfile: Path, logfile: Path, port: int, argv: list[str], cwd: Path, timeout: float = 30
) -> dict:
    """Start an actual child, verify its listener and readiness, then return receipt."""
    if pidfile.exists() or record_path(pidfile).exists():
        stop(pidfile)
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"Port {port} is busy; existing listener was left untouched"
            ) from exc
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with logfile.open("ab") as log:
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    expected = identity(child.pid)
    if expected is None:
        child.wait(timeout=5)
        raise RuntimeError("API exited before process identity could be recorded")
    receipt = {"identity": expected, "port": port, "log": str(logfile.resolve())}
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    record_path(pidfile).write_text(json.dumps(receipt, indent=2))
    pidfile.write_text(str(child.pid) + "\n")
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"API exited with status {child.returncode}; inspect {logfile}")
            # Python may exec once at process startup; require exact expected argv
            # and cwd from the launched process, not a healthy unrelated listener.
            if identity(child.pid) == expected and owns_listener(child.pid, port):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/ready", timeout=1
                    ) as reply:
                        if reply.status == 200 and identity(child.pid) == expected:
                            return receipt
                except OSError, urllib.error.URLError:
                    pass
            time.sleep(0.05)
        raise RuntimeError(f"API readiness timed out; inspect {logfile}")
    except BaseException:
        stop(pidfile)
        child.wait(timeout=5)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--pidfile", type=Path, default=Path(".devdata/run/serve.pid"))
    parser.add_argument("--logfile", type=Path, default=Path(".devdata/logs/serve.log"))
    args = parser.parse_args()
    try:
        with locked(args.pidfile):
            if args.action == "stop":
                print("stopped" if stop(args.pidfile) else "nothing started by make serve")
            else:
                # Configuration remains environment-owned; Makefile chooses the
                # same explicit localhost/auth/provider mode as the prior recipe.
                receipt = start(
                    args.pidfile,
                    args.logfile,
                    args.port,
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "ai_orchestrator.main:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(args.port),
                        "--log-level",
                        "warning",
                    ],
                    Path.cwd(),
                )
                print(json.dumps({"ready": True, **receipt}))
    except (RuntimeError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
