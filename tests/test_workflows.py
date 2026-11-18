"""Static checks on the GitHub Actions workflows.

The engine's control flow lives in YAML and shell as much as it does in Python:
which step runs after a failure, whether the fiscal host gets emailed, whether
the contractor hears anything back. None of that is reachable from a unit test
of a Python function, and a typo in a `steps.<id>.outcome` reference fails
*open* — the expression just evaluates empty and the step silently does the
wrong thing. These tests cover the parts that can be checked without a runner.

What is deliberately NOT claimed here: that `always()` / `failure()` /
`success()` evaluate as intended at runtime. That needs a real Actions run —
see PLAN.md's failure-drill note.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
CALLER_WORKFLOWS = sorted(
    (REPO_ROOT / "contractor-template" / ".github" / "workflows").glob("*.yml")
)
ALL_WORKFLOWS = ENGINE_WORKFLOWS + CALLER_WORKFLOWS

# `${{ ... }}` is not shell. Substitute a placeholder before syntax-checking so
# an expression spanning a quote boundary doesn't read as unbalanced quoting.
_EXPR = re.compile(r"\$\{\{[^}]*\}\}")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(workflow: dict) -> list[dict]:
    return [
        step
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    ]


def _id(step: dict) -> str:
    return step.get("id") or step.get("name") or "<unnamed>"


def test_workflows_are_discovered():
    """A glob that silently matches nothing would make every test below pass."""
    assert ENGINE_WORKFLOWS, "no engine workflows found"
    assert CALLER_WORKFLOWS, "no contractor-template workflows found"


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path):
    assert isinstance(_load(path), dict)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_run_blocks_are_syntactically_valid_shell(path):
    """A shell syntax error in a `run:` block is only discovered when the step
    executes — i.e. mid-submission, on a contractor's issue."""
    failures = []
    for step in _steps(_load(path)):
        script = step.get("run")
        if not script:
            continue
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8",
        ) as f:
            f.write(_EXPR.sub("EXPR", script))
            probe = Path(f.name)
        try:
            result = subprocess.run(
                ["bash", "-n", str(probe)], capture_output=True, text=True,
            )
            if result.returncode != 0:
                failures.append(f"{_id(step)}: {result.stderr.strip()}")
        finally:
            probe.unlink(missing_ok=True)
    assert not failures, "shell syntax errors:\n" + "\n".join(failures)


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_step_references_resolve_to_real_step_ids(path):
    """`steps.<id>.outcome` for an id that doesn't exist evaluates to empty
    rather than erroring, so a typo in a gating condition fails *open* — the
    guard quietly stops guarding. Nothing at runtime will tell you."""
    workflow = _load(path)
    for job in (workflow.get("jobs") or {}).values():
        steps = job.get("steps") or []
        known = {s["id"] for s in steps if s.get("id")}
        referenced = set()
        for ref in re.finditer(
            r"steps\.([A-Za-z0-9_-]+)\.(?:outcome|conclusion|outputs)",
            yaml.safe_dump(job),
        ):
            referenced.add(ref.group(1))
        unknown = referenced - known
        assert not unknown, (
            f"{path.name}: references unknown step id(s) {sorted(unknown)}; "
            f"known ids are {sorted(known)}"
        )


