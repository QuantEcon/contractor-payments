"""Tests for the renderer in scripts/post_error_comment.py.

GitHub-API-touching functions (find/create/update/delete comment, add/remove
label) are integration territory — they're exercised against a real disposable
issue in `contractor-engine-test` during Phase 1 end-to-end testing.
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
