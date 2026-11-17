"""v1 Pulse: deterministic on-demand cards from core session/cron state.

HONEST SCOPE: this is NOT upstream's proactive Pulse engine (persona, work
watch, follow-up turn generation, quiet hours). It is a minimal, deterministic
on-demand brief — zero LLM calls, zero writes. Every card is derived from a
read-only snapshot of the profile's ``state.db`` session rows and
``cron/jobs.json``.

All state reads are fail-soft: a missing or unreadable store contributes zeros
instead of raising, because this runs inside the shared plugin host.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter

router = APIRouter()

_E24H = 86400.0


@dataclass
class PulseCard:
    id: str
    title: str
    body: str
    kind: str
    created_at: str  # ISO-8601 UTC
    actionable: bool


def _default_home() -> Path:
    """Hermes home via core when importable, else HERMES_HOME, else ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def resolve_home(profile: Optional[str] = None) -> Path:
    """Profile home: ``<root>/profiles/<profile>``, or the root itself."""
    root = _default_home()
    if not profile:
        return root
    candidate = root / "profiles" / str(profile).strip()
    return candidate if candidate.is_dir() else root


# ── read-only state snapshots ────────────────────────────────────────────────

def _read_cron_jobs(home: Path) -> list[dict[str, Any]]:
    path = home / "cron" / "jobs.json"
    try:
        # utf-8-sig matches cron/jobs.py's dialect (BOM'd files exist in the wild).
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def _session_counts(home: Path, now: float) -> dict[str, int]:
    """{started_e24h, total} from the sessions table; {} if unreadable."""
    db = home / "state.db"
    if not db.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        day_start = datetime.fromtimestamp(now, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        started_e24h = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (now - _E24H,)
        ).fetchone()[0]
        started_today = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (day_start,)
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return {
            "started_e24h": int(started_e24h),
            "started_today": int(started_today),
            "total": int(total),
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# ── card synthesis (deterministic; no LLM) ───────────────────────────────────

async def build_cards(profile: Optional[str] = None) -> list[PulseCard]:
    home = resolve_home(profile)
    now = time.time()
    created_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

    jobs = _read_cron_jobs(home)
    enabled = [j for j in jobs if j.get("enabled", True)]
    failed = [
        j for j in enabled
        if str(j.get("last_status") or "").lower() in ("failed", "error")
    ]
    counts = _session_counts(home, now)

    cards: list[PulseCard] = []

    sess_part = (
        f"{counts.get('started_today', 0)} session(s) today, "
        f"{counts.get('started_e24h', 0)} in the last 24h"
        if counts else "session state unavailable"
    )
    cards.append(PulseCard(
        id="brief",
        title="Portal brief",
        body=(
            f"{sess_part}. "
            f"{len(enabled)} cron jobs enabled across {len(jobs)} total"
            + (f", {len(failed)} with a failed last run." if failed else ".")
        ),
        kind="brief",
        created_at=created_at,
        actionable=False,
    ))

    if failed:
        names = ", ".join(str(j.get("name") or j.get("id") or "?") for j in failed)
        cards.append(PulseCard(
            id="cron-failures",
            title=f"{len(failed)} cron job(s) failing",
            body=f"Last run failed for: {names}.",
            kind="cron_attention",
            created_at=created_at,
            actionable=False,
        ))

    return cards


def _cards_payload(profile: Optional[str] = None) -> dict[str, Any]:
    cards = _run_sync(build_cards(profile))
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cards": [asdict(c) for c in cards],
    }


def _run_sync(coro) -> Any:
    """Await ``coro`` from sync context; safe when a loop is already running
    (the tool handler is registered with is_async=True, so normally this path
    only runs outside a loop)."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Loop already running and we were called synchronously: run in a worker loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ── HTTP surface (auto-mounted at /api/plugins/portal/) ─────────────────────

@router.get("/pulse")
async def pulse_feed():
    return _cards_payload()


def register_routes(target: APIRouter) -> None:
    """Copy this module's routes onto the plugin's top-level router.

    Flat registration by convention: FastAPI >=0.163 nests ``include_router``
    results behind opaque ``_IncludedRouter`` branches, which breaks route
    introspection in tests and host tooling.
    """
    for route in router.routes:
        target.add_api_route(
            route.path,          # type: ignore[attr-defined]  # APIRoute
            route.endpoint,      # type: ignore[attr-defined]
            methods=list(route.methods or ["GET"]),   # type: ignore[attr-defined]
            name=route.name,     # type: ignore[attr-defined]
        )


# ── agent tool ────────────────────────────────────────────────────────────────

_PULSE_GET_SCHEMA = {
    "name": "pulse_get",
    "description": (
        "Build the deterministic Portal Pulse card feed (on-demand brief + "
        "cron-attention cards) from core session and cron state. No LLM."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "description": "Optional profile name; defaults to this host profile.",
            }
        },
    },
}


async def _pulse_get_handler(args: dict, **_kwargs) -> str:
    profile = (args or {}).get("profile")
    return json.dumps(_cards_payload(profile), ensure_ascii=False)


def register_tools(ctx) -> list[str]:
    """Register plugin tools on a plugin-host ctx. Returns the tool names."""
    ctx.register_tool(
        "pulse_get",
        toolset="portal",
        schema=_PULSE_GET_SCHEMA,
        handler=_pulse_get_handler,
        is_async=True,
        description="Deterministic Portal Pulse card feed (no LLM).",
        emoji="📟",
    )
    return ["pulse_get"]