class TestApprovalEmailCannotFireOnAFailedRun:
    """The money-path invariant, encoded so it cannot be lost to a refactor.

    `process-approved.yml` emails the real fiscal host. Every step there has an
    implicit `success()`, which is what guarantees a mid-pipeline failure can
    never produce an approval email. Adding failure gating to that workflow —
    as the review pass did, for the audit comment and the failure report — is
    exactly the kind of change that could weaken it by accident.
    """

    APPROVED = REPO_ROOT / ".github" / "workflows" / "process-approved.yml"

    def _email_steps(self):
        steps = [
            s for s in _steps(_load(self.APPROVED))
            if "notify_email" in (s.get("run") or "")
        ]
        assert steps, "no step invoking scripts.notify_email found"
        return steps

    def test_email_step_has_no_failure_bypassing_condition(self):
        for step in self._email_steps():
            condition = step.get("if")
            if condition is None:
                continue        # implicit success() — the safe default
            for bypass in ("always()", "failure()", "cancelled()"):
                assert bypass not in condition, (
                    f"`{_id(step)}` sends the approval email but its `if:` "
                    f"contains `{bypass}`, which can evaluate true after an "
                    f"upstream failure. The fiscal host must never be emailed "
                    f"for a run that did not complete."
                )

    def test_processed_label_is_success_gated(self):
        """A failed run must not end up looking processed."""
        labels = [
            s for s in _steps(_load(self.APPROVED))
            if "processed" in (s.get("run") or "")
            and "gh pr edit" in (s.get("run") or "")
        ]
        assert labels, "no step applying the `processed` label found"
        for step in labels:
            condition = step.get("if") or ""
            assert "always()" not in condition and "failure()" not in condition


class TestTypstPinIsConsistent:
    """CI derives the Typst version from these files, so a bump in one place
    that misses the other would leave the two halves of the pipeline rendering
    on different versions — and the page-overflow threshold moves with the
    version."""

    def _pins(self):
        found = {}
        for path in ENGINE_WORKFLOWS:
            versions = re.findall(
                r'TYPST_VERSION="([0-9.]+)"', path.read_text(encoding="utf-8")
            )
            if versions:
                found[path.name] = set(versions)
        return found

    def test_every_workflow_pins_the_same_version(self):
        pins = self._pins()
        assert pins, "no TYPST_VERSION pin found in any workflow"
        distinct = set().union(*pins.values())
        assert len(distinct) == 1, f"conflicting Typst pins: {pins}"

    def test_pin_is_an_exact_version(self):
        for name, versions in self._pins().items():
            for version in versions:
                assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
                    f"{name} pins Typst as `{version}`; use an exact "
                    f"MAJOR.MINOR.PATCH so renders are reproducible."
                )

    def test_every_workflow_pins_the_same_checksum(self):
        """The tarball is checked against a sha256 next to the version; a
        bump that updates one workflow's hash but not another's would fail
        that workflow's install (or, worse, leave one unverified)."""
        hashes = {}
        for path in ENGINE_WORKFLOWS:
            text = path.read_text(encoding="utf-8")
            if re.search(r'TYPST_VERSION="[0-9.]+"', text):
                hashes[path.name] = set(re.findall(r'TYPST_SHA256="([0-9a-f]{64})"', text))
                assert hashes[path.name], f"{path.name} pins a version but no TYPST_SHA256"
        assert len(set().union(*hashes.values())) == 1, f"conflicting Typst hashes: {hashes}"

    @pytest.mark.parametrize("path", ENGINE_WORKFLOWS + [REPO_ROOT / ".github/workflows/tests.yml"],
                             ids=lambda p: p.name)
    def test_tarball_is_verified_before_it_is_extracted(self, path):
        for step in _steps(_load(path)):
            run = step.get("run") or ""
            if "typst-x86_64-unknown-linux-musl.tar.xz" not in run:
                continue
            assert "| tar" not in run, f"{_id(step)} pipes the download straight into tar"
            assert run.index("sha256sum -c") < run.index("tar -xJ"), _id(step)


