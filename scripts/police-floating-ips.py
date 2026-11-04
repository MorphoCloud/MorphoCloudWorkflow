#!/usr/bin/env python3
"""Keep the project's floating IPs at max(N, running + 1) and nothing more.

INSTANCE_DASHBOARD_PLAN.md decision 24. Runs from the acquire runner's
crontab every five minutes (the runner is the clock, decision 21); the same
code joins the portal runner's minute loop with the migration.

What one run does, in this order:

1. A floating IP bound to a SHELVED server is released (Jetstream2's policy:
   an address never stays on a shelved server; a failed unshelve leaves one).
2. A non-routable address (anything outside 149.165/16 — Jetstream2 hands out
   172.17.x when its pool is dry) is deleted when unattached; one bound to a
   running server is reported, since that server is unreachable.
3. With no spare left, one is minted ahead so the next create or unshelve
   finds an address in hand (create-ip does the same the moment it takes the
   last one; this is the backstop). A mint that fails, or returns a
   non-routable address, is the start of a drought: the admins hear once.
4. With more held than max(N, running + 1), unattached routable addresses
   idle for ten minutes or more are deleted back to Jetstream2, oldest
   first. Ten minutes keeps clear of an unshelve that picked an address and
   is about to bind it.

Alerts go to the admins through the repository's admin-alert.yml, fired with
the runner's dispatcher token; once per episode, recorded in a state file.

Environment: OS_CLOUD (required), MORPHOCLOUD_FIP_HOLD (N, default 5),
MORPHOCLOUD_DISPATCH_REPO and MORPHOCLOUD_DISPATCH_TOKEN_FILE for alerts.
``--dry-run`` prints the plan and changes nothing.
"""

from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROUTABLE_PREFIX = "149.165."
IDLE_BEFORE_RELEASE = 600
SHELVED = ("SHELVED", "SHELVED_OFFLOADED")
HOLD = int(os.environ.get("MORPHOCLOUD_FIP_HOLD", "5"))
REPO = os.environ.get("MORPHOCLOUD_DISPATCH_REPO", "")
TOKEN_FILE = Path(
    os.environ.get(
        "MORPHOCLOUD_DISPATCH_TOKEN_FILE", "~/.config/morphocloud/dispatch.pat"
    )
).expanduser()
STATE_FILE = Path("~/.config/morphocloud/fip-police.json").expanduser()
MINT_DESCRIPTION = "MorphoCloud"
# The client next to this interpreter (the runner's venv); cron has no PATH.
OPENSTACK = (
    str(Path(sys.executable).with_name("openstack"))
    if Path(sys.executable).with_name("openstack").exists()
    else "openstack"
)


def log(msg: str) -> None:
    # The cron log is the record of what was done; stdout is the right place.
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)  # noqa: T201


def os_json(*args: str):
    r = subprocess.run(
        [OPENSTACK, *args, "-f", "json"],
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return json.loads(r.stdout)


def os_run(*args: str) -> None:
    subprocess.run(
        [OPENSTACK, *args], capture_output=True, text=True, timeout=180, check=True
    )


def epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))


def inventory() -> tuple[list[dict], dict[str, str]]:
    """Every floating IP of the project, and fixed address -> server status."""
    fips = os_json("floating", "ip", "list")
    by_fixed: dict[str, str] = {}
    for s in os_json("server", "list", "--long"):
        nets = s.get("Networks") or {}
        for addrs in nets.values() if isinstance(nets, dict) else []:
            for a in addrs if isinstance(addrs, list) else []:
                by_fixed[str(a)] = str(s.get("Status") or "")
    return fips, by_fixed


def plan(
    fips: list[dict], by_fixed: dict[str, str], hold: int, now: float, updated_at
) -> tuple[list, dict]:
    """The actions for this run (pure; `updated_at(fip)` is read lazily and
    only for release candidates). Returns (actions, counts)."""
    actions: list[tuple] = []
    routable: list[tuple[dict, bool]] = []
    for f in fips:
        ip, fid, port = (
            str(f.get("Floating IP Address") or ""),
            str(f.get("ID") or ""),
            f.get("Port"),
        )
        status = by_fixed.get(str(f.get("Fixed IP Address") or ""), "")
        attached = port is not None
        if attached and status in SHELVED:
            actions.append(("unset", fid, ip, status))
            attached = False
        if not ip.startswith(ROUTABLE_PREFIX):
            if attached:
                actions.append(
                    (
                        "alert",
                        f"nonroutable:{ip}",
                        f"Non-routable address {ip} on a running instance",
                        f"Floating IP {ip} is bound to a running server (fixed "
                        f"{f.get('Fixed IP Address')}, status {status or 'unknown'}). Nothing "
                        "reaches it. Shelve the instance and unshelve it: the next address "
                        "is checked before use.",
                    )
                )
            else:
                actions.append(("delete", fid, ip, "non-routable"))
            continue
        routable.append((f, attached))
    held = len(routable)
    running = sum(1 for _, a in routable if a)
    spares = held - running
    target = max(hold, running + 1)
    counts = {
        "held": held,
        "running": running,
        "spares": spares,
        "target": target,
        "hold": hold,
    }
    if spares == 0:
        actions.append(("mint",))
    elif held > target:
        idle = []
        for f, attached in routable:
            if attached:
                continue
            at = updated_at(f)
            if now - at >= IDLE_BEFORE_RELEASE:
                idle.append((at, f))
        idle.sort(key=lambda x: x[0])
        for _, f in idle[: held - target]:
            actions.append(
                ("delete", str(f["ID"]), str(f["Floating IP Address"]), "over the hold")
            )
    return actions, counts


