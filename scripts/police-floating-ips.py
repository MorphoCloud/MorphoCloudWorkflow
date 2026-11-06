#!/usr/bin/env python3
"""Keep the MorphoCloud instances' floating IPs at max(N, running + 1).

INSTANCE_DASHBOARD_PLAN.md decision 24. Runs from the acquire runner's
crontab every five minutes (the runner is the clock, decision 21); the same
code joins the portal runner's minute loop with the migration.

Only MorphoCloud instances (…_instance-<n>) count: "held" is the addresses
bound to them plus the unattached ones, "running" the addresses bound to
them. The runners, the portal and every other server of the project are
infrastructure — outside the rule, never counted, never touched.

What one run does, in this order:

1. A floating IP bound to a SHELVED instance is released (Jetstream2's
   policy: an address never stays on a shelved server; a failed unshelve
   leaves one) — but only when the server has no action in flight
   (task_state empty) and the address has been bound for ten minutes or
   more. Production binds the address before `server unshelve`, so for the
   length of an unshelve a shelved server legitimately holds one; Instances
   #214 lost its address three minutes into a hung unshelve this way. Such
   an address counts as running.
2. A non-routable address (anything outside 149.165/16 — Jetstream2 hands out
   172.17.x when its pool is dry) is deleted when unattached; one bound to a
   running server is reported, since that server is unreachable.
3. With fewer held than max(N, running + 1), one is minted per run until the
   rule is met, so a create or unshelve always finds an address in hand
   (create-ip also mints one the moment it takes the last spare). A mint
   that fails, or returns a non-routable address, is the start of a
   drought: the admins hear once.
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
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROUTABLE_PREFIX = "149.165."
# A MorphoCloud instance: instance-<n> (production), morpho-cloud-test_instance-<n>
# (Test), <prefix>_instance-<n> in general. Everything else in the project
# (runners, the portal, …) is infrastructure.
INSTANCE_RE = re.compile(r"(^|[_-])instance-[0-9]+$")
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
LOCK_FILE = Path("~/.config/morphocloud/fip-police.lock").expanduser()
ALERT_WAIT = (
    120  # seconds to wait for admin-alert.yml to finish before counting it sent
)
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
    """A missing timestamp counts as now: never idle, never a release candidate."""
    if not iso:
        return time.time()
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return time.time()


def addresses(nets) -> list[str]:
    """Every address in a server's Networks field, whatever shape the client
    renders it in: {"net": ["10.0.0.5", …]}, {"net": [{"addr": "10.0.0.5"}]},
    or the flat "net=10.0.0.5, 149.165.1.1; net2=…" of older clients."""
    out: list[str] = []
    if isinstance(nets, dict):
        for addrs in nets.values():
            for a in addrs if isinstance(addrs, list) else [addrs]:
                addr = a.get("addr") if isinstance(a, dict) else a
                if addr:
                    out.append(str(addr))
    elif isinstance(nets, str):
        for part in nets.replace(";", ",").split(","):
            a = part.split("=", 1)[-1].strip()
            if a:
                out.append(a)
    return out


# Nova task states of a server on its way up; with BUILD, and ACTIVE with no
# address yet, such an instance is about to take a spare (2026-11-06,
# Test-Instances#523: a create picked the spare, minted its replacement, and
# this script deleted the picked one as over the hold before it was attached).
STARTING_TASKS = (
    "scheduling",
    "block_device_mapping",
    "networking",
    "spawning",
    "unshelving",
)
# An over-the-hold surplus must still be there this long after it was first
# seen before an address is deleted: the next tick, not this one.
SURPLUS_SETTLE = 240


def starting_instances(servers: list[dict], fips: list[dict]) -> list[str]:
    """MorphoCloud instances on their way up that hold no address yet: each
    will take a spare in the next minutes, so each counts as running."""
    bound = {
        str(f.get("Fixed IP Address") or "") for f in fips if f.get("Port") is not None
    }
    out = []
    for s in servers:
        name = str(s.get("Name") or "")
        if INSTANCE_RE.search(name) is None:
            continue
        if any(a in bound for a in addresses(s.get("Networks"))):
            continue
        status = str(s.get("Status") or "")
        task = str(s.get("Task State") or "")
        task = "" if task in ("None", "null") else task
        if (
            status == "BUILD"
            or task in STARTING_TASKS
            or (status == "ACTIVE" and not task)
        ):
            out.append(name)
    return out


def inventory() -> tuple[list[dict], dict[str, dict], list[str]]:
    """Every floating IP of the project; fixed address -> its server:
    {"status", "task" (the action in flight, "" when none), "instance" (a
    MorphoCloud instance, as opposed to a runner, the portal or any other
    server of the project), "name"}; and the instances still starting."""
    fips = os_json("floating", "ip", "list")
    servers = os_json("server", "list", "--long")
    by_fixed: dict[str, dict] = {}
    for s in servers:
        name = str(s.get("Name") or "")
        task = str(s.get("Task State") or "")
        for a in addresses(s.get("Networks")):
            by_fixed[a] = {
                "status": str(s.get("Status") or ""),
                # `or ""` covers a JSON null; the client also renders a null
                # task state as the strings "None" / "null" in -f json.
                "task": "" if task in ("None", "null") else task,
                "instance": INSTANCE_RE.search(name) is not None,
                "name": name,
            }
    if (
        servers and not by_fixed
    ):  # servers exist but none parsed: the client's shape changed
        log(
            "WARNING: no server addresses parsed; shelved-server releases and reports are off this run"
        )
    return fips, by_fixed, starting_instances(servers, fips)


def nonroutable_alert(ip: str, f: dict, owner: dict | None, status: str) -> tuple:
    return (
        "alert",
        f"nonroutable:{ip}",
        f"Non-routable address {ip} on a running server",
        f"Floating IP {ip} is bound to {owner['name'] if owner else 'an unknown port'} "
        f"(fixed {f.get('Fixed IP Address')}, status {status or 'unknown'}). Nothing "
        "reaches it. Shelve the server and unshelve it: the next address is "
        "checked before use.",
    )


def classify(
    f: dict, by_fixed: dict[str, dict], settled=lambda _f: True
) -> tuple[str, list[tuple]]:
    """One floating IP: what it is to the rule, and what to do about it now.
    "running" / "spare": ours, counted. "infra" / "unknown": bound to a
    runner, the portal, … or to a port no listed server owns — not ours to
    count or touch (a dead address on one is still reported). "dead": a
    non-routable address of ours, deleted or reported.

    An address on a shelved instance is released only when the server has no
    action in flight and `settled(f)` says the binding is old enough: an
    unshelve binds first and boots after, and must not lose its address."""
    ip, fid, port = (
        str(f.get("Floating IP Address") or ""),
        str(f.get("ID") or ""),
        f.get("Port"),
    )
    owner = by_fixed.get(str(f.get("Fixed IP Address") or ""))
    status = owner["status"] if owner else ""
    attached = port is not None
    routable = ip.startswith(ROUTABLE_PREFIX)
    if attached and not (owner is not None and owner["instance"]):
        acts = [] if routable else [nonroutable_alert(ip, f, owner, status)]
        return ("unknown" if owner is None else "infra"), acts
    acts: list[tuple] = []
    # attached implies owner is an instance (the return above), so owner is
    # a dict here; `settled` last, so an in-flight server costs no API call.
    if attached and status in SHELVED and not owner.get("task") and settled(f):
        acts.append(("unset", fid, ip, status, owner["name"]))
        attached = False
    if not routable:
        acts.append(
            nonroutable_alert(ip, f, owner, status)
            if attached
            else ("delete", fid, ip, "non-routable")
        )
        return "dead", acts
    return ("running" if attached else "spare"), acts


def plan(  # noqa: PLR0913 — one pure function over the whole inventory
    fips: list[dict],
    by_fixed: dict[str, dict],
    hold: int,
    now: float,
    updated_at,
    starting: int = 0,
) -> tuple[list, dict]:
    """The actions for this run (pure; `updated_at(fip)` is read lazily and
    only for release candidates, including an address on a shelved
    instance). Returns (actions, counts). `starting`: instances on their way
    up without an address yet; each counts as running, so the spare it is
    about to take is never surplus.

    Only MorphoCloud instances count. An address bound to a runner, the
    portal or any other server of the project is infrastructure: outside the
    rule, never released, never deleted (a non-routable one is still
    reported). An address bound to a port no listed server owns is left
    alone too, and counted apart."""
    actions: list[tuple] = []
    routable: list[tuple[dict, bool]] = []
    tally = {"infra": 0, "unknown": 0}

    def settled(f: dict) -> bool:
        return now - updated_at(f) >= IDLE_BEFORE_RELEASE

    for f in fips:
        kind, acts = classify(f, by_fixed, settled)
        actions += acts
        if kind in ("running", "spare"):
            routable.append((f, kind == "running"))
        elif kind in tally:
            tally[kind] += 1
    held = len(routable)
    running = sum(1 for _, a in routable if a)
    spares = held - running
    target = max(hold, running + starting + 1)
    counts = {
        "held": held,
        "running": running,
        "starting": starting,
        "spares": spares,
        "target": target,
        "hold": hold,
        **tally,
    }
    if held < target:
        actions.append(("mint",))  # one per run, up to the rule
    elif held > target:
        idle = [(updated_at(f), f) for f, attached in routable if not attached]
        # Sort on the age only: two addresses with the same timestamp would
        # otherwise be compared as dicts, which raises (seen on Test
        # 2026-11-05: eleven silent crashes before anyone noticed).
        idle = sorted(
            ((at, f) for at, f in idle if now - at >= IDLE_BEFORE_RELEASE),
            key=lambda x: x[0],
        )
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


ISSUE_RE = re.compile(r"(^|[_-])instance-([0-9]+)$")  # the same boundary as INSTANCE_RE


def note_issue(server_name: str, note: str) -> None:
    """Decision 35: an automatic change to a member's instance is said on
    the member's issue, through admin-alert.yml (the runner cannot post).
    Fire and forget, unlike send_alert: a courtesy line is not worth holding
    the tick for two minutes, and the runner log keeps the record."""
    m = ISSUE_RE.search(server_name)
    try:
        with TOKEN_FILE.open(encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        token = ""
    if not m or not REPO or not token:
        log(f"NOTE (not posted): {server_name}: {note}")
        return
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
            f"issue={m.group(2)}",
            "-f",
            f"note={note}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GH_TOKEN": token},
    )
    if r.returncode != 0:
        log(
            f"NOTE could not be dispatched ({r.stderr.strip()[-200:]}): {server_name}: {note}"
        )


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
    dispatched_at = time.time()
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
    # Accepted is not delivered: wait for that run to finish, and count the
    # alert as sent only when it succeeded. Otherwise the next tick retries.
    deadline = time.time() + ALERT_WAIT
    while time.time() < deadline:
        time.sleep(10)
        q = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                REPO,
                "--workflow",
                "admin-alert.yml",
                "--limit",
                "1",
                "--json",
                "status,conclusion,createdAt",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GH_TOKEN": token},
        )
        try:
            runs = json.loads(q.stdout or "[]")
        except ValueError:
            runs = []
        if (
            runs
            and epoch(runs[0].get("createdAt")) >= dispatched_at - 5
            and runs[0].get("status") == "completed"
        ):
            if runs[0].get("conclusion") == "success":
                log(f"alert sent: {subject}")
                return True
            log(f"ALERT run failed ({runs[0].get('conclusion')}): {subject}")
            return False
    log(f"ALERT run not finished within {ALERT_WAIT}s; will retry next tick: {subject}")
    return False


def alert_once(state: dict, key: str, subject: str, body: str) -> None:
    if key in state:
        return
    if send_alert(subject, body):
        state[key] = time.time()


def clear_alert(state: dict, key: str) -> None:
    state.pop(key, None)


# --- the run --------------------------------------------------------------


PROBE_EVERY = 3600  # while a drought is open, ask Jetstream2 once an hour


def drought_body(lead: str, counts: dict) -> str:
    """The drought email: what failed, what is in hand, what that means."""
    spares, running = counts["spares"], counts["running"]
    if spares == 0:
        impact = "The next create or unshelve fails for want of an address"
    elif spares == 1:
        impact = "The next create or unshelve takes the spare; the one after that fails"
    else:
        impact = (
            f"The next {spares} creates or unshelves take the spares; the one after "
            "that fails"
        )
    return (
        f"{lead}\n\nIn hand now: {spares} spare address{'es' if spares != 1 else ''} beside "
        f"{running} running instance{'s' if running != 1 else ''}. {impact}, until a member "
        "shelves (a shelve returns its address to the spares) or Jetstream2 frees addresses. "
        "You get another email when Jetstream2 has addresses again.\n\nNothing to do on our "
        "side but wait, or ask Jetstream2."
    )


def request_address() -> tuple[str, str, str]:
    """One address from Jetstream2: (ip, id, error). A non-routable answer is
    deleted on the spot and returned with an empty id."""
    try:
        created = os_json(
            "floating", "ip", "create", "public", "--description", MINT_DESCRIPTION
        )
    except (subprocess.CalledProcessError, ValueError) as e:
        return "", "", (getattr(e, "stderr", "") or str(e)).strip()[-300:]
    ip, fid = (
        str(created.get("floating_ip_address") or ""),
        str(created.get("id") or ""),
    )
    if not ip.startswith(ROUTABLE_PREFIX):
        log(f"Jetstream2 returned non-routable {ip}: deleting it")
        os_run("floating", "ip", "delete", fid)
        return ip, "", ""
    return ip, fid, ""


def all_clear(state: dict, ip: str) -> None:
    """The drought is over: say so once, then forget it. If the email cannot
    be sent, the drought stays open and the next probe tries again."""
    if "mint-failed" not in state:
        return
    began = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(state["mint-failed"]))
    if send_alert(
        "Jetstream2 has public addresses again",
        f"Jetstream2 handed out {ip}, a routable address. Creates and unshelves get "
        f"addresses again. The shortage was first seen {began}.",
    ):
        clear_alert(state, "mint-failed")
        clear_alert(state, "mint-probe-at")


def mint(state: dict, counts: dict) -> None:
    """One routable address ahead; a drought is the admins' business, once."""
    ip, fid, error = request_address()
    if error:
        log(f"mint failed: {error}")
        alert_once(
            state,
            "mint-failed",
            "Floating-IP mint failed on Jetstream2",
            drought_body(f"openstack floating ip create failed: {error}", counts),
        )
        return
    if not fid:
        alert_once(
            state,
            "mint-failed",
            "Floating-IP mint failed on Jetstream2 (non-routable address)",
            drought_body(
                f"Jetstream2 handed out {ip}, which does not route; it was deleted. Its "
                "public pool is dry.",
                counts,
            ),
        )
        return
    log(f"minted {ip} (one in hand)")
    all_clear(state, ip)


