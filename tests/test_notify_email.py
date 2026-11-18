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


@pytest.fixture
def env(tmp_path, monkeypatch):
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


class TestSentOnce:
    """`Re-run failed jobs` replays the email step when something after it
    failed. The send is recorded in the submission YAML so the re-run reports
    the earlier send instead of emailing the fiscal host a second time."""


    def test_first_run_sends_and_records(self, env):
        sub, sent, run = env
        summary = run()
        assert len(sent) == 1
        record = yaml.safe_load(sub.read_text())["approval_email"]
        assert record["to"] == "reviewer" and record["cc"] is None
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

    def test_dry_run_after_a_send_still_previews_without_touching_the_record(self, env, capsys):
        sub, sent, run = env
        run()
        before = sub.read_text()
        capsys.readouterr()
        summary = run("--dry-run")
        out = capsys.readouterr().out
        assert "--- email (dry-run, not sent) ---" in out
        assert summary["dry_run"] is True
        assert not summary.get("already_sent") and not summary["resent"]
        assert len(sent) == 1
        assert sub.read_text() == before



# ─── No addresses where the contractor can read them (issue #20) ────────────

class TestNoAddressesInContractorVisibleOutput:
    """The log, the summary (which feeds the audit comment) and the committed
    record are all readable by the contractor. They name recipients by role;
    only the message headers carry addresses."""

    ADDRESSES = ("reviewer@example.org", "psl@example.org")

    @pytest.mark.parametrize("testing_mode", [True, False])
    def test_send_names_roles_not_addresses(self, env, capsys, tmp_path, testing_mode):
        sub, sent, run = env
        _write_fiscal_host(tmp_path, testing_mode)
        summary = run()
        out = capsys.readouterr()
        visible = out.out + out.err + json.dumps(summary) + sub.read_text()
        for address in self.ADDRESSES:
            assert address not in visible
        assert summary["to"] == ("reviewer" if testing_mode else "psl")
        assert summary["cc"] == (None if testing_mode else "reviewer")
        # The message itself still goes to the real addresses.
        headers = (sent[0]["To"], sent[0].get("Cc"))
        assert headers == (("reviewer@example.org", None) if testing_mode
                           else ("psl@example.org", "reviewer@example.org"))

    def test_rerun_over_a_record_holding_addresses_does_not_echo_them(self, env, capsys):
        """Records written by #16's first version stored addresses."""
        sub, sent, run = env
        data = yaml.safe_load(sub.read_text())
        data["approval_email"] = {"to": "reviewer@example.org", "cc": None,
                                  "sent_at": "2026-11-18 05:28:21 UTC", "testing_mode": True}
        sub.write_text(yaml.safe_dump(data, sort_keys=False))
        run()
        out = capsys.readouterr()
        assert "reviewer@example.org" not in out.err
        assert "to the QuantEcon reviewer" in out.err

    def test_refused_recipient_error_omits_addresses(self, monkeypatch):
        import smtplib

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, msg):
                raise smtplib.SMTPRecipientsRefused({"psl@example.org": (550, b"no such user")})

        monkeypatch.setattr(ne.smtplib, "SMTP", FakeSMTP)
        with pytest.raises(RuntimeError) as exc:
            ne.send_message(object(), host="h", port=1, user="u", password="p")
        assert "psl@example.org" not in str(exc.value)
        assert "550" in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.parametrize("record,expected", [
    ({"testing_mode": True}, "the QuantEcon reviewer"),
    ({"testing_mode": False}, "PSL (Cc the QuantEcon reviewer)"),
    ({}, "the QuantEcon reviewer"),                           # fail safe
    ({"testing_mode": True, "to": "someone@example.org"}, "the QuantEcon reviewer"),
])
def test_describe_recipients(record, expected):
    assert ne.describe_recipients(record) == expected
