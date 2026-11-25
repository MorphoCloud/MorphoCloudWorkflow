#!/usr/bin/env python3
"""Security groups for production (NETWORK_SECURITY_PLAN.md).

Runs on runner-1 (BIO180006), never inside a workflow:

    network-security.py groups            # dry run: prints what --apply would change
    network-security.py groups --apply    # creates/updates mc-instance, mc-runner, mc-join
    network-security.py proxy-check       # hourly from the crontab

`groups` is declarative for its three groups only: it creates each group, adds
the rules in GROUPS and removes every other rule in them. It never touches any
other group (exosphere, default, mc-portal, the k8s groups).

The web-desktop port (49528) of mc-instance is open only to Exosphere's proxy.
A security-group rule can hold only addresses, so `proxy-check` resolves the
proxy's name every hour and makes the 49528 rules follow it: a new address is
added at once, an address gone from the answer is removed after it has been
missing for MISSING_RUNS runs, and every change is emailed to the admins
(maintainer, 2026-11-25). An answer outside Jetstream2's range, more than
MAX_ADDRESSES addresses, no answer, or a failed OpenStack call changes nothing;
such a problem is emailed once it has lasted FAIL_RUNS runs, then at most once a
day. `groups` takes the proxy's addresses the same way, and both take the same
lock, so they never undo each other.

Output goes to stdout (the crontab appends it to ~/netsec/proxy-check.log). A
proxy-check run that changes nothing and has no problem prints nothing.

The parts that decide are plain functions, tested in test_network_security.py.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("MC_NETSEC_HOME", "~/netsec")).expanduser()
LOCK = HOME / "lock"
STATE = HOME / "proxy-check.json"
MAIL_FAILURES = HOME / "mail-failures.log"
INSTANCES = "MorphoCloud/Instances"
TOKEN_FILE = Path(
    os.environ.get(
        "MORPHOCLOUD_DISPATCH_TOKEN_FILE", "~/.config/morphocloud/dispatch.pat"
    )
).expanduser()

PROXY_HOST = "proxy-js2-iu.exosphere.app"
PROXY_PORT = 49528
INSTANCE_GROUP = "mc-instance"
ALLOWED = ipaddress.ip_network("149.165.0.0/16")  # Jetstream2's public range
MAX_ADDRESSES = 4
MISSING_RUNS = 3
FAIL_RUNS = 3
REALERT_SECONDS = 24 * 3600
ANY4 = "0.0.0.0/0"
ANY6 = "::/0"


@dataclass(frozen=True, order=True)
class Rule:
    """One security-group rule, in the form OpenStack reports it."""

    direction: str  # ingress | egress
    ethertype: str  # IPv4 | IPv6
    protocol: str | None  # tcp | udp | icmp | None (any)
    port_min: int | None
    port_max: int | None
    remote: str  # CIDR

    def describe(self) -> str:
        ports = (
            ""
            if self.port_min is None
            else f" {self.port_min}"
            + ("" if self.port_max == self.port_min else f"-{self.port_max}")
        )
        return f"{self.direction} {self.ethertype} {self.protocol or 'any'}{ports} from/to {self.remote}"


def tcp_in(port: int, remote: str = ANY4) -> Rule:
    return Rule("ingress", "IPv4", "tcp", port, port, remote)


ICMP_IN = Rule("ingress", "IPv4", "icmp", None, None, ANY4)
EGRESS4 = Rule("egress", "IPv4", None, None, None, ANY4)

# The tables of NETWORK_SECURITY_PLAN.md. mc-instance's 49528 rules are added
# from the proxy's resolved addresses (proxy_rules), never from a fixed value.
GROUPS: dict[str, tuple[str, list[Rule]]] = {
    "mc-instance": (
        "MorphoCloud member instances: SSH, TurboVNC; web desktop only from Exosphere's proxy (NETWORK_SECURITY_PLAN.md)",
        [tcp_in(22), tcp_in(5901), ICMP_IN, EGRESS4],
    ),
    "mc-runner": (
        "MorphoCloud runners: SSH only (NETWORK_SECURITY_PLAN.md)",
        [tcp_in(22), ICMP_IN, EGRESS4],
    ),
    "mc-join": (
        "MorphoCloud join site: SSH, HTTP, HTTPS (NETWORK_SECURITY_PLAN.md)",
        [tcp_in(22), tcp_in(80), tcp_in(443), ICMP_IN, EGRESS4],
    ),
}


def proxy_rules(addresses: set[str]) -> list[Rule]:
    return [tcp_in(PROXY_PORT, f"{a}/32") for a in sorted(addresses)]


# --- the parts that decide -----------------------------------------------------


def parse_rule(r: dict) -> tuple[str, Rule]:
    """(rule id, Rule) from one row of `openstack security group rule list -f json`."""
    ports = r.get("Port Range") or ""
    pmin = pmax = None
    if ports:
        a, _, b = ports.partition(":")
        pmin, pmax = int(a), int(b or a)
    ether = r.get("Ethertype") or "IPv4"
    # A rule whose source is another security group (or an address group) has
    # no IP range; it must never compare equal to an "anywhere" rule.
    other = r.get("Remote Security Group") or r.get("Remote Address Group")
    if other:
        remote = f"group:{other}"
    else:
        remote = r.get("IP Range") or (ANY6 if ether == "IPv6" else ANY4)
    proto = r.get("IP Protocol") or None
    return r["ID"], Rule(r["Direction"], ether, proto, pmin, pmax, remote)


def plan_group(
    current: list[tuple[str, Rule]], want: list[Rule]
) -> tuple[list[Rule], list[tuple[str, Rule]]]:
    """Rules to add and (id, rule) pairs to delete so the group holds exactly
    `want`. A duplicate of a wanted rule is deleted too."""
    have: dict[Rule, str] = {}
    delete: list[tuple[str, Rule]] = []
    for rid, rule in current:
        if rule in want and rule not in have:
            have[rule] = rid
        else:
            delete.append((rid, rule))
    add = [r for r in want if r not in have]
    return add, delete


def validate(addresses: set[str]) -> str | None:
    """None when the proxy's answer can be used, else the reason it cannot."""
    if not addresses:
        return f"{PROXY_HOST} resolved to no IPv4 address"
    if len(addresses) > MAX_ADDRESSES:
        return f"{PROXY_HOST} resolved to {len(addresses)} addresses (more than {MAX_ADDRESSES}): {sorted(addresses)}"
    outside = sorted(a for a in addresses if ipaddress.ip_address(a) not in ALLOWED)
    if outside:
        return f"{PROXY_HOST} resolved to addresses outside {ALLOWED}: {outside}"
    return None