def probe(state: dict, now: float) -> None:
    """While a drought is open and no mint is due, ask Jetstream2 for one
    address once an hour and hand it straight back: the only way to learn the
    drought is over while shelves keep the spares up."""
    if "mint-failed" not in state or now - state.get("mint-probe-at", 0) < PROBE_EVERY:
        return
    state["mint-probe-at"] = now
    ip, fid, error = request_address()
    if error or not fid:
        log(f"probe: Jetstream2 still has no public address ({error or ip})")
        return
    os_run("floating", "ip", "delete", fid)
    log(f"probe: Jetstream2 handed out {ip}; returned it (not needed)")
    all_clear(state, ip)


def settle_surplus(actions: list, state: dict, now: float) -> list:
    """An over-the-hold delete waits for the next tick: the surplus must have
    been seen SURPLUS_SETTLE ago and still be there. A create that picked a
    spare and minted its replacement shows a surplus for a minute or two,
    until it attaches the picked one."""
    surplus = [a for a in actions if a[0] == "delete" and a[3] == "over the hold"]
    if not surplus:
        state.pop("surplus-since", None)
        return actions
    since = state.setdefault("surplus-since", now)
    if now - since >= SURPLUS_SETTLE:
        return actions
    for a in surplus:
        log(f"{a[2]} is over the hold; deleted next tick if still so")
    return [a for a in actions if a not in surplus]


