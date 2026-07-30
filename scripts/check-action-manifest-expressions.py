#!/usr/bin/env python3
"""Catch template expressions an action manifest cannot resolve.

The Actions runner template-parses a whole ``action.yml`` when it loads the
action. Two mistakes make the load fail at **every** call site with something
like ``Unrecognized named-value: 'job'``, taking down every workflow that uses
the action:

1. A workflow-only context (``job``, ``needs``, ``secrets``, ``vars``) used
   anywhere in the manifest. These exist in workflows but not in actions.
2. Any expression in a metadata field -- ``description``, ``name``, ``author``,
   ``branding``. The runner evaluates those strings too, so an expression
   written purely as *documentation* is still parsed and still fatal.

Both shipped once: ``report-command-outcome`` documented its input as
"pass ``${{ job.status }}``", which is mistake 2 containing mistake 1, and it
broke every IssueOps command in Instances.

Nothing else in the toolchain notices. prettier, check-jsonschema's
check-github-actions and actionlint all pass such a file, because none of them
evaluate manifest templates the way the runner does.

Deliberately narrow. Expressions in ``runs:`` and in ``outputs.<id>.value`` are
how composite actions are written -- ``outputs.<id>.value`` *must* carry one --
and functions like ``failure()`` and ``fromJSON()`` are legal there. Flagging
those would just train people to ignore this hook.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)

# Contexts that exist in a workflow but not in an action manifest.
FORBIDDEN_CONTEXT = re.compile(r"(?<![\w.'\"])(job|needs|secrets|vars)\s*[.\[]")

# Metadata fields the runner still evaluates; an expression here is never
# intentional, it is documentation that happens to be executable.
METADATA_KEYS = {"description", "name", "author", "branding"}


def _walk(node: object, path: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Yield (dotted path, expression body) for every expression under node."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _walk(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _walk(value, (*path, f"[{index}]"))
    elif isinstance(node, str):
        joined = ".".join(path)
        found += [(joined, m.group(1).strip()) for m in EXPRESSION.finditer(node)]
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
        if FORBIDDEN_CONTEXT.search(expression):
            errors.append(
                f"{manifest}: '{expression}' at '{path}' uses a workflow-only "
                f"context. An action manifest cannot resolve job/needs/secrets/vars, "
                f"so the runner fails to load this action at every call site. "
                f"Pass the value in as an input instead."
            )
        elif path.split(".")[-1] in METADATA_KEYS:
            errors.append(
                f"{manifest}: expression '${{{{ {expression} }}}}' at '{path}' sits in "
                f"a metadata field. The runner parses these strings on load, so even a "
                f"documentation example is executable. Describe it in prose, or move it "
                f"to a YAML comment."
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