class TestEngineCheckoutIsPinnedToTheWorkflow:
    """Every reusable workflow must run the engine scripts from a pinned ref.

    `actions/checkout` with no `ref:` takes the repository's default branch, so
    a caller pinned to `@some-branch` got that branch's workflow YAML driving
    `main`'s scripts. The mismatch is invisible until the YAML passes a flag
    the scripts don't have — at which point the run dies on an `unrecognized
    arguments` error, having looked fine on every previous test.

    The ref arrives as the `engine_ref` input, because no context value
    supplies it: `github.job_workflow_ref` and `github.job_workflow_sha` both
    read as empty (verified on real runs), and `github.workflow_ref` describes
    the *calling* workflow in the contractor repo rather than this one.
    """

    PIN = "${{ inputs.engine_ref }}"

    @staticmethod
    def _is_reusable(workflow):
        # PyYAML parses a bare `on:` key as the boolean True.
        triggers = workflow.get("on") or workflow.get(True) or {}
        return "workflow_call" in triggers

    def _engine_checkouts(self, workflow):
        return [
            step for step in _steps(workflow)
            if (step.get("uses") or "").startswith("actions/checkout")
            and (step.get("with") or {}).get("repository")
               == "QuantEcon/contractor-payments"
        ]

    @pytest.mark.parametrize("path", ENGINE_WORKFLOWS, ids=lambda p: p.name)
    def test_engine_checkout_uses_the_pinned_ref(self, path):
        workflow = _load(path)
        if not self._is_reusable(workflow):
            pytest.skip("not a reusable workflow")
        checkouts = self._engine_checkouts(workflow)
        assert checkouts, f"{path.name}: no engine checkout found"
        for step in checkouts:
            ref = (step.get("with") or {}).get("ref")
            assert ref == self.PIN, (
                f"{path.name}: the engine checkout uses ref={ref!r}. It must be "
                f"{self.PIN!r}, or the scripts can come from a different commit "
                f"than this workflow."
            )

    @pytest.mark.parametrize("path", ENGINE_WORKFLOWS, ids=lambda p: p.name)
    def test_reusable_workflows_declare_engine_ref(self, path):
        workflow = _load(path)
        if not self._is_reusable(workflow):
            pytest.skip("not a reusable workflow")
        if not self._engine_checkouts(workflow):
            pytest.skip("no engine checkout")
        triggers = workflow.get("on") or workflow.get(True) or {}
        inputs = (triggers["workflow_call"] or {}).get("inputs") or {}
        assert "engine_ref" in inputs, f"{path.name}: no engine_ref input"
        spec = inputs["engine_ref"]
        assert spec.get("required") is False, (
            f"{path.name}: engine_ref must stay optional so existing callers "
            f"that omit it keep working."
        )
        assert spec.get("default") == "main", (
            f"{path.name}: engine_ref should default to 'main' — the behaviour "
            f"a caller pinned at @main already had."
        )


class TestCallerTemplatesPinBothHalves:
    """A caller pins the engine twice — the `@ref` on `uses:` and the
    `engine_ref` input — and the two must agree. This is the drift that made
    the original bug undetectable, so it gets a test rather than a comment."""

    @pytest.mark.parametrize("path", CALLER_WORKFLOWS, ids=lambda p: p.name)
    def test_uses_ref_and_engine_ref_agree(self, path):
        workflow = _load(path)
        for name, job in (workflow.get("jobs") or {}).items():
            uses = job.get("uses")
            if not uses or "contractor-payments" not in uses:
                continue
            uses_ref = uses.rsplit("@", 1)[-1]
            engine_ref = (job.get("with") or {}).get("engine_ref")
            assert engine_ref is not None, (
                f"{path.name}: job `{name}` calls the engine at @{uses_ref} but "
                f"passes no engine_ref, so the scripts would come from the "
                f"engine's default branch instead."
            )
            assert engine_ref == uses_ref, (
                f"{path.name}: job `{name}` calls the engine at @{uses_ref} but "
                f"passes engine_ref={engine_ref!r}. The workflow and the scripts "
                f"it drives would come from different commits."
            )