@dataclass
class State:
    """~/netsec/proxy-check.json. Missing or unreadable counts as empty."""

    missing: dict[str, int] = field(default_factory=dict)  # address -> runs missing
    failures: int = 0  # consecutive runs with a problem
    last_alert: float = 0.0  # when the last problem email went out

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            d = json.loads(path.read_text())
            return cls(
                {str(k): int(v) for k, v in dict(d.get("missing", {})).items()},
                int(d.get("failures", 0)),
                float(d.get("last_alert", 0.0)),
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, sort_keys=True))
        tmp.replace(path)


def follow(
    resolved: set[str], current: set[str], state: State
) -> tuple[set[str], set[str], State]:
    """Addresses to add and to remove so the 49528 rules follow the proxy.
    New addresses are added at once; one missing from the answer is removed
    only after MISSING_RUNS consecutive runs without it."""
    add = resolved - current
    missing = dict(state.missing)
    remove: set[str] = set()
    for a in current - resolved:
        missing[a] = missing.get(a, 0) + 1
        if missing[a] >= MISSING_RUNS:
            remove.add(a)
    for a in list(missing):
        if a in resolved or a in remove or a not in current:
            missing.pop(a)
    # A good run ends any problem episode: the next one alerts at its own
    # FAIL_RUNS-th run, not a day after the last alert.
    return add, remove, State(missing, 0, 0.0)


def problem_alert_due(state: State, now: float) -> bool:
    """After a run with a problem (state.failures already counted): email at
    the FAIL_RUNS-th consecutive run, then at most once a day."""
    if state.failures < FAIL_RUNS:
        return False
    return now - state.last_alert >= REALERT_SECONDS


# --- talking to the world -----------------------------------------------------


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)  # noqa: T201


def sh(*args: str, timeout: int = 120) -> str:
    r = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if r.returncode != 0:
        msg = f"{' '.join(args[:5])}: exit {r.returncode}: {r.stderr.strip()[-300:]}"
        raise RuntimeError(msg)
    return r.stdout


def os_json(*args: str):
    return json.loads(sh("openstack", *args, "-f", "json") or "null")


