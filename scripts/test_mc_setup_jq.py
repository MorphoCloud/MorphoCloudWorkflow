"""The shared "setup finished" rule, .github/actions/setup-state/mc-setup.jq
(SETUP_FINISHED_SIGNAL_DESIGN.md, section 4.B)."""

import json
import pathlib
import shutil
import subprocess

import pytest

LIB = (
    pathlib.Path(__file__).resolve().parent.parent
    / ".github"
    / "actions"
    / "setup-state"
)

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def run(props, fn="mc_setup_done"):
    out = subprocess.run(
        ["jq", "-c", "-L", str(LIB), f'include "mc-setup"; {fn}'],
        input=json.dumps(props),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


EXO_COMPLETE = '{"status":"complete", "epoch": 1763000000000}'
EXO_WAITING = '{"status":"waiting","epoch":null}'
EXO_ERROR = '{"status":"error","epoch":1763000000000}'


@pytest.mark.parametrize(
    ("props", "done"),
    [
        # mcSetup present: it alone decides.
        (
            {"exoSetup": EXO_COMPLETE, "mcSetup": "pending"},
            False,
        ),  # stopped after the early write
        ({"exoSetup": EXO_COMPLETE, "mcSetup": "complete"}, True),
        ({"exoSetup": EXO_WAITING, "mcSetup": "pending"}, False),
        ({"exoSetup": EXO_ERROR, "mcSetup": "pending"}, False),
        # mcSetup absent: the old exoSetup rule (instances from before the change).
        ({"exoSetup": EXO_COMPLETE}, True),
        ({"exoSetup": EXO_WAITING}, False),
        ({"exoSetup": EXO_ERROR}, False),
        ({"exoSetup": {"status": "complete"}}, True),  # a client that decodes it
        ({"exoSetup": "not json"}, False),
        ({}, False),
        (None, False),
        ("", False),
        (5, False),
        # The flat string some openstack client versions print.
        ("exoSetup='" + EXO_COMPLETE + "', exoGuac='{\"v\":1}'", True),
        ("exoSetup='" + EXO_COMPLETE + "', mcSetup='pending'", False),
        ("exoSetup='" + EXO_COMPLETE + "', mcSetup='complete'", True),
        ("exoSetup='" + EXO_WAITING + "'", False),
    ],
)
def test_setup_done(props, done):
    assert run(props) is done


def test_exo_setup_text_keeps_the_error_check():
    assert '"error"' in run(
        {"exoSetup": EXO_ERROR, "mcSetup": "pending"}, "mc_exo_setup_text"
    )
    assert '"error"' in run("exoSetup='" + EXO_ERROR + "'", "mc_exo_setup_text")
    assert run(None, "mc_exo_setup_text") == ""
    assert run({}, "mc_exo_setup_text") == ""
