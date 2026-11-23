#!/usr/bin/env python3
"""Every runner-side `gh workflow run` call site must log a DISPATCH line.

INCIDENT_STORE_PLAN.md ("Classify", actor_kind rule 1) and
INCIDENT_STORE_PHASE1_DESIGN.md ("The DISPATCH line and its check"). The
harvester tells automation from a maintainer's hand dispatch by joining a run
to a line in a runner-side log written the moment the dispatch returned:

    <UTC> DISPATCH <workflow.yml> repo=<owner/repo> issue=<n|-> by=<script> result=ok|FAILED <stderr tail>

A new dispatcher that forgets the line would make every one of its runs look
like a hand action, so this pre-commit hook fails when a call site has no
`DISPATCH ` within the next 30 lines of the same file. A call site is:

- shell: the substring ``gh workflow run`` on one line;
- Python: the string literal ``"gh"`` followed by the literals ``"workflow"``
  and ``"run"`` in that order within the next 5 lines, each on its own line
  (the way ``["gh", "workflow", "run", ...]`` is laid out one element per line
  by the formatter).

PR B extends the same check to .github/workflows and .github/actions, where
the required neighbor is a facts.sh dispatch record.

Usage: check-dispatch-logging.py [file ...]   (pre-commit passes the files)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL_SITE = re.compile(r"gh workflow run")
PY_GH = re.compile(r"""^\s*["']gh["'],?\s*$""")
PY_WORD = {
    "workflow": re.compile(r"""^\s*["']workflow["'],?\s*$"""),
    "run": re.compile(r"""^\s*["']run["'],?\s*$"""),
}
DISPATCH = "DISPATCH "
WITHIN = 30
PY_WITHIN = 5


def call_sites(lines: list[str], python: bool) -> list[int]:
    """0-based line numbers of the dispatch call sites in a file."""
    sites: list[int] = []
    for i, line in enumerate(lines):
        if not python:
            if SHELL_SITE.search(line) and not line.lstrip().startswith("#"):
                sites.append(i)
            continue
        if not PY_GH.match(line):
            continue
        window = lines[i + 1 : i + 1 + PY_WITHIN]
        pos = 0
        for word in ("workflow", "run"):
            found = next(
                (k for k in range(pos, len(window)) if PY_WORD[word].match(window[k])),
                None,
            )
            if found is None:
                break
            pos = found + 1
        else:
            sites.append(i)
    return sites


def check_text(text: str, python: bool) -> list[int]:
    """1-based line numbers of call sites with no DISPATCH line after them."""
    lines = text.splitlines()
    missing: list[int] = []
    for site in call_sites(lines, python):
        after = lines[site + 1 : site + 1 + WITHIN]
        if not any(DISPATCH in line for line in after):
            missing.append(site + 1)
    return missing


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(
        p
        for p in (ROOT / "scripts").iterdir()
        if p.suffix in (".sh", ".py") and p.name != Path(__file__).name
    )
    errors = 0
    for path in paths:
        if path.name.startswith("test_") or path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in check_text(text, python=path.suffix == ".py"):
            errors += 1
            print(  # noqa: T201 — this is a CLI lint hook
                f"{path}:{line}: `gh workflow run` with no DISPATCH log line within {WITHIN} lines. "
                f"Log `<UTC> DISPATCH <wf> repo=<owner/repo> issue=<n|-> by=<script> result=ok|FAILED <stderr>` "
                f"the moment the call returns (scripts/check-dispatch-logging.py).",
                file=sys.stderr,
            )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