def run(dry_run: bool) -> int:
    # One run at a time: a slow one must not overlap the next tick's.
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is still going; skipping this tick")
        return 0
    fips, by_fixed, starting = inventory()
    state = load_state()
    now = time.time()
    cache: dict[str, float] = {}

    def updated_at(f: dict) -> float:
        fid = str(f["ID"])
        if fid not in cache:
            cache[fid] = epoch(os_json("floating", "ip", "show", fid).get("updated_at"))
        return cache[fid]

    actions, counts = plan(fips, by_fixed, HOLD, now, updated_at, len(starting))
    clear_alert(state, "crashed")  # a run that planned is a run that works again
    actions = settle_surplus(actions, state, now)
    log(
        f"instances: held={counts['held']} running={counts['running']} "
        f"starting={counts['starting']} spares={counts['spares']} "
        f"(infra={counts['infra']} unknown={counts['unknown']}) "
        f"target={counts['target']} (hold {counts['hold']}) actions={len(actions)}"
        + (f" starting: {', '.join(starting)}" if starting else "")
    )
    seen = {a[1] for a in actions if a[0] == "alert"}
    for a in actions:
        apply(a, state, dry_run, counts)
    for key in [k for k in state if k.startswith("nonroutable:") and k not in seen]:
        clear_alert(state, key)  # gone: the next one alerts again
    if not dry_run:
        if not any(a[0] == "mint" for a in actions):
            probe(state, now)
        save_state(state)
    return 0


