"""Contract, ledger and approval checks (scripts/contract_checks.py) and the
two CLIs that run them: check_submission (at /validate, /submit) and
verify_approval (at merge)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts import check_submission, verify_approval
from scripts.contract_checks import (
    check_against_contract,
    is_safe_contract_id,
    verify_merge,
    verify_totals,
)
from scripts.create_submission_pr import enrich_submission

HOURLY = {
    "contract_id": "QE-PSL-2026-001",
    "type": "hourly",
    "status": "active",
    "start_date": "2026-01-01",
    "end_date": "2026-06-30",
    "terms": {"hourly_rate": 33.33, "currency": "AUD", "max_hours_per_month": 40},
}

MILESTONE = {
    "contract_id": "QE-IUJ-2026-002",
    "type": "milestone",
    "status": "active",
    "start_date": "2026-01-01",
    "end_date": "2026-03-31",
    "currency": "JPY",
    "milestones": [
        {"id": 1, "date": "2026-01-15", "amount": 77000, "description": "Jan"},
        {"id": 2, "date": "2026-02-15", "amount": 77000, "description": "Feb"},
    ],
}


def parsed_timesheet(dates=("2026-05-04", "2026-05-05"), hours=1.5):
    entries = [{"date": d, "hours": hours, "description": "work"} for d in dates]
    return {
        "type": "timesheet",
        "contract_id": "QE-PSL-2026-001",
        "period": "2026-05",
        "entries": entries,
        "totals": {"hours": hours * len(entries)},
        "notes": "",
        "status": "pending",
    }


def parsed_milestones(*rows):
    entries = [{"id": str(i), "date": d, "amount": a, "description": "m"} for i, d, a in rows]
    return {
        "type": "milestone_invoice",
        "contract_id": "QE-IUJ-2026-002",
        "period": "2026-02",
        "entries": entries,
        "totals": {"amount": sum(a for _, _, a in rows)},
        "notes": "",
        "status": "pending",
    }


def enriched(parsed, contract, sid="jdoe-timesheet-2026-05", supersedes=None):
    return enrich_submission(
        parsed, contract, submitter="jdoe", submission_id=sid,
        issue_number=7, submitted_date="2026-06-01", supersedes=supersedes,
    )


# ─── check_against_contract ─────────────────────────────────────────────────

class TestCheckAgainstContract:
    def test_clean_timesheet(self):
        assert check_against_contract(parsed_timesheet(), HOURLY) == ([], [])

    def test_wrong_form_for_contract(self):
        errors, _ = check_against_contract(parsed_timesheet(), MILESTONE)
        assert "Milestone Invoice form" in errors[0]

    def test_ended_contract_blocks_at_submit_only(self):
        ended = {**HOURLY, "status": "ended"}
        errors, _ = check_against_contract(parsed_timesheet(), ended)
        assert any("has ended" in e for e in errors)
        errors, _ = check_against_contract(parsed_timesheet(), ended, check_status=False)
        assert errors == []

    def test_hourly_dates_outside_contract_window(self):
        sub = parsed_timesheet(dates=("2026-07-01", "2026-07-02"))
        sub["period"] = "2026-07"
        errors, _ = check_against_contract(sub, HOURLY)
        assert "2 dates fall outside" in errors[0]
        assert "2026-07-01, 2026-07-02" in errors[0]

    def test_yaml_date_objects_are_understood(self):
        contract = yaml.safe_load(yaml.safe_dump(HOURLY).replace("'", ""))
        assert check_against_contract(parsed_timesheet(), contract) == ([], [])

    def test_jpy_amount_with_decimals_rejected(self):
        errors, _ = check_against_contract(
            parsed_milestones((1, "2026-01-15", 77000.5)), MILESTONE,
        )
        assert "more decimal places than JPY allows" in errors[0]

    def test_amount_differs_from_schedule_warns(self):
        errors, warnings = check_against_contract(
            parsed_milestones((1, "2026-01-15", 80000)), MILESTONE,
        )
        assert errors == []
        assert "80,000 JPY" in warnings[0] and "77,000 JPY" in warnings[0]

    def test_milestone_outside_window_only_warns(self):
        errors, warnings = check_against_contract(
            parsed_milestones((2, "2026-04-02", 77000)), MILESTONE,
        )
        assert errors == []
        assert "outside contract" in warnings[0]

    def test_already_paid_milestone_blocks(self):
        ledger = {"claims": [{
            "submission_id": "jdoe-invoice-2026-01", "approved_date": "2026-01-20",
            "entries": [{"id": "1", "amount": 77000}],
        }]}
        errors, _ = check_against_contract(
            parsed_milestones((1, "2026-01-15", 77000)), MILESTONE, ledger=ledger,
        )
        assert "already been paid on `jdoe-invoice-2026-01`" in errors[0]

    def test_revision_may_reclaim_the_superseded_milestone(self):
        ledger = {"claims": [{
            "submission_id": "jdoe-invoice-2026-01",
            "entries": [{"id": 1, "amount": 77000}],
        }]}
        errors, _ = check_against_contract(
            parsed_milestones((1, "2026-01-15", 77000)), MILESTONE, ledger=ledger,
            exclude_submission_ids=["jdoe-invoice-2026-01"],
        )
        assert errors == []

    def test_superseded_claims_dont_count(self):
        ledger = {"claims": [{
            "submission_id": "jdoe-invoice-2026-01", "status": "superseded",
            "entries": [{"id": 1, "amount": 77000}],
        }]}
        errors, _ = check_against_contract(
            parsed_milestones((1, "2026-01-15", 77000)), MILESTONE, ledger=ledger,
        )
        assert errors == []


@pytest.mark.parametrize("cid,ok", [
    ("QE-PSL-2026-001", True), ("qe.v2_draft", True),
    ("../config/settings", False), ("a/b", False), ("QE 1", False),
    ("QE-1\nx=y", False), ("", False), (None, False), ("-leading", False),
])
def test_safe_contract_id(cid, ok):
    assert is_safe_contract_id(cid) is ok


# ─── verify_totals ──────────────────────────────────────────────────────────

class TestVerifyTotals:
    def test_engine_generated_timesheet_verifies(self):
        assert verify_totals(enriched(parsed_timesheet(), HOURLY), HOURLY) == []

    def test_engine_generated_milestones_verify(self):
        sub = enriched(parsed_milestones((1, "2026-01-15", 77000), (2, "2026-02-15", 77000)),
                       MILESTONE, sid="jdoe-invoice-2026-02")
        assert verify_totals(sub, MILESTONE) == []

    def test_hand_edited_amount_is_refused(self):
        sub = enriched(parsed_timesheet(), HOURLY)
        sub["totals"]["amount"] = 9999.0
        errors = verify_totals(sub, HOURLY)
        assert any("3.00 hours x 33.33 AUD = 99.99 AUD" in e for e in errors), errors

    def test_rate_changed_after_submission_is_refused(self):
        sub = enriched(parsed_timesheet(), HOURLY)
        raised = {**HOURLY, "terms": {**HOURLY["terms"], "hourly_rate": 50}}
        errors = verify_totals(sub, raised)
        assert any("Rate is" in e for e in errors)

    def test_hours_total_must_match_entries(self):
        sub = enriched(parsed_timesheet(), HOURLY)
        sub["entries"].append({"date": "2026-05-06", "hours": 8.0, "description": "x"})
        errors = verify_totals(sub, HOURLY)
        assert any("entries add up to `11.00`" in e for e in errors)

    def test_duplicate_and_out_of_period_entries(self):
        sub = enriched(parsed_timesheet(), HOURLY)
        sub["entries"].append({"date": "2026-05-04", "hours": 1.5, "description": "dup"})
        sub["entries"].append({"date": "2026-04-30", "hours": 1.5, "description": "early"})
        errors = verify_totals(sub, HOURLY)
        assert any("appears more than once" in e for e in errors)
        assert any("outside the period" in e for e in errors)

    def test_currency_must_match_contract(self):
        sub = enriched(parsed_timesheet(), HOURLY)
        sub["totals"]["currency"] = "USD"
        assert any("Currency is `USD`" in e for e in verify_totals(sub, HOURLY))

    def test_milestone_total_must_match(self):
        sub = enriched(parsed_milestones((1, "2026-01-15", 77000)), MILESTONE,
                       sid="jdoe-invoice-2026-02")
        sub["totals"]["amount"] = 770000
        assert any("milestones add up to 77,000 JPY" in e for e in verify_totals(sub, MILESTONE))

    def test_malformed_yaml_is_an_error_not_a_crash(self):
        sub = enriched(parsed_timesheet(), HOURLY)
        del sub["totals"]["rate"]
        assert any("malformed" in e for e in verify_totals(sub, HOURLY))


# ─── verify_merge ───────────────────────────────────────────────────────────

class TestVerifyMerge:
    FILES = [
        "submissions/2026-05/jdoe-timesheet-2026-05.yml",
        "generated_pdfs/2026-05/jdoe-timesheet-2026-05.pdf",
        "generated_pdfs/2026-05/jdoe-timesheet-2026-05.png",
    ]

    def test_authorised_approver_case_insensitive(self):
        assert verify_merge(self.FILES, "MMcky", ["mmcky"]) == []

    def test_self_merge_by_contractor_refused(self):
        errors = verify_merge(self.FILES, "jdoe", ["mmcky"])
        assert "@jdoe merged this PR but is not an authorised approver (@mmcky)" in errors[0]

    def test_no_approvers_fails_closed(self):
        assert "No approvers are configured" in verify_merge(self.FILES, "mmcky", [])[0]

    def test_contract_edit_inside_submission_pr_refused(self):
        errors = verify_merge(self.FILES + ["contracts/QE-PSL-2026-001.yml", ""], "mmcky", ["mmcky"])
        assert "`contracts/QE-PSL-2026-001.yml`" in errors[0]


# ─── CLIs against a temporary contractor repo ───────────────────────────────

def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def repo(tmp_path):
    _write_yaml(tmp_path / "contracts" / "QE-PSL-2026-001.yml", HOURLY)
    _write_yaml(tmp_path / "contracts" / "QE-IUJ-2026-002.yml", MILESTONE)
    return tmp_path


def _run_check(repo, submission, issue=7):
    sub_file = repo / "submission.json"
    err_file = repo / "errors.json"
    sub_file.write_text(json.dumps(submission))
    err_file.write_text(json.dumps({"ok": True, "errors": [], "warnings": [{"message": "parser note"}]}))
    rc = check_submission.main([
        "--submission-file", str(sub_file), "--errors-file", str(err_file),
        "--issue", str(issue), "--repo-root", str(repo),
    ])
    return rc, sub_file.exists(), json.loads(err_file.read_text())


class TestCheckSubmissionCli:
    def test_clean_submission_is_kept(self, repo):
        rc, kept, report = _run_check(repo, parsed_timesheet())
        assert (rc, kept, report["ok"]) == (0, True, True)
        assert report["warnings"] == [{"message": "parser note"}]

    def test_missing_contract_becomes_a_fixable_error(self, repo):
        sub = {**parsed_timesheet(), "contract_id": "QE-PSL-2099-999"}
        rc, kept, report = _run_check(repo, sub)
        assert rc == 0 and not kept and not report["ok"]
        assert "isn't set up in this repository" in report["errors"][0]["message"]
        assert report["errors"][0]["line"] is None

    def test_path_like_contract_id_never_reaches_the_filesystem(self, repo):
        sub = {**parsed_timesheet(), "contract_id": "../contracts/QE-PSL-2026-001"}
        rc, kept, report = _run_check(repo, sub)
        assert not kept and "isn't a valid contract ID" in report["errors"][0]["message"]

    def test_reopened_issue_may_reclaim_its_own_milestone(self, repo):
        _write_yaml(repo / "ledger" / "QE-IUJ-2026-002.yml", {"claims": [{
            "submission_id": "jdoe-invoice-2026-01", "entries": [{"id": 1}],
        }]})
        # Without a prior approved submission for this issue: double claim.
        rc, kept, report = _run_check(repo, parsed_milestones((1, "2026-01-15", 77000)))
        assert not kept and "already been paid" in report["errors"][0]["message"]
        # The paid claim came from issue 7, so issue 7 is filing a revision.
        _write_yaml(repo / "submissions" / "2026-01" / "jdoe-invoice-2026-01.yml", {
            "submission_id": "jdoe-invoice-2026-01", "issue_number": 7,
            "status": "approved", "approved_date": "2026-01-20",
        })
        rc, kept, report = _run_check(repo, parsed_milestones((1, "2026-01-15", 77000)))
        assert kept and report["ok"]


class TestVerifyApprovalCli:
    def _setup(self, repo, *, merged_by="mmcky", extra_files=(), tamper=None):
        sub = enriched(parsed_timesheet(), HOURLY)
        if tamper:
            tamper(sub)
        sub_path = repo / "submissions" / "2026-05" / "jdoe-timesheet-2026-05.yml"
        _write_yaml(sub_path, sub)
        changed = repo / "changed.txt"
        changed.write_text("\n".join([
            "submissions/2026-05/jdoe-timesheet-2026-05.yml",
            "generated_pdfs/2026-05/jdoe-timesheet-2026-05.pdf",
            *extra_files,
        ]) + "\n")
        fiscal = repo / "fiscal-host.yml"
        _write_yaml(fiscal, {"quantecon": {"approvers": ["mmcky"]}})
        report = repo / "report.md"
        rc = verify_approval.main([
            "--submission", str(sub_path), "--changed-files", str(changed),
            "--merged-by", merged_by, "--fiscal-host", str(fiscal),
            "--repo-root", str(repo), "--report", str(report),
        ])
        return rc, report

    def test_good_merge_passes(self, repo):
        rc, report = self._setup(repo)
        assert rc == 0 and not report.exists()

    def test_bad_merge_writes_a_report(self, repo):
        def tamper(sub):
            sub["totals"]["amount"] = 5000.0
        rc, report = self._setup(
            repo, merged_by="jdoe", extra_files=[".github/workflows/x.yml"], tamper=tamper,
        )
        assert rc == 1
        text = report.read_text()
        assert "Approval refused" in text
        assert "@jdoe merged this PR" in text
        assert "`.github/workflows/x.yml`" in text
        assert "Amount is `5000.0` AUD" in text
        assert "revert this merge" in text

    def test_approvers_come_from_engine_file(self, tmp_path):
        fiscal = tmp_path / "fh.yml"
        _write_yaml(fiscal, {"quantecon": {"approvers": ["@mmcky", " other "]}})
        assert verify_approval.load_approvers(fiscal) == ["mmcky", "other"]
        assert verify_approval.load_approvers(tmp_path / "missing.yml") == []


def test_engine_fiscal_host_names_an_approver():
    path = Path(__file__).resolve().parents[1] / "templates" / "fiscal-host.yml"
    assert verify_approval.load_approvers(path), "quantecon.approvers must not be empty"
