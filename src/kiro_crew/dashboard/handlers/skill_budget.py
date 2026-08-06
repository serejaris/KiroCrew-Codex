"""GET /api/skills/budget — per-skill 30-day injection cost with alias folding.

This is the *control-plane* read endpoint powering the Context Budget screen.
Unlike ``list_skills()`` (runs on the event loop during context assembly and
must be O(skills) with no extra IO), this does deliberate filesystem work
(``Path.resolve()`` per ledger key) and caches the expensive alias map so
repeated dashboard refreshes are cheap.

The fold logic lives here — not in ``list_skills()`` — because:
1. ``list_skills()`` guarantees one stat per skill (its docstring contract) and
   runs on the hot path; the fold needs per-ledger-key path resolution.
2. The fold is a user-action-frequency operation (dashboard page load), not a
   per-message one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import discovery_executor
from kiro_crew.skill_usage import SkillUsageLedger

from ._shared import _get_skills

logger = logging.getLogger(__name__)

#: 30-day window matching the ledger's _MAX_AGE_SECS.
_WINDOW_DAYS = 30


def _build_alias_map(
    ledger: SkillUsageLedger,
    skill_pairs: list[tuple[str, Path]],
) -> dict[str, list[str]]:
    """Map each served skill key to ledger keys that resolve to the same file.

    Returns ``{served_key: [alias_key, ...]}`` — only entries with at least one
    alias appear. Unresolvable ledger keys (no SKILL.md on disk) are dropped.
    """
    snapshot = ledger.snapshot()
    if not snapshot:
        return {}

    # Build realpath → served_key from the authoritative skill list.
    realpath_to_served: dict[str, str] = {}
    for key, skill_file in skill_pairs:
        try:
            rp = str(skill_file.resolve())
        except OSError:
            continue
        realpath_to_served[rp] = key

    # Served keys already in the ledger — not aliases.
    served_key_set = {key for key, _ in skill_pairs}

    alias_map: dict[str, list[str]] = {}
    skills_dir_parent = skill_pairs[0][1].parent.parent if skill_pairs else None
    if skills_dir_parent is None:
        return {}

    for ledger_key in snapshot:
        if ledger_key in served_key_set:
            continue  # Not an alias — it's the canonical key itself.
        # Try to resolve this ledger key to a SKILL.md on disk.
        candidate = skills_dir_parent / ledger_key / "SKILL.md"
        try:
            rp = str(candidate.resolve())
        except OSError:
            continue  # Unresolvable — drop (orphan key).
        if not Path(rp).exists():
            continue  # SKILL.md.pre-relocation or similar — drop.
        served_key = realpath_to_served.get(rp)
        if served_key is None:
            continue  # Resolves to a file not in the served set — drop.
        alias_map.setdefault(served_key, []).append(ledger_key)

    return alias_map


def _compute_budget(
    skills_loader: Any,
) -> dict[str, Any]:
    """Blocking function that computes the full budget response.

    Runs on the discovery executor, off the event loop.
    """
    skill_pairs = skills_loader._iter()
    ledger: SkillUsageLedger | None = skills_loader._usage

    # Build the alias map (cached on the ledger's key set).
    alias_map: dict[str, list[str]] = {}
    ledger_snapshot: dict[str, tuple[int, float]] = {}
    if ledger is not None:
        try:
            ledger_snapshot = ledger.snapshot()
            alias_map = _build_alias_map(ledger, skill_pairs)
        except Exception:
            logger.warning("skill-budget: alias map build failed", exc_info=True)

    now = time.time()
    rows: list[dict[str, Any]] = []
    total_chars = 0

    for key, skill_file in skill_pairs:
        # Size
        try:
            st = skill_file.stat()
            size_bytes = st.st_size
        except OSError:
            size_bytes = 0

        # Frontmatter
        meta = skills_loader._cached_frontmatter(skill_file)

        # Deliveries: own key + aliases
        deliveries: int | None = None
        idle_days: float | None = None

        if ledger is not None:
            own_entry = ledger_snapshot.get(key)
            alias_keys = alias_map.get(key, [])

            if own_entry is not None or alias_keys:
                total_hits = 0
                latest_seen = 0.0
                if own_entry is not None:
                    total_hits += own_entry[0]
                    latest_seen = max(latest_seen, own_entry[1])
                for ak in alias_keys:
                    alias_entry = ledger_snapshot.get(ak)
                    if alias_entry is not None:
                        total_hits += alias_entry[0]
                        latest_seen = max(latest_seen, alias_entry[1])
                deliveries = total_hits if total_hits > 0 else (
                    0 if (own_entry is not None or alias_keys) else None
                )
                # idle_days: days since last_seen
                if latest_seen > 0:
                    idle_days = (now - latest_seen) / 86400.0
                    idle_days = round(idle_days, 1)
                else:
                    idle_days = None
            else:
                # No ledger entry at all — untracked.
                deliveries = None
                idle_days = None
        else:
            # No ledger available.
            deliveries = None
            idle_days = None

        # chars = size_bytes * (deliveries or 0)
        chars = size_bytes * (deliveries if deliveries is not None else 0)
        total_chars += chars

        # Determine source
        try:
            owned = skills_loader._owned_hint(skill_file)
        except Exception:
            owned = False
        source = "kirocrew" if owned else "external"

        row: dict[str, Any] = {
            "key": key,
            "name": meta.get("name", key),
            "size_bytes": size_bytes,
            "deliveries": deliveries,
            "chars": chars,
            "inject_on_trigger": (
                meta.get("inject_on_trigger", "").strip().lower() != "false"
            ),
            "always": meta.get("always", "").lower() == "true",
            "owned": owned,
            "source": source,
            "idle_days": idle_days,
        }

        # folded_from: alias keys whose SKILL.md resolves to the same file
        folded = alias_map.get(key, [])
        if folded:
            row["folded_from"] = sorted(folded)

        rows.append(row)

    return {
        "window_days": _WINDOW_DAYS,
        "total_chars": total_chars,
        "rows": rows,
    }


async def api_skills_budget(request: web.Request) -> web.Response:
    """GET /api/skills/budget — per-skill 30-day injection cost.

    Offloads the blocking filesystem work (path resolution for alias folding,
    frontmatter reads) to the discovery executor, same as GET /api/skills.
    """
    state: DashboardState = request.app["state"]
    skills = _get_skills(state)

    result = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(),
        _compute_budget,
        skills,
    )
    return web.json_response(result)
