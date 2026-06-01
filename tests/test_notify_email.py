"""Tests for testing_mode resolution in scripts/notify_email.py.

The SMTP / compose / send paths are integration territory (exercised against
`contractor-engine-test` during Phase 2 E2E). These tests cover the per-repo
`testing_mode` precedence, which decides whether PSL is ever contacted — so
the fail-safe direction (never email PSL unless explicitly told to) matters.
"""
from __future__ import annotations

from pathlib import Path

from scripts.notify_email import _effective_testing_mode

# Sentinel: leave testing_mode out of the engine fiscal-host.yml entirely.
_OMIT = object()

_ENGINE_DEFAULT = "engine fiscal-host.yml default"
_REPO = "repo settings.yml"


def _write_fiscal_host(tmp_path: Path, testing_mode) -> Path:
    """Write a minimal fiscal-host.yml. `testing_mode=_OMIT` leaves the key
    out of the notifications block; otherwise it is written as a YAML bool."""
    path = tmp_path / "fiscal-host.yml"
    if testing_mode is _OMIT:
        path.write_text("notifications: {}\n", encoding="utf-8")
    else:
        path.write_text(
            f"notifications:\n  testing_mode: {str(testing_mode).lower()}\n",
            encoding="utf-8",
        )
    return path


class TestEffectiveTestingMode:
    def test_repo_false_overrides_engine_true(self, tmp_path):
        fh = _write_fiscal_host(tmp_path, True)
        settings = {"notifications": {"testing_mode": False}}
        assert _effective_testing_mode(settings, fh) == (False, _REPO)

    def test_repo_true_overrides_engine_false(self, tmp_path):
        fh = _write_fiscal_host(tmp_path, False)
        settings = {"notifications": {"testing_mode": True}}
        assert _effective_testing_mode(settings, fh) == (True, _REPO)

    def test_engine_false_used_when_repo_silent(self, tmp_path):
        fh = _write_fiscal_host(tmp_path, False)
        assert _effective_testing_mode({}, fh) == (False, _ENGINE_DEFAULT)

    def test_engine_true_used_when_repo_silent(self, tmp_path):
        fh = _write_fiscal_host(tmp_path, True)
        assert _effective_testing_mode({}, fh) == (True, _ENGINE_DEFAULT)

    def test_failsafe_true_when_engine_file_absent(self, tmp_path):
        missing = tmp_path / "does-not-exist.yml"
        assert _effective_testing_mode({}, missing) == (True, _ENGINE_DEFAULT)

    def test_engine_notifications_without_key_defaults_true(self, tmp_path):
        fh = _write_fiscal_host(tmp_path, _OMIT)
        assert _effective_testing_mode({}, fh) == (True, _ENGINE_DEFAULT)

    def test_repo_notifications_without_testing_mode_falls_through(self, tmp_path):
        # A notifications block that doesn't set testing_mode is not an override.
        fh = _write_fiscal_host(tmp_path, False)
        settings = {"notifications": {"something_else": 1}}
        assert _effective_testing_mode(settings, fh) == (False, _ENGINE_DEFAULT)

    def test_none_notifications_block_falls_through(self, tmp_path):
        # `notifications:` with nothing under it parses to None, not a dict.
        fh = _write_fiscal_host(tmp_path, True)
        assert _effective_testing_mode({"notifications": None}, fh) == (True, _ENGINE_DEFAULT)

    def test_repo_override_coerced_to_bool(self, tmp_path):
        # YAML may carry truthy/falsy non-bools; the resolver normalises them.
        fh = _write_fiscal_host(tmp_path, True)
        assert _effective_testing_mode({"notifications": {"testing_mode": 0}}, fh)[0] is False
        assert _effective_testing_mode({"notifications": {"testing_mode": 1}}, fh)[0] is True
