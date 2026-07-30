"""Tests for the renderer in scripts/post_error_comment.py.

GitHub-API-touching functions (find/create/update/delete comment, add/remove
label) are integration territory — they're exercised against a real disposable
issue in `test-contractor-payments` during Phase 1 end-to-end testing.
"""
from __future__ import annotations

from scripts.post_error_comment import SENTINEL, render_error_comment


class TestRenderErrorComment:
    def test_includes_sentinel(self):
        out = render_error_comment([{"message": "x"}])
        assert SENTINEL in out
        # Sentinel should be near the end (last non-empty line).
        non_empty = [ln for ln in out.splitlines() if ln.strip()]
        assert non_empty[-1] == SENTINEL

    def test_line_specific_error_formatted(self):
        out = render_error_comment([
            {"line": 3, "message": "couldn't read a date from `2025/01/05`"},
        ])
        assert "**Line 3:**" in out
        assert "2025/01/05" in out

    def test_general_error_no_line_prefix(self):
        out = render_error_comment([
            {"message": "Period field is required.", "line": None},
        ])
        assert "Line " not in out
        assert "- Period field is required." in out

    def test_multiple_errors_each_appear(self):
        out = render_error_comment([
            {"line": 1, "message": "bad date"},
            {"line": 3, "message": "bad hours"},
            {"message": "missing confirmation"},
        ])
        assert "**Line 1:**" in out
        assert "**Line 3:**" in out
        assert "missing confirmation" in out

    def test_warnings_section_only_when_present(self):
        without = render_error_comment([{"message": "x"}])
        assert "Notes" not in without

        with_warn = render_error_comment(
            [{"message": "x"}],
            warnings=[{"message": "Used `,` as separator — please use `|` next time."}],
        )
        assert "Notes" in with_warn
        assert "`,`" in with_warn

    def test_includes_edit_instruction(self):
        """The contractor needs to know what to do — verify the call-to-action."""
        out = render_error_comment([{"message": "x"}])
        assert "edit this issue" in out.lower()

    def test_header_is_friendly(self):
        out = render_error_comment([{"message": "x"}])
        assert "Submission needs a fix" in out


class TestPostParseFailureFraming:
    """A failure that happens *after* a clean parse — a claim too long to
    render — used to borrow the parse-error wording, telling the contractor
    "I couldn't parse this submission" about a submission that parsed fine.
    """

    ERRORS = [{"message": "This claim is too long for the one-page document."}]

    def test_post_parse_failure_does_not_claim_a_parse_error(self):
        body = render_error_comment(self.ERRORS, parse_failed=False)
        assert "couldn't parse" not in body
        assert "The submission itself is fine" in body
        assert "couldn't be filed" in body
        assert SENTINEL in body

    def test_parse_failure_keeps_the_original_framing(self):
        body = render_error_comment(self.ERRORS)
        assert "I couldn't parse this submission" in body

    def test_neither_promises_an_automatic_recheck(self):
        """The caller fires on `issues: [labeled]` and
        `issue_comment: [created]` only — editing the issue re-triggers
        nothing, so promising a re-check leaves the contractor waiting."""
        for parse_failed in (True, False):
            body = render_error_comment(self.ERRORS, parse_failed=parse_failed)
            assert "re-check automatically" not in body
            assert "/validate" in body

    def test_post_parse_framing_points_at_a_real_retry_route(self):
        body = render_error_comment(self.ERRORS, parse_failed=False)
        assert "`submit` label" in body or "/validate" in body
