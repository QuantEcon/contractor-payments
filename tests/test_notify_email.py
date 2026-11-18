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
    elif testing_mode is None:
        # `testing_mode:` with no value — parses to None; must fail safe.
        path.write_text("notifications:\n  testing_mode: null\n", encoding="utf-8")
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

    def test_engine_null_is_failsafe_true(self, tmp_path):
        # `testing_mode: null` in the engine file must NOT coerce to production.
        fh = _write_fiscal_host(tmp_path, None)
        assert _effective_testing_mode({}, fh) == (True, _ENGINE_DEFAULT)

    def test_repo_null_falls_through_not_production(self, tmp_path):
        # A present-but-null repo override is "unset" — it falls through to the
        # engine default rather than silently enabling production routing.
        fh = _write_fiscal_host(tmp_path, True)
        settings = {"notifications": {"testing_mode": None}}
        assert _effective_testing_mode(settings, fh) == (True, _ENGINE_DEFAULT)


# ─── Sent once (issue #11) ──────────────────────────────────────────────────

import json

import pytest
import yaml

import scripts.notify_email as ne


class TestSentOnce:
    """`Re-run failed jobs` replays the email step when something after it
    failed. The send is recorded in the submission YAML so the re-run reports
    the earlier send instead of emailing the fiscal host a second time."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        for k, v in {
            "SMTP_HOST": "smtp.example.org", "SMTP_PORT": "587", "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "SMTP_FROM": "payments@example.org",
            "QUANTECON_EMAIL_REVIEWER": "reviewer@example.org",
            "PSL_EMAIL": "psl@example.org",
        }.items():
            monkeypatch.setenv(k, v)
        sent = []
        monkeypatch.setattr(ne, "send_message", lambda msg, **kw: sent.append(msg))
        sub = tmp_path / "sub.yml"
        sub.write_text(yaml.safe_dump({
            "submission_id": "jdoe-timesheet-2026-05", "type": "timesheet",
            "contract_id": "QE-PSL-2026-001", "period": "2026-05",
            "status": "approved", "approved_by": "mmcky", "approved_date": "2026-06-02",
            "totals": {"hours": 10.0, "rate": 50.0, "amount": 500.0, "currency": "AUD"},
        }, sort_keys=False))
        settings = tmp_path / "settings.yml"
        settings.write_text(yaml.safe_dump({"contractor": {"name": "Jane Doe", "github": "jdoe"}}))
        pdf = tmp_path / "sub.pdf"
        pdf.write_bytes(b"%PDF-1.7 approved")
        fiscal = _write_fiscal_host(tmp_path, True)

        def run(*extra):
            summary = tmp_path / "summary.json"
            assert ne.main([
                "--submission", str(sub), "--settings", str(settings), "--pdf", str(pdf),
                "--fiscal-host", str(fiscal), "--output-summary", str(summary), *extra,
            ]) == 0
            return json.loads(summary.read_text())

        return sub, sent, run

    def test_first_run_sends_and_records(self, env):
        sub, sent, run = env
        summary = run()
        assert len(sent) == 1
        record = yaml.safe_load(sub.read_text())["approval_email"]
        assert record["to"] == "reviewer@example.org"
        assert record["sent_at"] == summary["sent_at"]
        assert record["testing_mode"] is True
        assert not summary.get("already_sent")

    def test_rerun_reports_the_earlier_send_without_sending(self, env):
        sub, sent, run = env
        first = run()
        before = sub.read_text()
        again = run()
        assert len(sent) == 1
        assert again["already_sent"] is True
        assert again["sent_at"] == first["sent_at"]
        assert again["to"] == first["to"]
        assert sub.read_text() == before

    def test_force_resend_sends_again_and_replaces_the_record(self, env, monkeypatch):
        sub, sent, run = env
        run()
        record = yaml.safe_load(sub.read_text())["approval_email"]
        record["sent_at"] = "2026-06-02 00:00:00 UTC"
        data = yaml.safe_load(sub.read_text())
        data["approval_email"] = record
        sub.write_text(yaml.safe_dump(data, sort_keys=False))
        summary = run("--force-resend")
        assert len(sent) == 2
        assert summary["resent"] is True
        assert yaml.safe_load(sub.read_text())["approval_email"]["sent_at"] == summary["sent_at"]
        assert summary["sent_at"] != "2026-06-02 00:00:00 UTC"

    def test_failed_send_records_nothing(self, env, monkeypatch):
        sub, sent, run = env

        def boom(msg, **kw):
            raise OSError("SMTP down")
        monkeypatch.setattr(ne, "send_message", boom)
        with pytest.raises(OSError):
            run()
        assert "approval_email" not in yaml.safe_load(sub.read_text())

    def test_dry_run_records_nothing(self, env):
        sub, sent, run = env
        run("--dry-run")
        assert "approval_email" not in yaml.safe_load(sub.read_text())