class TestApprovalIsVerifiedBeforeAnythingOutwardFacing:
    """`verify` (scripts/verify_approval.py) is the gate that stops a
    contractor's self-merge, a PR that also edits a contract, or a hand-edited
    total from being stamped, ledgered and emailed to the fiscal host. It must
    run before all of those, with no condition that lets them run when it
    fails."""

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "process-approved.yml"
    GATED = ("stamp", "update_ledger", "commit_back", "email")

    def _ids(self):
        return [_id(s) for s in _steps(_load(self.WORKFLOW))]

    def test_verify_runs_before_every_outward_facing_step(self):
        ids = self._ids()
        assert "verify" in ids
        for step_id in self.GATED:
            assert ids.index("verify") < ids.index(step_id), step_id

    def test_gated_steps_cannot_run_after_a_failed_verify(self):
        for step in _steps(_load(self.WORKFLOW)):
            if _id(step) in self.GATED:
                condition = str(step.get("if", ""))
                assert "always()" not in condition and "failure()" not in condition, _id(step)

    def test_verify_reads_approvers_from_the_engine_checkout(self):
        verify = next(s for s in _steps(_load(self.WORKFLOW)) if _id(s) == "verify")
        assert "scripts.verify_approval" in verify["run"]
        assert "--fiscal-host engine/templates/fiscal-host.yml" in verify["run"]


def test_submission_parse_step_runs_contract_checks():
    """Contract problems must reach the contractor as a fixable error, which
    only happens if check_submission runs inside the parse step (it decides
    parse_ok by whether the submission JSON survives)."""
    wf = _load(REPO_ROOT / ".github" / "workflows" / "process-submission.yml")
    parse = next(s for s in _steps(wf) if _id(s) == "parse")
    run = parse["run"]
    assert run.index("scripts.parse_issue") < run.index("scripts.check_submission")
    assert run.index("scripts.check_submission") < run.index('echo "parse_ok=true"')


def test_submitted_issues_are_not_locked():
    """Contractors are Triage collaborators. Only Write+ can unlock an issue,
    so locking it on /submit would block their reopen → /submit revision
    path. See ADMIN_RUNBOOK.md → "Access model and branch protection"."""
    text = (REPO_ROOT / ".github" / "workflows" / "process-submission.yml").read_text()
    assert "gh issue lock" not in text


def test_onboarding_never_grants_contractors_write():
    """Write lets a contractor merge (self-approve), edit contracts, and read
    org secrets from a workflow on any branch."""
    text = (REPO_ROOT / "onboarding" / "new-contractor.py").read_text()
    assert "permission=push" not in text
    assert "permission=triage" in text


def test_merge_commit_is_fetched_before_it_is_diffed():
    """The approval job checks out main at depth 2. If another approval's
    commit lands first, the merge commit's parent is outside that history
    and both the find and verify diffs fail. The find step must fetch the
    merge commit explicitly before its first diff."""
    wf = _load(REPO_ROOT / ".github" / "workflows" / "process-approved.yml")
    run = next(s for s in _steps(wf) if _id(s) == "find_submission")["run"]
    fetch = run.index('git fetch --no-tags --depth=2 origin "$MERGE_SHA"')
    assert fetch < run.index("git diff")


# ─── Safe re-runs (issue #11) ───────────────────────────────────────────────

