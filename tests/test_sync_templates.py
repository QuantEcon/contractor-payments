"""Tests for onboarding/sync_templates.py — the presence rule and the
substitution blocks behind conditional issue-form availability (PLAN §9).

The git/gh CLI surface is integration territory (Phase 5 E2E + retrofit);
these tests cover the pure plan/apply core against tmp_path fixtures.
"""
from __future__ import annotations

import subprocess

import pytest
import yaml

from onboarding import sync_templates
from onboarding.sync_templates import (
    ENGINE_ROOT,
    REIMBURSEMENT_LEDGER_TITLE,
    ReimbursementConfigError,
    TEMPLATE_DIR,
    apply_plan,
    build_substitutions,
    init_reimbursement_ledger,
    main,
    plan_sync,
    render_issue_templates,
    sync_issue_templates,
    validate_reimbursements_config,
    write_ledger_issue,
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


class TestCommitStagesExactPaths:
    """`sync_templates` auto-pushes its commit, so what it stages matters. It
    used to stage the *top-level directory* of each changed path
    (`git add --all .github config`), which swept up anything the admin had in
    flight in the contractor repo — including `.github/CODEOWNERS`, a file this
    module deliberately does not sync — into that pushed commit.
    """

    @staticmethod
    def _git(repo, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        ).stdout

    def _repo_with_history(self, tmp_path):
        repo = _write_repo(tmp_path, contracts=[HOURLY])
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "admin@example.org")
        self._git(repo, "config", "user.name", "Admin")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "seed")
        return repo

    def test_unrelated_in_flight_edits_are_not_swept_in(self, tmp_path):
        repo = self._repo_with_history(tmp_path)
        (repo / ".github").mkdir(exist_ok=True)
        codeowners = repo / ".github" / "CODEOWNERS"
        codeowners.write_text("* @half-finished-edit\n", encoding="utf-8")
        scratch = repo / ".github" / "scratch-notes.md"
        scratch.write_text("local only\n", encoding="utf-8")

        changed = sync_issue_templates(repo)
        assert changed
        subprocess.run(
            ["git", "add", "--all", "--", *sorted(changed)],
            cwd=repo, check=True,
        )

        staged = set(self._git(repo, "diff", "--cached", "--name-only").split())
        assert staged == set(changed)
        assert ".github/CODEOWNERS" not in staged
        assert ".github/scratch-notes.md" not in staged

    def test_pathspec_form_still_records_deletions(self, tmp_path):
        """The old form staged deletions via the directory; the exact-path
        form has to keep doing so or a removed form would linger."""
        repo = self._repo_with_history(tmp_path)
        sync_issue_templates(repo)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        self._git(repo, "commit", "-qm", "sync")

        # Reimbursements were never enabled here; drop the hourly contract so
        # the hourly form is deleted on the next sync.
        (repo / "contracts" / "QE-PSL-2026-001.yml").unlink()
        changed = sync_issue_templates(repo)
        assert ".github/ISSUE_TEMPLATE/hourly-timesheet.yml" in changed

        subprocess.run(
            ["git", "add", "--all", "--", *sorted(changed)],
            cwd=repo, check=True,
        )
        staged = self._git(repo, "diff", "--cached", "--name-status")
        assert "D\t.github/ISSUE_TEMPLATE/hourly-timesheet.yml" in staged


# A seeded config as it looks in a contractor repo after onboarding's
# substitutions — comments and all, because keeping them is part of the
# contract of `write_ledger_issue`.
SEEDED_CONFIG = """\
# Reimbursement arrangement for this contractor (engine Phase 5).

# PSL funding/billing code — rendered as "Project" on claim PDFs.
project: CHOW

# Line-item categories the parser accepts (case-insensitive).
allowed_categories: [travel, meals]

# Pinned "Running ledger — Reimbursements" issue number.
ledger_issue: null
"""


def _write_config(tmp_path, text):
    (tmp_path / "config").mkdir(exist_ok=True)
    path = tmp_path / "config" / "reimbursements.yml"
    path.write_text(text, encoding="utf-8")
    return path


