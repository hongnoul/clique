"""Background process management for `clique serve` / `clique join`.

Each role ("server", "agent") gets a pidfile (JSON: pid, start time,
argv, free-form meta) and a log file under <data_dir>/run/. Spawned
processes are fully detached (new session, redirected stdio) so they
outlive the terminal that started them; stopping them is a signal +
wait, not a socket the caller has to hold open.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProcInfo:
    pid: int
    started_at: str
    argv: list[str]
    meta: dict = field(default_factory=dict)


def _run_dir(data_dir: Path) -> Path:
    d = data_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file(data_dir: Path, name: str) -> Path:
    return _run_dir(data_dir) / f"{name}.json"


def log_file(data_dir: Path, name: str) -> Path:
    return _run_dir(data_dir) / f"{name}.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def status(data_dir: Path, name: str) -> ProcInfo | None:
    """Info for `name` if it's running; clears the pidfile if it's stale."""
    f = _pid_file(data_dir, name)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        f.unlink(missing_ok=True)
        return None
    if not _alive(data["pid"]):
        f.unlink(missing_ok=True)
        return None
    return ProcInfo(pid=data["pid"], started_at=data["started_at"],
                    argv=data.get("argv", []), meta=data.get("meta", {}))


class SpawnError(RuntimeError):
    """Raised when the process exits during the startup grace window
    instead of staying up (e.g. port already in use). Carries whatever it
    printed so the caller can surface the real failure immediately."""

    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


def spawn(data_dir: Path, name: str, argv: list[str], meta: dict | None = None,
         grace_s: float = 1.5) -> ProcInfo:
    """Start argv as a detached background process. Raises RuntimeError if
    `name` is already running (check status() first if you want to decide
    what to do instead of erroring). Raises SpawnError -- instead of
    reporting a false success -- if the process exits on its own within
    grace_s seconds, which catches the common instant-crash cases (port
    already in use, bad args) without waiting around for slow ones."""
    if status(data_dir, name) is not None:
        raise RuntimeError(f"{name} is already running")
    log_path = log_file(data_dir, name)
    log_start = log_path.stat().st_size if log_path.exists() else 0
    log_fh = open(log_path, "ab")
    try:
        kwargs: dict = dict(stdout=log_fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, close_fds=True)
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
    finally:
        log_fh.close()

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            with log_path.open("rb") as f:
                f.seek(log_start)
                tail = f.read().decode(errors="replace").strip()
            raise SpawnError(
                f"{name} exited immediately (code {proc.returncode})", tail)
        time.sleep(0.1)

    info = ProcInfo(pid=proc.pid, started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    argv=argv, meta=meta or {})
    _pid_file(data_dir, name).write_text(json.dumps({
        "pid": info.pid, "started_at": info.started_at,
        "argv": info.argv, "meta": info.meta,
    }))
    return info


def stop(data_dir: Path, name: str, timeout_s: float = 10.0, force: bool = False) -> bool:
    """Signal the running process to shut down and wait for it to exit.

    A plain stop sends SIGTERM (the server and agent both treat this as
    "shut down cleanly"); if it hasn't exited within timeout_s, escalates
    to SIGKILL. force=True skips straight to SIGKILL. Returns False if
    `name` wasn't running to begin with.
    """
    info = status(data_dir, name)
    if info is None:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(info.pid, signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and _alive(info.pid):
        time.sleep(0.2)
    if _alive(info.pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(info.pid, signal.SIGKILL)
        time.sleep(0.2)
    _pid_file(data_dir, name).unlink(missing_ok=True)
    return True


def tail(data_dir: Path, name: str, lines: int = 40, follow: bool = False) -> None:
    """Print the tail of the role's log file; with follow=True, keep
    printing new lines (like `tail -f`) until stopped. This never touches
    the background process -- it's just reading its log file."""
    path = log_file(data_dir, name)
    if not path.exists():
        print(f"no log yet for {name} ({path})")
        return
    with path.open("r", errors="replace") as f:
        for line in f.readlines()[-lines:]:
            print(line, end="")
        if not follow:
            return
        _follow_with_quit_key(f)


_FOOTER = "-- following; q to stop (Ctrl+C also works) --"


def _follow_with_quit_key(f) -> None:
    """Poll `f` for new lines, printing them, until 'q' is pressed or the
    process is interrupted. On a real terminal the footer stays pinned to
    the bottom line -- new log lines are printed above it via a
    clear-and-redraw, the same trick progress bars use -- and it's wiped
    on the way out so it doesn't linger next to the shell prompt. Piped
    output just gets the footer once, no redraw games. Uses cbreak mode
    (not raw) so Ctrl+C still raises KeyboardInterrupt normally; terminal
    settings are restored in `finally` no matter how the loop exits."""
    is_tty = sys.stdout.isatty()
    fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    saved_tty = None
    if fd is not None:
        try:
            import termios
            import tty
            saved_tty = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            fd = None

    def emit(line: str) -> None:
        if is_tty:
            sys.stdout.write("\r\033[K")  # wipe the footer line
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
            sys.stdout.write(_FOOTER)
            sys.stdout.flush()
        else:
            print(line, end="")

    if is_tty:
        sys.stdout.write(_FOOTER)
        sys.stdout.flush()
    else:
        print(_FOOTER)

    try:
        while True:
            line = f.readline()
            if line:
                emit(line)
                continue
            if fd is not None:
                import select
                ready, _, _ = select.select([sys.stdin], [], [], 0.3)
                if ready and sys.stdin.read(1).lower() in ("q", "\x1b"):
                    return
            else:
                time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        if is_tty:
            sys.stdout.write("\r\033[K\n")  # wipe the footer, leave a clean line
            sys.stdout.flush()
        if fd is not None and saved_tty is not None:
            import termios
            termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