APPROVED_WF = REPO_ROOT / ".github" / "workflows" / "process-approved.yml"
CALLER_APPROVED = REPO_ROOT / "contractor-template" / ".github" / "workflows" / "process-approved.yml"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not available")
class TestResolveJob:
    """The `resolve` step decides whether the pipeline runs at all, so execute
    its shell for real against a stubbed `gh` returning a PR payload."""

    def _run(self, tmp_path, pr, force_resend="false"):
        import json
        import os
        step = _load(APPROVED_WF)["jobs"]["resolve"]["steps"][0]
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (tmp_path / "pr.json").write_text(json.dumps(pr))
        gh = bindir / "gh"
        gh.write_text(f"#!/bin/sh\ncat {tmp_path / 'pr.json'}\n")
        gh.chmod(0o755)
        out, summary = tmp_path / "out", tmp_path / "summary"
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
                 "REPO": "Q/r", "PR_NUMBER": "12", "FORCE_RESEND": force_resend,
                 "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)},
        )
        outputs = dict(
            line.split("=", 1) for line in (out.read_text().splitlines() if out.exists() else [])
        )
        return result, outputs

    PR = {"merged": True, "merge_commit_sha": "abc123", "merged_by": {"login": "mmcky"},
          "body": "Submission.\n\nCloses #7\n", "labels": [{"name": "submission"}]}

    def test_merged_submission_runs(self, tmp_path):
        result, out = self._run(tmp_path, self.PR)
        assert result.returncode == 0, result.stderr
        assert out == {"merge_sha": "abc123", "merged_by": "mmcky",
                       "issue_number": "7", "run": "true"}

    def test_processed_pr_is_left_alone(self, tmp_path):
        pr = {**self.PR, "labels": [{"name": "submission"}, {"name": "processed"}]}
        result, out = self._run(tmp_path, pr)
        assert result.returncode == 0, result.stderr
        assert out["run"] == "false"

    def test_force_resend_runs_a_processed_pr(self, tmp_path):
        pr = {**self.PR, "labels": [{"name": "submission"}, {"name": "processed"}]}
        _, out = self._run(tmp_path, pr, force_resend="true")
        assert out["run"] == "true"

    def test_body_without_closes_falls_back_to_the_pr(self, tmp_path):
        result, out = self._run(tmp_path, {**self.PR, "body": None})
        assert result.returncode == 0, result.stderr
        assert out["issue_number"] == "12"

    @pytest.mark.parametrize("change", [{"merged": False}, {"labels": []}])
    def test_refuses_unmerged_or_non_submission_prs(self, tmp_path, change):
        result, out = self._run(tmp_path, {**self.PR, **change})
        assert result.returncode != 0
        assert "run" not in out


class TestApprovalPipelineIsRerunSafe:
    def _steps(self):
        return {_id(s): s for s in _steps(_load(APPROVED_WF))}

    def test_process_job_skips_finished_prs_and_serialises_per_pr(self):
        job = _load(APPROVED_WF)["jobs"]["process"]
        assert job["needs"] == "resolve"
        assert job["if"] == "needs.resolve.outputs.run == 'true'"
        assert "inputs.pr_number" in job["concurrency"]["group"]
        assert job["concurrency"]["cancel-in-progress"] is False

    def test_pr_facts_come_from_resolve_not_caller_inputs(self):
        """Callers may omit these (a dispatch passes only the PR number)."""
        process = yaml.safe_dump(_load(APPROVED_WF)["jobs"]["process"])
        for name in ("inputs.merged_by", "inputs.merge_commit_sha", "inputs.pr_body"):
            assert name not in process

    def test_email_send_is_recorded_on_main_right_after_it(self):
        ids = list(self._steps())
        assert ids.index("record_email") == ids.index("email") + 1
        record = self._steps()["record_email"]
        assert "if" not in record          # implicit success(): only after a send
        assert "scripts.push_main" in record["run"]

    def test_force_resend_reaches_the_email_script(self):
        email = self._steps()["email"]
        assert email["env"]["FORCE_RESEND"] == "${{ inputs.force_resend }}"
        assert "--force-resend" in email["run"]

    def test_both_commits_use_the_retrying_push(self):
        for step_id in ("commit_back", "record_email"):
            assert "python -m scripts.push_main" in self._steps()[step_id]["run"]

    def test_failure_report_covers_the_record_step(self):
        report = self._steps()["Report pipeline failure on the PR and issue"]
        assert report["env"]["O_RECORD"] == "${{ steps.record_email.outcome }}"