# --- alerts, once per episode -------------------------------------------


def load_state() -> dict:
    try:
        with STATE_FILE.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as fh:
        json.dump(state, fh)


def send_alert(subject: str, body: str) -> bool:
    """Through the repository's admin-alert.yml; the runner cannot mail."""
    try:
        with TOKEN_FILE.open(encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        token = ""
    if not REPO or not token:
        log(f"ALERT (no dispatch repo/token): {subject} -- {body}")
        return False
    r = subprocess.run(
        [
            "gh",
            "workflow",
            "run",
            "admin-alert.yml",
            "--repo",
            REPO,
            "--ref",
            "main",
            "-f",
            f"subject={subject}",
            "-f",
            f"body={body}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GH_TOKEN": token},
    )
    if r.returncode != 0:
        log(f"ALERT could not be dispatched ({r.stderr.strip()[-200:]}): {subject}")
        return False
    log(f"alert dispatched: {subject}")
    return True


def alert_once(state: dict, key: str, subject: str, body: str) -> None:
    if key in state:
        return
    if send_alert(subject, body):
        state[key] = time.time()


def clear_alert(state: dict, key: str) -> None:
    state.pop(key, None)


# --- the run --------------------------------------------------------------


def mint(state: dict) -> None:
    """One routable address ahead; a drought is the admins' business, once."""
    try:
        created = os_json(
            "floating", "ip", "create", "public", "--description", MINT_DESCRIPTION
        )
    except (subprocess.CalledProcessError, ValueError) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip()[-300:]
        log(f"mint failed: {detail}")
        alert_once(
            state,
            "mint-failed",
            "Floating-IP mint failed on Jetstream2",
            f"openstack floating ip create failed: {detail}\n\nNo spare address is in hand; "
            f"each create or unshelve from now on fails for want of one until Jetstream2 "
            "has addresses again. Nothing to do on our side but wait, or ask Jetstream2.",
        )
        return
    ip, fid = (
        str(created.get("floating_ip_address") or ""),
        str(created.get("id") or ""),
    )
    if not ip.startswith(ROUTABLE_PREFIX):
        log(f"mint returned non-routable {ip}: deleting it")
        os_run("floating", "ip", "delete", fid)
        alert_once(
            state,
            "mint-failed",
            "Floating-IP mint failed on Jetstream2 (non-routable address)",
            f"Jetstream2 handed out {ip}, which does not route; it was deleted. Its public pool "
            "is dry. No spare address is in hand; each create or unshelve from now on fails "
            "for want of one until Jetstream2 has addresses again.",
        )
        return
    log(f"minted {ip} (one in hand)")
    clear_alert(state, "mint-failed")


def run(dry_run: bool) -> int:
    fips, by_fixed = inventory()
    state = load_state()
    now = time.time()
    cache: dict[str, float] = {}

    def updated_at(f: dict) -> float:
        fid = str(f["ID"])
        if fid not in cache:
            cache[fid] = epoch(os_json("floating", "ip", "show", fid).get("updated_at"))
        return cache[fid]

    actions, counts = plan(fips, by_fixed, HOLD, now, updated_at)
    log(
        f"held={counts['held']} running={counts['running']} spares={counts['spares']} "
        f"target={counts['target']} (hold {counts['hold']}) actions={len(actions)}"
    )
    seen_nonroutable = set()
    for a in actions:
        kind = a[0]
        if kind == "unset":
            log(
                f"{'would release' if dry_run else 'releasing'} {a[2]} from a {a[3]} server"
            )
            if not dry_run:
                os_run("floating", "ip", "unset", "--port", a[1])
        elif kind == "delete":
            log(f"{'would delete' if dry_run else 'deleting'} {a[2]} ({a[3]})")
            if not dry_run:
                os_run("floating", "ip", "delete", a[1])
        elif kind == "mint":
            log("no spare in hand: " + ("would mint one" if dry_run else "minting one"))
            if not dry_run:
                mint(state)
        elif kind == "alert":
            seen_nonroutable.add(a[1])
            if not dry_run:
                alert_once(state, a[1], a[2], a[3])
            else:
                log(f"would alert: {a[2]}")
    for key in [
        k for k in state if k.startswith("nonroutable:") and k not in seen_nonroutable
    ]:
        clear_alert(state, key)  # gone: the next one alerts again
    if not dry_run:
        save_state(state)
    return 0


def main(argv: list[str]) -> int:
    if not os.environ.get("OS_CLOUD"):
        print("OS_CLOUD is not set", file=sys.stderr)  # noqa: T201 — CLI usage error
        return 2
    try:
        return run(dry_run="--dry-run" in argv)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
        OSError,
    ) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip()[-300:]
        log(f"run failed: {detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