def resolve(host: str = PROXY_HOST) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return set()
    return {i[4][0] for i in infos}


def group_id(name: str) -> str | None:
    for g in os_json("security", "group", "list"):
        if g["Name"] == name:
            return g["ID"]
    return None


def group_rules(gid: str) -> list[tuple[str, Rule]]:
    return [parse_rule(r) for r in os_json("security", "group", "rule", "list", gid)]


def create_rule(gid: str, rule: Rule) -> None:
    args = [
        "openstack",
        "security",
        "group",
        "rule",
        "create",
        gid,
        f"--{rule.direction}",
        "--ethertype",
        rule.ethertype,
        "--protocol",
        rule.protocol or "any",
        "--remote-ip",
        rule.remote,
    ]
    if rule.port_min is not None:
        args += ["--dst-port", f"{rule.port_min}:{rule.port_max}"]
    sh(*args)


def delete_rule(rid: str) -> None:
    sh("openstack", "security", "group", "rule", "delete", rid)


def email(subject: str, body: str) -> bool:
    """One email to the admins through admin-alert.yml (the runner cannot send
    mail). A failure is logged in ~/netsec/mail-failures.log."""
    try:
        token = TOKEN_FILE.read_text().strip()
        r = subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                "admin-alert.yml",
                "-R",
                INSTANCES,
                "--ref",
                "main",
                "-f",
                f"subject={subject}",
                "-f",
                f"body={body}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "GH_TOKEN": token},
        )
        # The uniform runner-side line (scripts/check-dispatch-logging.py).
        result = "ok" if r.returncode == 0 else f"FAILED {r.stderr.strip()[-200:]}"
        log(
            f"DISPATCH admin-alert.yml repo={INSTANCES} issue=- by=network-security.py result={result}"
        )
        if r.returncode == 0:
            return True
        why = r.stderr.strip()[-300:]
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
    HOME.mkdir(parents=True, exist_ok=True)
    with MAIL_FAILURES.open("a") as f:
        f.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {subject}: {why}\n"
        )
    log(f"email failed: {why}")
    return False


@contextlib.contextmanager
def locked(wait: bool):
    """The lock `groups` and `proxy-check` share. proxy-check skips its run
    when the lock is held; groups waits up to 10 minutes."""
    HOME.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as f:
        deadline = time.time() + (600 if wait else 0)
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    yield False
                    return
                time.sleep(2)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# --- commands -----------------------------------------------------------------


def say(msg: str) -> None:
    print(msg, flush=True)  # noqa: T201


def sync_group(name: str, apply: bool, resolved: set[str], problem: str | None) -> int:
    """Print, and with apply make, the changes that bring one group to the
    plan. Returns the number of changes."""
    desc, rules = GROUPS[name]
    gid = group_id(name)
    if gid is None:
        say(f"[{name}] does not exist: {'creating' if apply else 'would create'}")
        if apply:
            gid = os_json("security", "group", "create", name, "--description", desc)[
                "id"
            ]
    current = group_rules(gid) if gid else []
    want = list(rules)
    if name == INSTANCE_GROUP:
        if problem:
            # Keep whatever 49528 rules exist: a bad answer never opens or
            # closes the web desktop.
            say(f"[{name}] proxy answer not used ({problem}); its 49528 rules stay")
            want += [
                r
                for _, r in current
                if r.protocol == "tcp" and r.port_min == PROXY_PORT
            ]
        else:
            want += proxy_rules(resolved)
    add, delete = plan_group(current, want)
    verb = ("add", "remove") if apply else ("would add", "would remove")
    for r in add:
        say(f"[{name}] {verb[0]}: {r.describe()}")
    for _, r in delete:
        say(f"[{name}] {verb[1]}: {r.describe()}")
    if not add and not delete:
        say(f"[{name}] matches the plan")
    if apply and gid:
        for r in add:  # add first, so nothing wanted is ever missing
            create_rule(gid, r)
        for rid, _ in delete:
            delete_rule(rid)
    return len(add) + len(delete)


def cmd_groups(apply: bool) -> int:
    with locked(wait=True) as ok:
        if not ok:
            log("another network-security.py run holds the lock; try again")
            return 1
        resolved = resolve()
        problem = validate(resolved)
        changes = sum(sync_group(n, apply, resolved, problem) for n in GROUPS)
        if not apply and changes:
            say("dry run: nothing changed; run with --apply to make these changes")
        return 0


