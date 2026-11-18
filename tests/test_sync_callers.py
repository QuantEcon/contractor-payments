"""onboarding/sync_callers.py: write the template callers into a contractor
repo checkout at a pinned engine ref."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sync_callers", ROOT / "onboarding" / "sync_callers.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

OLD_CALLER = """\
jobs:
  process:
    uses: QuantEcon/contractor-payments/.github/workflows/process-approved.yml@main
    with:
      pr_number: 1
      engine_ref: main
    secrets: inherit
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "process-approved.yml").write_text(OLD_CALLER)
    return tmp_path


def _pins(text):
    return (
        set(m.group(2) for m in sync._USES.finditer(text)),
        set(m.group(2) for m in sync._ENGINE_REF.finditer(text)),
    )


def test_template_pins_a_single_release_tag():
    assert sync.template_ref() == "v1"


def test_sync_writes_every_caller_at_the_template_ref(repo):
    changed = sync.sync_repo(repo, "v1")
    assert sorted(changed) == sorted(p.name for p in sync.TEMPLATE_WORKFLOWS.glob("*.yml"))
    for name in changed:
        text = (repo / ".github" / "workflows" / name).read_text()
        assert text == (sync.TEMPLATE_WORKFLOWS / name).read_text()
    assert "secrets: inherit" not in (repo / ".github/workflows/process-approved.yml").read_text()


def test_sync_to_main_for_the_test_repo_keeps_both_halves_in_step(repo):
    sync.sync_repo(repo, "main")
    for path in (repo / ".github" / "workflows").glob("*.yml"):
        uses, engine_refs = _pins(path.read_text())
        assert uses == {"main"} and engine_refs == {"main"}, path.name
        yaml.safe_load(path.read_text())    # still valid YAML


def test_sync_is_idempotent_and_check_reports_drift(repo):
    assert sync.sync_repo(repo, "v1", check=True)          # drift, nothing written
    assert (repo / ".github/workflows/process-approved.yml").read_text() == OLD_CALLER
    sync.sync_repo(repo, "v1")
    assert sync.sync_repo(repo, "v1") == []
    assert sync.main(["--check", str(repo)]) == 0
    sync.sync_repo(repo, "main")
    assert sync.main(["--check", str(repo)]) == 1


@pytest.mark.parametrize("bad", ["v1 # x", "main\nengine_ref: evil", "", "-rf"])
def test_refuses_unsafe_refs(bad):
    with pytest.raises(ValueError):
        sync.pin("uses: QuantEcon/contractor-payments/.github/workflows/x.yml@main", bad)


def test_refuses_a_directory_that_is_not_a_repo(tmp_path):
    with pytest.raises(SystemExit):
        sync.sync_repo(tmp_path, "v1")
