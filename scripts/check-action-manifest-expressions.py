#!/usr/bin/env python3
"""Reject template expressions outside ``runs.steps.*`` in composite action manifests.

The Actions runner template-parses a whole ``action.yml`` when it loads the
action, not just the parts that look executable. Contexts that are perfectly
legal in a *workflow* -- ``job``, ``needs``, ``secrets`` -- are not legal in an
*action manifest*, and one of them anywhere outside ``runs.steps.*`` makes the
action fail to load at **every** call site with::

    Unrecognized named-value: 'job'

That is a production outage across every workflow that uses the action, and it
is invisible to the rest of the toolchain: prettier, check-jsonschema's
check-github-actions and actionlint all pass such a file, because none of them
evaluate manifest templates the way the runner does. It shipped once already
(``report-command-outcome``, a ``${{ job.status }}`` written as documentation
inside an input *description*), so it gets a dedicated check.

Only ``inputs.*`` and ``env`` under ``runs.steps.*`` may carry expressions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)

# Contexts the runner accepts inside an action manifest's runs.steps.* section.
ALLOWED_CONTEXTS = {"inputs", "env", "github", "runner", "steps", "strategy", "matrix"}


def _walk(node: object, path: str = "") -> list[tuple[str, str]]:
    """Yield (path, expression) for every template expression under ``node``."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _walk(value, f"{path}[{index}]")
    elif isinstance(node, str):
        found += [(path, m.group(1).strip()) for m in EXPRESSION.finditer(node)]
    return found


def check(manifest: Path) -> list[str]:
    try:
        doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # malformed YAML is check-yaml's job, not ours
        return [f"{manifest}: could not parse ({exc.__class__.__name__})"]
    if not isinstance(doc, dict) or "runs" not in doc:
        return []

    errors: list[str] = []
    for path, expression in _walk(doc):
        # runs.steps.* is the only region the runner evaluates at step time.
        if path.startswith("runs.steps"):
            root = re.split(r"[.\[(]", expression.lstrip("!( "), maxsplit=1)[0]
            if root and root not in ALLOWED_CONTEXTS:
                errors.append(
                    f"{manifest}: '{expression}' at {path} uses the '{root}' context, "
                    f"which an action manifest cannot resolve."
                )
            continue
        errors.append(
            f"{manifest}: template expression '${{{{ {expression} }}}}' at '{path}' is "
            f"outside runs.steps.* -- the runner parses it on load and the action will "
            f"fail to load at every call site. Describe it in prose, or move it to a "
            f"YAML comment."
        )
    return errors


def main(argv: list[str]) -> int:
    errors: list[str] = []
    for name in argv:
        path = Path(name)
        if path.name in {"action.yml", "action.yaml"}:
            errors += check(path)
    for error in errors:
        print(error, file=sys.stderr)  # noqa: T201 — this is a CLI lint hook
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