def proxy_rules_now(
    rules: list[tuple[str, Rule]],
) -> tuple[dict[str, str], list[tuple[str, Rule]]]:
    """Split mc-instance's port-49528 ingress rules into the proxy's own (one
    IPv4 /32 rule per address: address -> rule id) and strays (duplicates, a
    wider range, IPv6, a remote group), which only the proxy may not need."""
    mine: dict[str, str] = {}
    strays: list[tuple[str, Rule]] = []
    for rid, r in rules:
        # Any ingress TCP (or any-protocol) rule whose range includes 49528, or
        # that has no port limit at all; ICMP never does.
        covers = r.protocol in ("tcp", None) and (
            r.port_min is None or r.port_min <= PROXY_PORT <= (r.port_max or r.port_min)
        )
        if r.direction != "ingress" or not covers:
            continue
        addr = r.remote.removesuffix("/32")
        proxy_form = (
            r.protocol == "tcp"
            and r.port_max == PROXY_PORT
            and r.ethertype == "IPv4"
            and r.remote.endswith("/32")
            and addr not in mine
        )
        if proxy_form:
            mine[addr] = rid
        else:
            strays.append((rid, r))
    return mine, strays


def cmd_proxy_check() -> int:
    with locked(wait=False) as ok:
        if not ok:
            return 0  # a groups run is writing; the next hour checks again
        prev = State.load(STATE)
        new: State | None = None  # follow()'s result, once it has run
        now = time.time()
        done: list[str] = []  # every rule change made this run, for the email
        resolved: set[str] = set()
        problem: str | None = None
        try:
            gid = group_id(INSTANCE_GROUP)
            if gid is None:
                return 0  # before rollout step 3: nothing to follow
            resolved = resolve()
            problem = validate(resolved)
            if problem is None:
                current, strays = proxy_rules_now(group_rules(gid))
                add, remove, new = follow(resolved, set(current), prev)
                for a in sorted(add):  # add first: no moment without the proxy
                    create_rule(gid, tcp_in(PROXY_PORT, f"{a}/32"))
                    done.append(f"added {a}")
                for a in sorted(remove):
                    delete_rule(current[a])
                    done.append(
                        f"removed {a} (missing from {PROXY_HOST} for {MISSING_RUNS} runs)"
                    )
                for rid, r in strays:  # only the proxy may reach 49528
                    delete_rule(rid)
                    done.append(
                        f"removed a rule that is not the proxy's: {r.describe()}"
                    )
        except Exception as e:
            problem = f"{type(e).__name__}: {e}"
        if done:
            # Sent even when a later call failed, so no change goes unreported.
            msg = (
                f"{PROXY_HOST} resolves to {sorted(resolved)}; port {PROXY_PORT} rules in {INSTANCE_GROUP}: "
                + "; ".join(done)
            )
            log(msg)
            email(
                "MorphoCloud: Exosphere's proxy address changed; the web-desktop rule was updated",
                msg
                + "\n\nNothing to do: the rule was updated automatically (NETWORK_SECURITY_PLAN.md)."
                + (f"\n\nThe run then failed: {problem}" if problem else ""),
            )
        if problem is None and new is not None:
            new.save(STATE)  # a good run: counters reset (follow)
            return 0
        if problem is None:
            problem = "the check ended without a result"
        # A problem run: count it on the loaded state, never on follow()'s
        # reset one, so a call that fails every hour reaches the alert.
        state = State((new or prev).missing, prev.failures + 1, prev.last_alert)
        log(
            f"problem (run {state.failures} in a row){'' if done else ', nothing changed'}: {problem}"
        )
        if problem_alert_due(state, now) and email(
            "MorphoCloud: the hourly check of Exosphere's proxy address cannot update the rule",
            f"{problem}\n\n{state.failures} consecutive hourly runs on runner-1 found this. "
            f"Log: ~/netsec/proxy-check.log (NETWORK_SECURITY_PLAN.md).",
        ):
            state.last_alert = now
        state.save(STATE)
        return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("groups", help="create/update mc-instance, mc-runner, mc-join")
    g.add_argument(
        "--apply", action="store_true", help="make the changes (default: dry run)"
    )
    sub.add_parser(
        "proxy-check", help="make mc-instance's 49528 rules follow the proxy"
    )
    a = ap.parse_args(argv)
    if a.cmd == "groups":
        return cmd_groups(a.apply)
    return cmd_proxy_check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