class TestReimbursementConfigValidation:
    """`allowed_categories` semantics used to be decided by a silent
    truthiness test in `parse_issue` (`if allowed_categories:`), which made an
    empty list mean "no restriction" while the shipped config comment and the
    contractor guide both claimed the opposite. The shape is now validated at
    sync/onboarding time and the ambiguous cases are refused outright.
    """

    def _validate(self, tmp_path, text):
        path = _write_config(tmp_path, text)
        validate_reimbursements_config(
            yaml.safe_load(path.read_text()) or {}, path,
        )

    def test_forgotten_brackets_are_rejected(self, tmp_path):
        # `travel, meals` is a *string*; parse_issue's membership test would
        # then compare each category against that one string and reject every
        # real one.
        with pytest.raises(ReimbursementConfigError) as exc:
            self._validate(tmp_path, "project: CHOW\nallowed_categories: travel, meals\n")
        message = str(exc.value)
        assert "reimbursements.yml" in message          # names the file
        assert "must be a YAML list" in message
        assert "allowed_categories: [travel, meals]" in message  # shows the fix

    def test_empty_list_is_rejected_as_ambiguous(self, tmp_path):
        with pytest.raises(ReimbursementConfigError) as exc:
            self._validate(tmp_path, "project: CHOW\nallowed_categories: []\n")
        assert "delete the `allowed_categories` key" in str(exc.value)

    def test_omitted_key_means_no_restriction(self, tmp_path):
        # The sanctioned way to accept any category: no key at all.
        self._validate(tmp_path, "project: CHOW\nledger_issue: 4\n")

    def test_null_categories_is_rejected(self, tmp_path):
        with pytest.raises(ReimbursementConfigError):
            self._validate(tmp_path, "project: CHOW\nallowed_categories:\n")

    def test_blank_category_entry_is_rejected(self, tmp_path):
        # A dangling `-` yields a null entry, which crashes parse_issue's
        # `c.strip()` on None rather than saying anything useful.
        with pytest.raises(ReimbursementConfigError) as exc:
            self._validate(tmp_path, "allowed_categories:\n  - travel\n  -\n")
        assert "non-empty category name" in str(exc.value)

    @pytest.mark.parametrize("line", [
        "project: $REIMBURSEMENT_PROJECT",
        "allowed_categories: $REIMBURSEMENT_CATEGORIES",
        "ledger_issue: $REIMBURSEMENT_LEDGER_ISSUE",
    ])
    def test_unsubstituted_placeholders_are_rejected(self, tmp_path, line):
        with pytest.raises(ReimbursementConfigError) as exc:
            self._validate(tmp_path, line + "\n")
        assert "template placeholder" in str(exc.value)

    @pytest.mark.parametrize("value", ["'12'", "true", "[]"])
    def test_ledger_issue_must_be_a_number_or_null(self, tmp_path, value):
        with pytest.raises(ReimbursementConfigError):
            self._validate(tmp_path, f"project: CHOW\nledger_issue: {value}\n")

    def test_ledger_issue_number_and_null_are_fine(self, tmp_path):
        self._validate(tmp_path, "ledger_issue: 12\n")
        self._validate(tmp_path, "ledger_issue: null\n")

    def test_sync_refuses_to_run_on_a_broken_config(self, tmp_path):
        repo = _write_repo(tmp_path, contracts=[HOURLY])
        _write_config(repo, "project: CHOW\nallowed_categories: travel, meals\n")
        with pytest.raises(ReimbursementConfigError):
            plan_sync(repo)

    def test_cli_reports_the_error_without_a_traceback(self, tmp_path, capsys):
        repo = _write_repo(tmp_path, contracts=[HOURLY])
        (repo / ".git").mkdir()
        _write_config(repo, "project: CHOW\nallowed_categories: travel, meals\n")
        assert main(["--repo-dir", str(repo)]) == 1
        assert "must be a YAML list" in capsys.readouterr().err

    def test_no_restriction_reminder_does_not_send_the_contractor_to_the_admin(self):
        # The old text ("no categories configured — ask the admin") told the
        # contractor to chase a restriction that isn't being enforced.
        reminder = build_substitutions([], {"project": "CHOW"})[
            "REIMBURSEMENT_CATEGORIES_REMINDER"
        ]
        assert "ask the admin" not in reminder
        assert "doesn't restrict categories" in reminder

    def test_shipped_config_template_matches_the_code(self):
        template = (TEMPLATE_DIR / "config" / "reimbursements.yml").read_text()
        # No reject-all reading, and the escape hatch is documented.
        assert "delete this key" in template
        assert "empty list is a" in template
        assert "configuration error" in template


class FakeGh:
    """Stand-in for the `gh` CLI: records commands, fails the ones asked for."""

    def __init__(self, *, existing=(), create_number=42, fail_verbs=()):
        self.existing = list(existing)          # [(number, title)]
        self.create_number = create_number
        self.fail_verbs = set(fail_verbs)       # e.g. {"pin"}
        self.commands: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        verb = cmd[2] if cmd[:2] == ["gh", "issue"] else None
        if verb == "list":
            import json
            payload = [{"number": n, "title": t} for n, t in self.existing]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if verb == "create":
            return subprocess.CompletedProcess(
                cmd, 0,
                f"https://github.com/QuantEcon/x/issues/{self.create_number}\n", "",
            )
        if verb in self.fail_verbs:
            return self._fail(cmd, kwargs, "cannot pin: limit reached")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @staticmethod
    def _fail(cmd, kwargs, stderr):
        """Honour `check=True` the way `subprocess.run` does.

        Without this the fake returns a non-zero CompletedProcess and callers
        using `check=True` sail straight past it — which silently turned the
        "config is still wired when pinning fails" test into a no-op that
        passed against the pre-fix code too.
        """
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(1, cmd, output="", stderr=stderr)
        return subprocess.CompletedProcess(cmd, 1, "", stderr)

    def verbs(self):
        return [c[2] for c in self.commands if c[:2] == ["gh", "issue"]]


