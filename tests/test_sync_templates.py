"""Tests for onboarding/sync_templates.py — the presence rule and the
substitution blocks behind conditional issue-form availability (PLAN §9).

The git/gh CLI surface is integration territory (Phase 5 E2E + retrofit);
these tests cover the pure plan/apply core against tmp_path fixtures.
"""
from __future__ import annotations

import yaml

from onboarding.sync_templates import (
    apply_plan,
    build_substitutions,
    plan_sync,
    render_issue_templates,
    sync_issue_templates,
)


HOURLY = {
    "contract_id": "QE-PSL-2026-001", "type": "hourly", "status": "active",
    "terms": {"hourly_rate": 45.0, "currency": "AUD", "max_hours_per_month": 40},
}
HOURLY_ENDED = {
    "contract_id": "QE-PSL-2025-009", "type": "hourly", "status": "ended",
    "terms": {"hourly_rate": 40.0, "currency": "AUD", "max_hours_per_month": 40},
}
MILESTONE = {
    "contract_id": "QE-IUJ-2026-002", "type": "milestone", "status": "active",
    "currency": "JPY", "milestones": [],
}
REIMBURSEMENTS = {
    "project": "CHOW",
    "allowed_categories": ["travel", "meals", "other"],
    "ledger_issue": None,
}


def _write_repo(tmp_path, contracts=(), reimbursements=None):
    (tmp_path / "contracts").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    for c in contracts:
        path = tmp_path / "contracts" / f"{c['contract_id']}.yml"
        path.write_text(yaml.safe_dump(c, sort_keys=False))
    if reimbursements is not None:
        (tmp_path / "config" / "reimbursements.yml").write_text(
            yaml.safe_dump(reimbursements, sort_keys=False)
        )
    return tmp_path


class TestPresenceRule:
    """A form is present iff its config exists; dead forms are deleted."""

    def _forms(self, contracts, reimbursements):
        desired = render_issue_templates(list(contracts), reimbursements)
        return {
            rel.rsplit("/", 1)[-1]
            for rel, content in desired.items() if content is not None
        }

    def test_hourly_only(self):
        assert self._forms([HOURLY], None) == {"hourly-timesheet.yml"}

    def test_milestone_only(self):
        assert self._forms([MILESTONE], None) == {"milestone-invoice.yml"}

    def test_reimbursement_only_payee(self):
        assert self._forms([], REIMBURSEMENTS) == {"reimbursement-claim.yml"}

    def test_all_three(self):
        assert self._forms([HOURLY, MILESTONE], REIMBURSEMENTS) == {
            "hourly-timesheet.yml",
            "milestone-invoice.yml",
            "reimbursement-claim.yml",
        }

    def test_ended_contract_does_not_surface_a_form(self):
        assert self._forms([HOURLY_ENDED], REIMBURSEMENTS) == {
            "reimbursement-claim.yml",
        }

    def test_empty_reimbursements_file_still_enables_form(self):
        # File presence is the switch, even if the config is sparse.
        assert "reimbursement-claim.yml" in self._forms([HOURLY], {})


class TestSubstitutions:
    def test_dropdowns_list_all_active_contracts_of_type(self):
        second = {**HOURLY, "contract_id": "QE-PSL-2026-005"}
        subs = build_substitutions([HOURLY, second, HOURLY_ENDED, MILESTONE], None)
        assert '- "QE-PSL-2026-001"' in subs["CONTRACT_OPTIONS"]
        assert '- "QE-PSL-2026-005"' in subs["CONTRACT_OPTIONS"]
        assert "QE-PSL-2025-009" not in subs["CONTRACT_OPTIONS"]  # ended
        assert '- "QE-IUJ-2026-002"' in subs["MILESTONE_CONTRACT_OPTIONS"]

    def test_reminders_carry_terms(self):
        subs = build_substitutions([HOURLY, MILESTONE], REIMBURSEMENTS)
        assert "45.0/hour" in subs["HOURLY_CONTRACT_REMINDER"]
        assert "JPY (milestone)" in subs["MILESTONE_CONTRACT_REMINDER"]
        assert "- `travel`" in subs["REIMBURSEMENT_CATEGORIES_REMINDER"]

    def test_rendered_forms_are_valid_yaml_without_placeholders(self):
        desired = render_issue_templates([HOURLY, MILESTONE], REIMBURSEMENTS)
        for rel, content in desired.items():
            assert content is not None, rel
            # Substitution placeholders sit at column 0 in the templates;
            # none may survive ($-names inside comments are $$-escaped).
            assert "\n$" not in content, rel
            yaml.safe_load(content)

    def test_multi_contract_substitution_survives_header_comment(self):
        # Regression: the templates' header comments name their placeholders;
        # an unescaped mention would splice the (multi-line) options block
        # into the comment and break the YAML.
        second = {**HOURLY, "contract_id": "QE-PSL-2026-005"}
        desired = render_issue_templates([HOURLY, second], None)
        content = desired[".github/ISSUE_TEMPLATE/hourly-timesheet.yml"]
        yaml.safe_load(content)


class TestPlanAndApply:
    def test_lifecycle_add_and_remove(self, tmp_path):
        repo = _write_repo(tmp_path, contracts=[HOURLY], reimbursements=REIMBURSEMENTS)
        changed = sync_issue_templates(repo)
        forms_dir = repo / ".github" / "ISSUE_TEMPLATE"
        assert sorted(p.name for p in forms_dir.glob("*.yml")) == [
            "hourly-timesheet.yml", "reimbursement-claim.yml",
        ]
        assert ".github/workflows/issue-to-pr.yml" in changed

        # Milestone contract added later → invoice form appears.
        (repo / "contracts" / "QE-IUJ-2026-002.yml").write_text(
            yaml.safe_dump(MILESTONE, sort_keys=False)
        )
        sync_issue_templates(repo)
        assert (forms_dir / "milestone-invoice.yml").exists()

        # Reimbursements disabled → claim form deleted.
        (repo / "config" / "reimbursements.yml").unlink()
        sync_issue_templates(repo)
        assert not (forms_dir / "reimbursement-claim.yml").exists()

    def test_idempotent(self, tmp_path):
        repo = _write_repo(tmp_path, contracts=[HOURLY], reimbursements=None)
        sync_issue_templates(repo)
        assert sync_issue_templates(repo) == []
        plan = plan_sync(repo)
        assert all(action == "unchanged" for _, action, _ in plan)

    def test_workflows_synced_verbatim(self, tmp_path):
        repo = _write_repo(tmp_path, contracts=[HOURLY])
        sync_issue_templates(repo)
        gate = (repo / ".github" / "workflows" / "issue-to-pr.yml").read_text()
        assert "contains(github.event.issue.labels.*.name, 'reimbursement')" in gate

    def test_apply_reports_changed_paths_only(self, tmp_path):
        repo = _write_repo(tmp_path, contracts=[HOURLY])
        first = apply_plan(repo, plan_sync(repo))
        assert first  # everything written on first run
        second = apply_plan(repo, plan_sync(repo))
        assert second == []
