"""The DISPATCH-line pre-commit check (INCIDENT_STORE_PHASE1_DESIGN.md).
Run: python -m pytest scripts/test_check_dispatch_logging.py"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "cdl", SCRIPTS / "check-dispatch-logging.py"
)
cdl = importlib.util.module_from_spec(spec)
sys.modules["cdl"] = cdl
spec.loader.exec_module(cdl)

SHELL_OK = """\
for wf in "$@"; do
  if err=$(gh workflow run "$wf" --repo "$REPO" 2>&1 >/dev/null); then
    echo "$ts DISPATCH $wf repo=$REPO issue=- by=x.sh result=ok" >>"$LOG"
  fi
done
"""

SHELL_MISSING = """\
# gh workflow run in a comment does not count
gh workflow run "$wf" --repo "$REPO"
echo "dispatched $wf"
"""

PY_OK = """\
    r = subprocess.run(
        [
            "gh",
            "workflow",
            "run",
            "admin-alert.yml",
        ],
        check=False,
    )
    log(f"DISPATCH admin-alert.yml repo={REPO} issue=- by=p.py result={res}")
"""

PY_MISSING = """\
    r = subprocess.run(
        [
            "gh",
            "workflow",
            "run",
            "admin-alert.yml",
        ],
        check=False,
    )
    if r.returncode != 0:
        log("ALERT could not be dispatched")
"""

PY_NOT_A_SITE = """\
    q = subprocess.run(
        [
            "gh",
            "run",
            "list",
        ],
    )
"""


def test_shell_sites():
    assert cdl.check_text(SHELL_OK, python=False) == []
    assert cdl.check_text(SHELL_MISSING, python=False) == [2]


def test_python_sites():
    assert cdl.check_text(PY_OK, python=True) == []
    assert cdl.check_text(PY_MISSING, python=True) == [3]
    assert cdl.check_text(PY_NOT_A_SITE, python=True) == []


def test_dispatch_line_must_be_within_30_lines():
    far = PY_OK.replace('    log(f"DISPATCH', "\n" * 31 + '    log(f"DISPATCH')
    assert cdl.check_text(far, python=True) == [3]


def test_the_repository_scripts_pass():
    assert cdl.main([]) == 0