@pytest.fixture
def fake_gh(monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(sync_templates.subprocess, "run", gh)
    monkeypatch.setattr(sync_templates, "create_label", lambda *a, **k: "ok")
    return gh


class TestInitReimbursementLedger:
    """Creating the pinned ledger issue has to be safe to re-run.

    It used to pin + lock with `check=True` *before* writing `ledger_issue`
    back to the config, so a failure at the pin step (GitHub caps a repo at 3
    pinned issues, and each contract ledger already takes one) left an orphan
    issue that every retry duplicated.
    """

    def test_config_is_wired_even_when_pinning_fails(self, tmp_path, fake_gh):
        path = _write_config(tmp_path, SEEDED_CONFIG)
        fake_gh.fail_verbs = {"pin", "lock"}

        assert init_reimbursement_ledger(tmp_path, "QuantEcon/x", dry_run=False) == 42
        assert yaml.safe_load(path.read_text())["ledger_issue"] == 42

    def test_rerun_reuses_the_orphan_instead_of_creating_another(
        self, tmp_path, fake_gh,
    ):
        path = _write_config(tmp_path, SEEDED_CONFIG)
        fake_gh.existing = [(7, REIMBURSEMENT_LEDGER_TITLE), (9, "📒 Running ledger — QE-1")]

        assert init_reimbursement_ledger(tmp_path, "QuantEcon/x", dry_run=False) == 7
        assert yaml.safe_load(path.read_text())["ledger_issue"] == 7
        assert "create" not in fake_gh.verbs()

    def test_already_wired_is_a_no_op(self, tmp_path, fake_gh):
        _write_config(tmp_path, SEEDED_CONFIG.replace("ledger_issue: null",
                                                      "ledger_issue: 12"))
        assert init_reimbursement_ledger(tmp_path, "QuantEcon/x", dry_run=False) is None
        assert fake_gh.commands == []

    def test_unsubstituted_ledger_placeholder_is_not_mistaken_for_wired(
        self, tmp_path, fake_gh,
    ):
        # The literal is truthy: this used to print "already wired
        # (#$REIMBURSEMENT_LEDGER_ISSUE)" and silently skip the ledger.
        _write_config(tmp_path, SEEDED_CONFIG.replace(
            "ledger_issue: null", "ledger_issue: $REIMBURSEMENT_LEDGER_ISSUE"))
        with pytest.raises(ReimbursementConfigError):
            init_reimbursement_ledger(tmp_path, "QuantEcon/x", dry_run=False)

    def test_wiring_preserves_the_seeded_comments(self, tmp_path, fake_gh):
        path = _write_config(tmp_path, SEEDED_CONFIG)
        init_reimbursement_ledger(tmp_path, "QuantEcon/x", dry_run=False)

        text = path.read_text()
        assert "ledger_issue: 42" in text
        # safe_dump round-tripping used to strip every one of these.
        assert "# PSL funding/billing code" in text
        assert "# Line-item categories the parser accepts" in text
        assert "allowed_categories: [travel, meals]" in text

    def test_write_ledger_issue_appends_when_the_key_is_absent(self, tmp_path):
        path = _write_config(tmp_path, "project: CHOW\n")
        write_ledger_issue(path, 5)
        assert yaml.safe_load(path.read_text()) == {"project": "CHOW",
                                                    "ledger_issue": 5}

    def test_missing_config_is_a_warning_not_a_crash(self, tmp_path, capsys, fake_gh):
        assert init_reimbursement_ledger(tmp_path, "QuantEcon/x", dry_run=False) is None
        assert "not found" in capsys.readouterr().err


class TestHeaderRowGuidanceIsAccurate:
    """The forms and the guide used to say the parser recognises the entries
    table *from the header row*. It doesn't — the `### <Type> Entries` section
    heading does; a header row is merely tolerated and skipped. A contractor
    who deleted it lost nothing, but the text implied catastrophe.
    """

    DOCS = ("submit-timesheet.md", "submit-invoice.md", "submit-reimbursement.md")

    def _sources(self):
        for relpath in sync_templates.FORM_FILES.values():
            yield relpath, (TEMPLATE_DIR / relpath).read_text(encoding="utf-8")
        for name in self.DOCS:
            path = ENGINE_ROOT / "docs" / "contractor-guide" / name
            yield name, path.read_text(encoding="utf-8")

    def test_no_source_claims_the_header_row_identifies_the_table(self):
        for name, text in self._sources():
            flat = " ".join(text.split())
            assert "uses it to recognise" not in flat, name
            assert "header row in place" not in flat, name

    def test_every_source_calls_the_header_row_optional(self):
        for name, text in self._sources():
            flat = " ".join(text.split())
            assert "header row is optional" in flat, name
