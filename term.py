"""Opt-in remote terminal: host-side PTY session manager + routes.

Plan 2026-11-18-remote-terminal.md, Task 1. The Portal app drives one
interactive shell on the Hermes host over the existing authenticated
transport. The plugin host serves this module's routes through ``op_asgi``
(httpx.ASGITransport, complete bodies), so the transport here is REST +
a <=2 s long-poll on the output route — no WebSocket, no streaming.

Privacy / safety model (plan Global Constraints):

* **Off by default.** The host opt-in flag ``<hermes home>/plugins/portal/
  term/opt-in`` is the authority (mirroring ``phone.py``); it is flipped only
  from the owner's shell (``python -m portal.term opt-in``), never over HTTP.
* **No keystroke logging.** ``audit.log`` records session lifecycle only
  (event, session id, start/stop timestamp, exit code). Command and output
  bytes go only to the user's own PTY and are never persisted here.
* **Bounded exposure.** Max 2 live sessions, a 256 KiB output ring buffer per
  session (tail-kept, cursor-clamped), and a 10-minute idle reaper (any
  client poll counts as an attached poll; clock injectable for tests).

The PTY is spawned in-process (``plugins.isolation: host`` — the plugin runs
inside the Hermes process on the Linux host). One reader thread per session
drains the master fd into the ring buffer; ``wait()`` on the child sets
``eof`` + ``exit_code``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import fcntl
import json
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- limits

MAX_SESSIONS = 2                    # live PTY sessions per profile
IDLE_TIMEOUT_SECONDS = 600          # 10 min without input or attached poll
RING_BYTES = 256 * 1024             # per-session output ring (tail kept)
LONG_POLL_SECONDS = 2.0             # GET /output holds at most this long
POLL_SLICE_SECONDS = 0.05           # long-poll wake granularity
KILL_ESCALATION_SECONDS = 0.5       # SIGHUP -> SIGKILL grace
EVICT_AFTER_EOF_SECONDS = 3600      # drained sessions stop answering 410 after this
READ_CHUNK = 65536

OPT_IN_FILENAME = "opt-in"
AUDIT_FILENAME = "audit.log"

_ERR_OPT_IN = (
    "portal remote terminal is opt-in and this profile has not opted in. "
    "The profile owner runs `python -m portal.term opt-in` on the Hermes "
    "host to enable it."
)


# --------------------------------------------------------------------------
# profile-scoped paths (same context-local resolution as phone.py)
# --------------------------------------------------------------------------

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"
    return Path(get_hermes_home())


def term_dir(home: Optional[Path] = None) -> Path:
    base = _hermes_home() if home is None else home
    return base / "plugins" / "portal" / "term"


def is_opted_in(home: Optional[Path] = None) -> bool:
    """Opt-in flag presence. Off (missing) unless explicitly created."""
    return (term_dir(home) / OPT_IN_FILENAME).is_file()


def set_opt_in(enabled: bool, home: Optional[Path] = None) -> bool:
    """Create/remove the opt-in flag. Owner-side action only (CLI / shell);
    deliberately *not* reachable over HTTP — consent changes must not have a
    network-reachable mutation path."""
    flag = term_dir(home) / OPT_IN_FILENAME
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n",
                        encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)
    return flag.is_file()


# --------------------------------------------------------------------------
# injectable clock (idle reaper is testable without sleeping)
# --------------------------------------------------------------------------

_clock: Callable[[], float] = time.monotonic
_clock_lock = threading.Lock()


def set_clock(fn: Callable[[], float]) -> None:
    """Swap the session-idle clock (tests inject a fake one)."""
    global _clock
    with _clock_lock:
        _clock = fn


def _now() -> float:
    with _clock_lock:
        return _clock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# audit (lifecycle only — NEVER command/output bytes)
# --------------------------------------------------------------------------

def _audit(event: str, session_id: str, **fields) -> None:
    """Append one lifecycle row. Best-effort: an audit write failure must
    never kill the user's own shell, and must never leak PTY bytes anyway
    (we only ever serialize the fields passed here)."""
    row = {"event": event, "session_id": session_id, "ts": _now_iso(), **fields}
    try:
        path = term_dir() / AUDIT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - disk-full etc; lifecycle proceeds
        pass


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

class _SessionFull(Exception):
    pass


class _Session:
    """One live PTY session: master fd, ring buffer, lifecycle flags.

    Locking: ``lock`` guards ring/cursor state; ``io_lock`` guards the master
    fd so a client write can never race the reader thread's close (and so an
    ``eof`` observation implies the fd is already closed — no writes to a
    recycled fd number).
    """

    def __init__(self, session_id: str, proc: subprocess.Popen, master_fd: int,
                 cols: int, rows: int) -> None:
        self.id = session_id
        self.proc = proc
        self.master_fd = master_fd
        self.cols = cols
        self.rows = rows
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.buf = bytearray()
        self.start_abs = 0          # absolute byte offset of buf[0]
        self.total = 0              # total bytes ever produced (== end offset)
        self.eof = False
        self.eof_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.reported_eof = False
        self.killing = False
        self.last_activity = _now()

    # -- activity tracking (input or an attached output poll) -------------

    def note_activity(self) -> None:
        self.last_activity = _now()

    # -- ring buffer -------------------------------------------------------

    def append(self, data: bytes) -> None:
        with self.lock:
            self.buf.extend(data)
            self.total += len(data)
            if len(self.buf) > RING_BYTES:
                drop = len(self.buf) - RING_BYTES
                del self.buf[:drop]
                self.start_abs += drop

    def read_since(self, cursor: int) -> tuple[bytes, int, bool, Optional[int]]:
        """(bytes after ``cursor``, new cursor, eof, exit_code). Cursors older
        than the ring (or the naive default 0) clamp to the oldest kept byte."""
        with self.lock:
            cursor = max(0, min(int(cursor), self.total))
            cursor = max(cursor, self.start_abs)
            data = bytes(self.buf[cursor - self.start_abs:])
            return data, self.total, self.eof, self.exit_code

    def mark_eof(self, exit_code: Optional[int]) -> None:
        with self.lock:
            self.eof = True
            self.exit_code = exit_code
            self.eof_at = _now()

    def alive(self) -> bool:
        with self.lock:
            return not self.eof


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _get(session_id: str) -> Optional[_Session]:
    with _sessions_lock:
        return _sessions.get(session_id)


def _spawn_shell(cwd: Optional[str], cols: int, rows: int) -> _Session:
    home = os.environ.get("HOME") or str(Path.home())
    cwd = cwd or home
    if not os.path.isdir(cwd):
        raise NotADirectoryError(cwd)
    shell = os.environ.get("SHELL") or "/bin/bash"
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    try:
        proc = subprocess.Popen(
            [shell, "-i"],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=cwd, env=env,
            start_new_session=True,   # own session/pgroup -> killpg escalates cleanly
        )
    except OSError:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    session_id = secrets.token_hex(8)
    return _Session(session_id, proc, master_fd, cols, rows)


def _start_session(cwd: Optional[str], cols: int, rows: int) -> _Session:
    with _sessions_lock:
        live = sum(1 for s in _sessions.values() if s.alive())
        if live >= MAX_SESSIONS:
            raise _SessionFull(f"terminal session limit reached (max {MAX_SESSIONS} live)")
        sess = _spawn_shell(cwd, cols, rows)
        _sessions[sess.id] = sess
    _audit("start", sess.id, start=_now_iso())
    threading.Thread(target=_reader_loop, args=(sess,),
                     daemon=True, name=f"portal-term-{sess.id}").start()
    _ensure_reaper()
    return sess


def _reader_loop(sess: _Session) -> None:
    """Drain the PTY master into the ring buffer until EOF, then reap the
    child and stamp eof + exit_code (which is what the output route reads)."""
    exit_code: Optional[int] = None
    try:
        while True:
            try:
                chunk = os.read(sess.master_fd, READ_CHUNK)
            except OSError as exc:
                # Linux PTY masters report EIO once the last slave closer is
                # gone; anything else is an equally terminal read failure.
                if exc.errno not in (errno.EIO, errno.EBADF):
                    pass
                break
            if not chunk:
                break
            sess.append(chunk)
    finally:
        with sess.io_lock:      # serialize against in-flight client writes
            try:
                os.close(sess.master_fd)
            except OSError:
                pass
        try:
            exit_code = sess.proc.wait(timeout=30)
        except Exception:  # pragma: no cover - child wedged past kill
            exit_code = None
        sess.mark_eof(exit_code)
        _audit("stop", sess.id, stop=_now_iso(), exit_code=exit_code)


def _terminate(sess: _Session) -> None:
    """SIGHUP the session's whole process group, SIGKILL after a grace."""
    with sess.lock:
        if sess.killing:
            return
        sess.killing = True

    def _signal_group(sig: int) -> None:
        try:
            os.killpg(sess.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                sess.proc.send_signal(sig)
            except (ProcessLookupError, PermissionError):
                pass

    _signal_group(signal.SIGHUP)

    def _escalate() -> None:
        if sess.proc.poll() is None:
            _signal_group(signal.SIGKILL)

    timer = threading.Timer(KILL_ESCALATION_SECONDS, _escalate)
    timer.daemon = True
    timer.start()


# --------------------------------------------------------------------------
# idle reaper (clock injectable via set_clock)
# --------------------------------------------------------------------------

def reap_once() -> int:
    """One reaper sweep: kill sessions idle past IDLE_TIMEOUT_SECONDS and
    evict long-drained ones. Returns how many sessions were killed."""
    now = _now()
    killed = 0
    with _sessions_lock:
        for sid in list(_sessions):
            sess = _sessions[sid]
            if sess.eof_at is not None:
                if now - sess.eof_at > EVICT_AFTER_EOF_SECONDS:
                    del _sessions[sid]
                continue
            if now - sess.last_activity > IDLE_TIMEOUT_SECONDS:
                _terminate(sess)
                killed += 1
    return killed


_reaper_started = False


def _ensure_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def _loop() -> None:
        while True:
            time.sleep(5.0)
            try:
                reap_once()
            except Exception:  # pragma: no cover - reaper must never die
                pass

    threading.Thread(target=_loop, daemon=True, name="portal-term-reaper").start()


def shutdown_all() -> None:
    """Kill every session and forget it (test teardown / host shutdown)."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for sess in sessions:
        _terminate(sess)
    for sess in sessions:
        try:
            sess.proc.wait(timeout=5)
        except Exception:
            try:
                sess.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# HTTP routes (mounted by portal.router at /api/plugins/portal/)
# --------------------------------------------------------------------------

router = APIRouter()


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


class _StartBody(BaseModel):
    cwd: Optional[str] = None
    cols: int = Field(ge=1, le=10000)
    rows: int = Field(ge=1, le=10000)


class _InputBody(BaseModel):
    data: str


class _ResizeBody(BaseModel):
    cols: int = Field(ge=1, le=10000)
    rows: int = Field(ge=1, le=10000)


def register_routes(router: APIRouter) -> None:
    """Mount the terminal routes flat onto the portal router.

    Deliberately not ``router.include_router(...)`` (same rationale as
    phone.py): FastAPI >=0.163 nests included routers as opaque
    ``_IncludedRouter`` branches, breaking structural assertions on
    ``portal.router.routes``.
    """

    @router.post("/term/start", status_code=201)
    async def term_start(body: _StartBody):
        if not is_opted_in():
            return _err(403, _ERR_OPT_IN)
        try:
            sess = _start_session(body.cwd, body.cols, body.rows)
        except _SessionFull as exc:
            return _err(429, str(exc))
        except NotADirectoryError:
            return _err(400, "cwd does not exist")
        except OSError as exc:
            return _err(500, f"failed to spawn shell: {exc}")
        return JSONResponse(status_code=201, content={"session_id": sess.id})

    @router.post("/term/{session_id}/input", status_code=204)
    async def term_input(session_id: str, body: _InputBody):
        sess = _get(session_id)
        if sess is None:
            return _err(404, "unknown session")
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError):
            return _err(400, "data must be strict base64")
        sess.note_activity()
        # The plugin never interprets these bytes — straight to the PTY.
        with sess.io_lock:
            if not sess.eof:
                try:
                    os.write(sess.master_fd, data)
                except OSError:
                    pass  # child died between the eof check and the write
        return Response(status_code=204)

    @router.get("/term/{session_id}/output")
    async def term_output(session_id: str, since: int = 0):
        sess = _get(session_id)
        if sess is None:
            return _err(404, "unknown session")
        if since < 0:
            return _err(400, "since must be >= 0")
        sess.note_activity()  # an attached poll keeps the session alive
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LONG_POLL_SECONDS
        while True:
            data, cursor, eof, exit_code = sess.read_since(since)
            if data:
                if eof:
                    sess.reported_eof = True
                return {"data": base64.b64encode(data).decode(), "cursor": cursor,
                        "eof": eof, "exit_code": exit_code}
            if eof:
                if sess.reported_eof:
                    return _err(410, "terminal session ended; output fully drained")
                sess.reported_eof = True
                return {"data": "", "cursor": cursor, "eof": True,
                        "exit_code": exit_code}
            remaining = deadline - loop.time()
            if remaining <= 0:
                # Long-poll window elapsed with no new bytes: empty page,
                # client re-polls from the same cursor.
                return {"data": "", "cursor": cursor, "eof": False,
                        "exit_code": None}
            await asyncio.sleep(min(remaining, POLL_SLICE_SECONDS))

    @router.post("/term/{session_id}/resize", status_code=204)
    async def term_resize(session_id: str, body: _ResizeBody):
        sess = _get(session_id)
        if sess is None:
            return _err(404, "unknown session")
        sess.note_activity()
        with sess.io_lock:
            if not sess.eof:
                try:
                    fcntl.ioctl(sess.master_fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", body.rows, body.cols, 0, 0))
                    sess.cols, sess.rows = body.cols, body.rows
                except OSError:
                    pass
        return Response(status_code=204)

    @router.delete("/term/{session_id}", status_code=204)
    async def term_delete(session_id: str):
        sess = _get(session_id)
        if sess is None:
            return _err(404, "unknown session")
        _terminate(sess)  # reader thread marks eof; output drains then 410s
        return Response(status_code=204)

    @router.get("/term/status")
    async def term_status():
        with _sessions_lock:
            sessions = [{"session_id": s.id, "eof": s.eof, "exit_code": s.exit_code}
                        for s in _sessions.values()]
        return {"enabled": is_opted_in(), "sessions": sessions}


def make_test_client():
    """A FastAPI app exactly like the host's plugin-host mount (op_asgi:
    ``FastAPI()`` + ``include_router(router)``), for manual probes. TestClient
    import deferred so this test-only dependency never breaks a production
    plugin load."""
    from fastapi import FastAPI  # local: test-only mount
    from fastapi.testclient import TestClient  # local: test-only dep

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --------------------------------------------------------------------------
# owner-side CLI: opt-in / opt-out / status (never over HTTP)
# --------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    usage = "usage: python -m portal.term {opt-in|opt-out|status}"
    if not argv:
        print(usage)
        return 2
    action = argv[0]
    if action == "status":
        print(f"terminal: {'ON' if is_opted_in() else 'off'}")
        return 0
    if action in ("opt-in", "opt-out"):
        enabled = set_opt_in(action == "opt-in")
        print("terminal: opted IN — the Portal app may open shell sessions"
              if enabled else "terminal: opted out — /term/start returns 403")
        return 0
    print(usage)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
