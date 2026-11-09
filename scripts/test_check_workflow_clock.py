"""The crontab renderer's store field (INCIDENT_STORE_PHASE1_DESIGN.md,
"Renderer and restore changes"). Run: python -m pytest scripts/test_check_workflow_clock.py"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "clock", SCRIPTS / "check-workflow-clock.py"
)
clock = importlib.util.module_from_spec(spec)
sys.modules["clock"] = clock
spec.loader.exec_module(clock)

CLOCK = {"follow-through.yml": "*/5 * * * *", "close-expired-issues.yml": "0 12 * * *"}


def test_harvester_line_present_exactly_when_the_store_is_set():
    for rel, (repo, cloud, hold, _extra, _store) in clock.TARGETS.items():
        with_store = clock.render(
            CLOCK, repo, cloud, hold, None, "MorphoCloud/Incidents-x"
        ).splitlines()
        without = clock.render(CLOCK, repo, cloud, hold, None, None).splitlines()
        harvester = [line for line in with_store if "harvest-incidents.py" in line]
        assert len(harvester) == 1, rel
        assert "MORPHOCLOUD_STORE_REPO=MorphoCloud/Incidents-x" in harvester[0]
        assert f"MORPHOCLOUD_SOURCE_REPO={repo}" in harvester[0]
        assert f"OS_CLOUD={cloud}" in harvester[0]
        assert "None" not in "\n".join(with_store)
        assert not any("harvest-incidents" in line for line in without)
        # nothing else changed
        assert [
            line for line in with_store if "harvest-incidents" not in line
        ] == without


def test_targets_carry_a_store_field():
    for rel, target in clock.TARGETS.items():
        assert len(target) == len(("repo", "cloud", "hold", "extra", "store")), rel
    assert (
        clock.TARGETS["scripts/instances-runner.crontab"][4] == "MorphoCloud/Incidents"
    )


def test_committed_crontabs_are_current():
    found, errors = clock.scan()
    assert errors == []
    assert clock.crontab_errors(found, write=False) == []