class TestCallerCanRedriveAPr:
    def test_dispatch_takes_a_pr_number_and_force_resend(self):
        wf = _load(CALLER_APPROVED)
        triggers = wf.get("on") or wf.get(True)
        inputs = triggers["workflow_dispatch"]["inputs"]
        assert inputs["pr_number"]["required"] is True
        assert inputs["force_resend"]["type"] == "boolean"
        assert inputs["force_resend"]["default"] is False

    def test_job_passes_the_pr_number_for_both_triggers(self):
        job = _load(CALLER_APPROVED)["jobs"]["process"]
        assert "workflow_dispatch" in job["if"]
        # Dispatch inputs can arrive as strings; the engine input is a number,
        # and a type mismatch fails the run before any job starts (seen in the
        # E2E for #11). So the caller must convert, not pass through.
        assert job["with"]["pr_number"] == (
            "${{ fromJSON(format('{0}', github.event.pull_request.number || inputs.pr_number)) }}"
        )
        assert job["with"]["force_resend"] == (
            "${{ inputs.force_resend == true || inputs.force_resend == 'true' }}"
        )



# ─── Supply chain (issue #9) ────────────────────────────────────────────────

class TestSecretsAreExplicit:
    """`secrets: inherit` handed every org secret to whatever the engine ref
    pointed at. Only the approval pipeline needs any (the mail credentials)."""

    SMTP = {"SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
            "PSL_EMAIL", "QUANTECON_EMAIL_REVIEWER"}

    @pytest.mark.parametrize("path", CALLER_WORKFLOWS, ids=lambda p: p.name)
    def test_no_caller_inherits_secrets(self, path):
        for name, job in (_load(path).get("jobs") or {}).items():
            assert job.get("secrets") != "inherit", f"{path.name}: job `{name}`"

    def test_only_the_approval_caller_passes_secrets_and_only_smtp(self):
        for path in CALLER_WORKFLOWS:
            for job in (_load(path).get("jobs") or {}).values():
                passed = set(job.get("secrets") or {})
                if path.name == "process-approved.yml":
                    assert passed == self.SMTP
                else:
                    assert not passed, f"{path.name} passes {sorted(passed)}"

    @pytest.mark.parametrize("path", ENGINE_WORKFLOWS, ids=lambda p: p.name)
    def test_engine_uses_only_secrets_it_declares(self, path):
        workflow = _load(path)
        triggers = workflow.get("on") or workflow.get(True) or {}
        call = triggers.get("workflow_call") if isinstance(triggers, dict) else None
        if call is None:
            pytest.skip("not a reusable workflow")
        declared = set((call or {}).get("secrets") or {})
        used = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", path.read_text(encoding="utf-8")))
        assert used - {"GITHUB_TOKEN"} <= declared, (
            f"{path.name} uses undeclared secrets {sorted(used - {'GITHUB_TOKEN'} - declared)}"
        )
        if path.name == "process-approved.yml":
            assert declared == self.SMTP
        else:
            assert not declared


class TestCallersPinAReleaseTag:
    """Callers at `@main` ran every merge to this public repo's main in every
    contractor repo straight away, with write access and the mail secrets.
    They pin a release tag; only a deliberate tag move ships engine changes."""

    @pytest.mark.parametrize("path", CALLER_WORKFLOWS, ids=lambda p: p.name)
    def test_template_callers_use_a_version_tag(self, path):
        for name, job in (_load(path).get("jobs") or {}).items():
            uses = job.get("uses") or ""
            if "contractor-payments" not in uses:
                continue
            ref = uses.rsplit("@", 1)[-1]
            assert re.fullmatch(r"v\d+(\.\d+\.\d+)?", ref), (
                f"{path.name}: job `{name}` pins the engine at @{ref}; pin a "
                f"release tag (v1 or vX.Y.Z), not a branch."
            )


