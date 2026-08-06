"""Tests for GET /api/skills/-/budget (skill context budget endpoint)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from kiro_crew.skill_usage import SkillUsageLedger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(tmp_path: Path, key: str, body: str = "# Skill\nHello") -> Path:
    """Create a SKILL.md under tmp_path/<key>/SKILL.md and return the file."""
    d = tmp_path / key
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body)
    return f


def _make_ledger(tmp_path: Path, keys: dict[str, tuple[int, float]]) -> SkillUsageLedger:
    """Build a ledger pre-populated with hits/last_seen per key."""
    path = tmp_path / "skill-usage.json"
    payload = {
        "version": 1,
        "keys": {k: {"hits": h, "last_seen": ls} for k, (h, ls) in keys.items()},
    }
    path.write_text(json.dumps(payload))
    return SkillUsageLedger(path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAliasFold:
    """Two ledger keys resolving to one file are folded."""

    def test_fold_sums_deliveries_and_reports_alias(self, tmp_path):
        """Symlinked alias key's hits are added to the canonical key."""
        from kiro_crew.dashboard.handlers.skill_budget import (
            _compute_budget,
        )

        # Create the canonical skill file.
        canonical_file = _make_skill(tmp_path, "ns/pod-e2e", "# Pod\nbody here!!")

        # Create a symlink at the root level (the alias path).
        alias_dir = tmp_path / "pod-e2e"
        alias_dir.mkdir()
        alias_skill = alias_dir / "SKILL.md"
        os.symlink(str(canonical_file), str(alias_skill))

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "ns/pod-e2e": (262, now - 3600),
            "pod-e2e": (52, now - 7200),
        })

        # Build a minimal SkillsLoader mock.
        skill_pairs = [("ns/pod-e2e", canonical_file)]

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            def _iter(self):
                return skill_pairs

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Pod E2e"}

            def _owned_hint(self, path):
                return True

        result = _compute_budget(FakeLoader())

        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["key"] == "ns/pod-e2e"
        assert row["deliveries"] == 314  # 262 + 52
        assert row["folded_from"] == ["pod-e2e"]
        assert row["chars"] == row["size_bytes"] * 314

    def test_unresolvable_ledger_key_is_dropped(self, tmp_path):
        """A ledger key whose SKILL.md doesn't exist is not folded."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        canonical_file = _make_skill(tmp_path, "real-skill", "# Real\ncontent")

        now = time.time()
        # "ghost-skill" has no SKILL.md on disk at all.
        ledger = _make_ledger(tmp_path, {
            "real-skill": (10, now - 100),
            "ghost-skill": (999, now - 50),
        })

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            def _iter(self):
                return [("real-skill", canonical_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Real Skill"}

            def _owned_hint(self, path):
                return True

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        # The ghost key must NOT appear in deliveries or folded_from.
        assert row["deliveries"] == 10
        assert "folded_from" not in row

    def test_untracked_skill_gives_null_deliveries(self, tmp_path):
        """A skill with no ledger entry yields deliveries=None, not 0."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill_file = _make_skill(tmp_path, "brand-new", "# New\nstuff")

        # Empty ledger — no keys at all.
        ledger = _make_ledger(tmp_path, {})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            def _iter(self):
                return [("brand-new", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Brand New"}

            def _owned_hint(self, path):
                return True

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        assert row["deliveries"] is None
        assert row["idle_days"] is None
        assert row["chars"] == 0

    def test_chars_arithmetic(self, tmp_path):
        """chars = size_bytes * deliveries."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        body = "x" * 500
        skill_file = _make_skill(tmp_path, "measured", body)
        size = skill_file.stat().st_size

        now = time.time()
        ledger = _make_ledger(tmp_path, {"measured": (7, now - 60)})

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            def _iter(self):
                return [("measured", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Measured"}

            def _owned_hint(self, path):
                return True

        result = _compute_budget(FakeLoader())

        row = result["rows"][0]
        assert row["chars"] == size * 7
        assert row["deliveries"] == 7

    def test_total_chars_equals_row_sum(self, tmp_path):
        """total_chars is the sum of all row chars."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        f1 = _make_skill(tmp_path, "alpha", "aaa")
        f2 = _make_skill(tmp_path, "beta", "bbbbb")

        now = time.time()
        ledger = _make_ledger(tmp_path, {
            "alpha": (3, now),
            "beta": (2, now),
        })

        class FakeLoader:
            _usage = ledger
            _dir = tmp_path

            def _iter(self):
                return [("alpha", f1), ("beta", f2)]

            def _cached_frontmatter(self, path, mtime=None):
                return {}

            def _owned_hint(self, path):
                return True

        result = _compute_budget(FakeLoader())

        row_sum = sum(r["chars"] for r in result["rows"])
        assert result["total_chars"] == row_sum

    def test_unreadable_ledger_degrades_gracefully(self, tmp_path):
        """When ledger is None, all deliveries are None and no crash."""
        from kiro_crew.dashboard.handlers.skill_budget import _compute_budget

        skill_file = _make_skill(tmp_path, "orphan", "# Hi")

        class FakeLoader:
            _usage = None  # Ledger unavailable
            _dir = tmp_path

            def _iter(self):
                return [("orphan", skill_file)]

            def _cached_frontmatter(self, path, mtime=None):
                return {"name": "Orphan"}

            def _owned_hint(self, path):
                return True

        result = _compute_budget(FakeLoader())

        assert result["window_days"] == 30
        row = result["rows"][0]
        assert row["deliveries"] is None
        assert row["idle_days"] is None
        assert row["chars"] == 0
        assert result["total_chars"] == 0


class TestSnapshotMethod:
    """SkillUsageLedger.snapshot() returns a consistent copy."""

    def test_snapshot_returns_all_keys(self, tmp_path):
        ledger = _make_ledger(tmp_path, {
            "a": (5, time.time()),
            "b": (3, time.time() - 100),
        })
        snap = ledger.snapshot()
        assert set(snap.keys()) == {"a", "b"}
        assert snap["a"][0] == 5
        assert snap["b"][0] == 3

    def test_snapshot_empty_ledger(self, tmp_path):
        ledger = _make_ledger(tmp_path, {})
        assert ledger.snapshot() == {}