def apply(action: tuple, state: dict, dry_run: bool, counts: dict) -> None:
    """One planned action, done or (dry run) described."""
    kind = action[0]
    if kind == "unset":
        log(
            f"{'would release' if dry_run else 'releasing'} {action[2]} from a {action[3]} server ({action[4]})"
        )
        if not dry_run:
            os_run("floating", "ip", "unset", "--port", action[1])
            note_issue(
                action[4],
                f"The public address {action[2]} was released from your shelved instance: an address"
                " never stays on a shelved server. The next unshelve attaches a fresh one.",
            )
    elif kind == "delete":
        log(f"{'would delete' if dry_run else 'deleting'} {action[2]} ({action[3]})")
        if not dry_run:
            os_run("floating", "ip", "delete", action[1])
    elif kind == "mint":
        log("below the rule: " + ("would mint one" if dry_run else "minting one"))
        if not dry_run:
            mint(state, counts)
    elif kind == "alert":
        if dry_run:
            log(f"would alert: {action[2]}")
        else:
            alert_once(state, action[1], action[2], action[3])


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
    except Exception as e:
        log(f"run crashed: {type(e).__name__}: {e}")
        traceback.print_exc()
        if "--dry-run" in argv:
            return 1  # a dry run changes nothing, including the alert state
        state = load_state()
        alert_once(
            state,
            "crashed",
            "Floating-IP policing is crashing on the runner",
            f"police-floating-ips.py raised {type(e).__name__}: {e}\n\nEvery tick fails the same way "
            "until the script is fixed; no address is minted, released or deleted meanwhile. The "
            "traceback is in ~/fip-police.log on the runner.",
        )
        save_state(state)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
