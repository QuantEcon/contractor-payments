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


# ─── Message composition (Phase 5 adds the reimbursement branch) ────────────

class TestComposeMessage:
    def _submission(self, **overrides):
        sub = {
            "submission_id": "janedoe-reimbursement-2026-06",
            "type": "reimbursement",
            "project": "CHOW",
            "period": "2026-06",
            "approved_by": "mmcky",
            "approved_date": "2026-06-12",
            "totals": {"amount": 12300, "currency": "JPY"},
        }
        sub.update(overrides)
        return sub

    def _compose(self, tmp_path, submission=None, receipts=()):
        from scripts.notify_email import compose_message
        pdf = tmp_path / "claim.pdf"
        pdf.write_bytes(b"%PDF fake")
        receipt_paths = []
        for name in receipts:
            rp = tmp_path / name
            rp.write_bytes(b"\x89PNG fake" if name.endswith(".png") else b"%PDF fake")
            receipt_paths.append(rp)
        return compose_message(
            submission=submission or self._submission(),
            contractor={"name": "Jane Doe", "github": "janedoe"},
            pdf_path=pdf,
            issue_url="https://github.com/QuantEcon/x/issues/42",
            sender="payments@example.org",
            to="psl@example.org",
            cc=None,
            reply_to="payments@example.org",
            receipt_paths=receipt_paths,
        )

    def test_reimbursement_subject_uses_type_label(self, tmp_path):
        msg = self._compose(tmp_path)
        assert "Reimbursement Claim approved" in msg["Subject"]
        assert "12,300 JPY" in msg["Subject"]

    def test_project_line_replaces_contract_line(self, tmp_path):
        msg = self._compose(tmp_path)
        body = msg.get_body(preferencelist=("plain",)).get_content()
        assert "Project:       CHOW" in body
        assert "Contract:" not in body

    def test_receipts_attached_with_mime_types(self, tmp_path):
        msg = self._compose(
            tmp_path, receipts=("01-taxi.png", "02-hotel.pdf"),
        )
        attachments = list(msg.iter_attachments())
        names = [a.get_filename() for a in attachments]
        assert names == ["claim.pdf", "01-taxi.png", "02-hotel.pdf"]
        types = [a.get_content_type() for a in attachments]
        assert types == ["application/pdf", "image/png", "application/pdf"]

    def test_body_counts_receipts(self, tmp_path):
        msg = self._compose(tmp_path, receipts=("01-taxi.png",))
        body = msg.get_body(preferencelist=("plain",)).get_content()
        assert "1 receipt file(s)" in body

    def test_timesheet_body_unchanged(self, tmp_path):
        submission = {
            "submission_id": "janedoe-timesheet-2026-04",
            "type": "timesheet",
            "contract_id": "QE-PSL-2026-001",
            "period": "2026-04",
            "approved_by": "mmcky",
            "approved_date": "2026-05-13",
            "totals": {"amount": 725.0, "currency": "AUD"},
        }
        msg = self._compose(tmp_path, submission=submission)
        body = msg.get_body(preferencelist=("plain",)).get_content()
        assert "Contract:      QE-PSL-2026-001" in body
        assert "Project:" not in body
        assert "Attached: the approved invoice PDF." in body

    def test_revision_marker_in_subject(self, tmp_path):
        msg = self._compose(
            tmp_path,
            submission=self._submission(
                supersedes="janedoe-reimbursement-2026-06",
            ),
        )
        assert "REVISION approved" in msg["Subject"]
