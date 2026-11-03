#!/usr/bin/env python3
"""The runner's crontab is the clock; keep every workflow registered with it.

GitHub's ``on: schedule`` is best-effort. Measured over Oct 27 - Nov 3 2026 on
both Instances and Test-Instances, a ``7,37 * * * *`` workflow fired 3-5 times a
day and a ``*/5`` one about the same -- whatever the cron line says, a scheduled
workflow runs a few times a day. The real clock is a crontab on the acquire
runner that fires ``gh workflow run`` on time (``runner-cron-dispatcher.md``).

That clock was a hand-edited list on the VM, so every scheduled workflow written
after it was installed (``reconcile-instances`` among them) silently ran a few
times a day while its cron line promised every half hour. This hook closes that
gap by making the crontab a *rendered* file:

1. A time-driven workflow carries a marker comment ``# dispatch: <cron>`` in
   its ``on:`` block (markers elsewhere in the file are ignored). Its
   ``on: schedule`` stays as the backstop (and as the only clock in MC-* course
   repos, which have no dispatcher by the maintainer's decision), and must equal
   the marker.
2. A workflow whose only trigger is ``workflow_dispatch`` must say what drives
   it: a ``# dispatch: <cron>`` marker, or ``# dispatch: manual`` when a person
   or another workflow dispatches it on demand. Otherwise a forgotten marker
   looks exactly like a manual workflow.
3. The committed crontabs under ``scripts/`` are rendered from the markers.
   ``check`` fails when one is stale; ``render`` rewrites them.

Downstream repos get this script with the vendorized pre-commit config but not
the crontabs, so rules 1-2 apply everywhere and rule 3 only here.

Usage::

    check-workflow-clock.py check            # pre-commit / CI
    check-workflow-clock.py render           # rewrite scripts/*.crontab
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
MARKER = re.compile(r"^\s*#\s*dispatch:\s*(.+?)\s*$", re.MULTILINE)
# The marker lives in the ``on:`` block: from the ``on:`` line to the next
# top-level key. A "dispatch:" mentioned in a run: script or a comment elsewhere
# is not a marker.
ON_BLOCK = re.compile(r"^on:[^\n]*\n(?:(?:[ \t#][^\n]*)?\n)*", re.MULTILINE)
CRON = re.compile(r"^(\S+\s+){4}\S+$")
DISPATCHER = "/home/exouser/morphocloud-dispatch.sh"

# Rendered crontabs: committed path -> (repository the lines dispatch to, lines
# appended verbatim after the rendered ones, or None).
TARGETS = {
    "scripts/instances-runner.crontab": (
        "MorphoCloud/Instances",
        "scripts/instances-runner.crontab.extra",
    ),
    "scripts/test-instances-runner.crontab": ("MorphoCloud/Test-Instances", None),
}


def _triggers(doc: dict) -> dict:
    # PyYAML reads an unquoted ``on`` key as the boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


def _schedule_cron(triggers: dict) -> str | None:
    entries = triggers.get("schedule") or []
    crons = [e.get("cron") for e in entries if isinstance(e, dict)]
    return " ".join(crons) if crons else None


def scan() -> tuple[dict[str, str], list[str]]:
    """Return {workflow file name: cron} for the time-driven workflows, and errors."""
    clock: dict[str, str] = {}
    errors: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # malformed YAML is check-yaml's job
            errors.append(f"{path}: could not parse ({exc.__class__.__name__})")
            continue
        triggers = _triggers(doc if isinstance(doc, dict) else {})
        on_block = ON_BLOCK.search(text)
        markers = MARKER.findall(on_block.group(0) if on_block else "")
        if len(markers) > 1:
            errors.append(f"{path}: more than one '# dispatch:' marker")
            continue
        marker = markers[0] if markers else None
        cron = _schedule_cron(triggers)
        name = path.name

        if marker and marker != "manual" and not CRON.match(marker):
            errors.append(
                f"{path}: '# dispatch: {marker}' is neither a 5-field cron nor 'manual'"
            )
            continue
        if cron and not marker:
            errors.append(
                f"{path}: has 'on: schedule' but no '# dispatch: {cron}' marker. "
                f"GitHub fires a schedule a few times a day at best; the runner "
                f"crontab is the clock, and it is rendered from the markers."
            )
        elif cron and marker != cron:
            errors.append(
                f"{path}: '# dispatch: {marker}' does not match the schedule cron "
                f"'{cron}'. The schedule is the backstop; keep them equal."
            )
        elif marker and marker != "manual" and not cron:
            errors.append(
                f"{path}: '# dispatch: {marker}' needs a matching 'on: schedule' "
                f"backstop (it is the only clock in course repos)."
            )
        elif marker == "manual" and cron:
            errors.append(f"{path}: '# dispatch: manual' but has 'on: schedule'")
        elif not marker and set(triggers) == {"workflow_dispatch"}:
            errors.append(
                f"{path}: only 'workflow_dispatch' triggers it. Add '# dispatch: "
                f"<cron>' so the runner crontab fires it, or '# dispatch: manual' "
                f"if a person or another workflow dispatches it on demand."
            )
        if marker and marker != "manual" and marker == cron:
            clock[name] = marker
    return clock, errors


def render(clock: dict[str, str], repo: str, extra: Path | None) -> str:
    by_cron: dict[str, list[str]] = {}
    for name, cron in sorted(clock.items()):
        by_cron.setdefault(cron, []).append(name)
    lines = [
        f"# {repo} dispatcher crontab — ACQUIRE runner ONLY (a copy on the control",
        "# runner double-fires every line). RENDERED by scripts/check-workflow-clock.py",
        "# from the '# dispatch:' markers in .github/workflows; edit those, not this.",
        "# Installed by scripts/restore-instances-dispatcher.sh; see runner-cron-dispatcher.md.",
    ]
    for cron, names in sorted(by_cron.items(), key=lambda kv: kv[1]):
        lines.append(
            f"{cron} MORPHOCLOUD_DISPATCH_REPO={repo} {DISPATCHER} {' '.join(names)}"
        )
    if extra is not None:
        lines.append("# --- appended verbatim from " + extra.name + " ---")
        lines.extend(extra.read_text(encoding="utf-8").rstrip("\n").splitlines())
    return "\n".join(lines) + "\n"


def crontab_errors(clock: dict[str, str], write: bool) -> list[str]:
    errors: list[str] = []
    for rel, (repo, extra_rel) in TARGETS.items():
        path = ROOT / rel
        extra = ROOT / extra_rel if extra_rel else None
        if not path.exists():  # a vendorized copy: no crontabs to keep current
            continue
        if write:
            path.write_text(render(clock, repo, extra), encoding="utf-8")
        elif path.read_text(encoding="utf-8") != render(clock, repo, extra):
            errors.append(
                f"{path}: stale. Run 'python scripts/check-workflow-clock.py render'."
            )
    return errors


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "check"
    if mode not in {"check", "render"}:
        print(__doc__, file=sys.stderr)  # noqa: T201 — CLI usage
        return 2
    clock, errors = scan()
    if not errors:
        errors += crontab_errors(clock, write=(mode == "render"))
    for error in errors:
        print(error, file=sys.stderr)  # noqa: T201 — this is a CLI lint hook
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
