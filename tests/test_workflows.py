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


class TestEngineCheckoutIsPinnedToTheWorkflow:
    """Every reusable workflow must run the engine scripts from a pinned ref.

    `actions/checkout` with no `ref:` takes the repository's default branch, so
    a caller pinned to `@some-branch` got that branch's workflow YAML driving
    `main`'s scripts. The mismatch is invisible until the YAML passes a flag
    the scripts don't have — which is how it surfaced on the test repo:
    `parse_issue.py: error: unrecognized arguments: --reimbursements`.

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