def test_python_dependency_is_pinned_in_the_engine_workflows():
    for path in ENGINE_WORKFLOWS:
        for step in _steps(_load(path)):
            run = step.get("run") or ""
            if run.strip().startswith("pip install pyyaml"):
                assert re.search(r"pyyaml==\d+\.\d+\.\d+", run), f"{path.name}: {run.strip()}"


# ─── Concurrency (issue #10) ────────────────────────────────────────────────

def test_submissions_run_one_at_a_time_per_issue():
    job = _load(REPO_ROOT / ".github" / "workflows" / "process-submission.yml")["jobs"]["process"]
    assert "inputs.issue_number" in job["concurrency"]["group"]
    assert job["concurrency"]["cancel-in-progress"] is False


class TestCommitBackRebuildsOnConflict:
    """Two approvals on one contract both append to its ledger, so the second
    push's rebase conflicts. commit_back replays verify + stamp + ledger on
    the updated main instead of failing. The replay is shell text inside YAML,
    so execute it, and check it repeats the real steps exactly."""

    def _steps(self):
        return {_id(s): s for s in _steps(_load(APPROVED_WF))}

    def _replay(self):
        run = self._steps()["commit_back"]["run"]
        m = re.search(r"REPLAY='(.*?)'\n", run, re.S)
        assert m, "commit_back defines no REPLAY='...'"
        assert '--replay "$REPLAY"' in run
        assert "--paths submissions/ generated_pdfs/ ledger/" in run
        return m.group(1)

    @staticmethod
    def _commands(script):
        import shlex
        script = script.replace("\\\n", " ")
        start = script.index("python -m scripts.")
        return [shlex.split(line) for line in script[start:].splitlines()
                if line.strip().startswith("python -m scripts.")]

    def test_replay_repeats_the_verify_stamp_and_ledger_steps(self):
        replay = self._commands(self._replay())
        steps = self._steps()
        expected = [self._commands(steps[s]["run"])[0]
                    for s in ("verify", "stamp", "update_ledger")]
        assert replay == expected

    def test_replay_has_the_env_its_commands_use(self):
        env = self._steps()["commit_back"]["env"]
        assert env["SUBMISSION_FILE"] == "${{ steps.find_submission.outputs.submission_file }}"
        assert env["MERGED_BY"] == "${{ needs.resolve.outputs.merged_by }}"
        assert env["PYTHONPATH"] == "engine"

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_replay_runs_with_values_expanded_by_the_child_shell(self, tmp_path):
        import os
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "calls"
        fake = bindir / "python"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {log}\necho >> {log}\n')
        fake.chmod(0o755)
        result = subprocess.run(
            ["bash", "-ec", self._replay()], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
                 "SUBMISSION_FILE": "submissions/2026-08/x y.yml", "MERGED_BY": "mmcky"},
        )
        assert result.returncode == 0, result.stderr
        calls = log.read_text().splitlines()
        assert [c.split("|")[1] for c in calls] == [
            "scripts.verify_approval", "scripts.finalize_approval", "scripts.update_ledger"]
        assert all("submissions/2026-08/x y.yml" in c.split("|") for c in calls)
        assert "mmcky" in calls[1].split("|")



def test_recipient_addresses_are_read_as_secrets():
    """Org variables print verbatim in the step's env block of the log, which
    contractors can read; secrets are masked (issue #20). The switch-over
    fallback to `vars.*` is gone, and must not come back."""
    email = next(s for s in _steps(_load(APPROVED_WF)) if _id(s) == "email")
    for name in ("PSL_EMAIL", "QUANTECON_EMAIL_REVIEWER"):
        assert email["env"][name] == f"${{{{ secrets.{name} }}}}"
    for path in ENGINE_WORKFLOWS + CALLER_WORKFLOWS:
        assert "vars." not in path.read_text(encoding="utf-8"), path.name
    others = [s for s in _steps(_load(APPROVED_WF)) if _id(s) != "email"]
    assert not any("PSL_EMAIL" in yaml.safe_dump(s) for s in others)
