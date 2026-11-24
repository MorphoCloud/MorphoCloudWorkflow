#!/usr/bin/env python3
"""Monthly golden-image bake (STORAGE_REDESIGN_PLAN.md, "Monthly bake",
decision 22).

Runs on runner-1 (the acquire runner) from its crontab, outside GitHub
Actions:

    0 8 * * 2   bake-monthly.py            # goes on only on the first Tuesday
    0 9 * * 1   bake-monthly.py --overdue  # emails when no run succeeded in 36 days

By hand: `bake-monthly.py --now` (skips the first-Tuesday rule).
`touch ~/bake/STOP` ends a run at its next step.

One run: clone MWF main once (its bake-image.sh, check-image.sh and
cloud-config serve the whole run), bake the vGPU and regular images with the
2026-11-23 retry loop, check each with one create, set their Glance
properties, prune old images, and email the admins through admin-alert.yml
whatever happens. The image variables are switched by hand (maintainer,
2026-11-24): the email gives the command.

The parts that decide are plain functions, tested in test_bake_monthly.py.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

HOME = Path(os.environ.get("MC_BAKE_HOME", "~/bake")).expanduser()
LOCK = HOME / "lock"
STOP = HOME / "STOP"
LAST_SUCCESS = HOME / "last-success"
MAIL_FAILURES = HOME / "mail-failures.log"
MWF_URL = "https://github.com/MorphoCloud/MorphoCloudWorkflow.git"
INSTANCES = "MorphoCloud/Instances"
TOKEN_FILE = Path(
    os.environ.get(
        "MORPHOCLOUD_DISPATCH_TOKEN_FILE", "~/.config/morphocloud/dispatch.pat"
    )
).expanduser()

KINDS = ("vgpu", "regular")
# First choice, then the fallback, in turn (maintainer, 2026-11-24: g3.medium
# first, it is the easiest vGPU flavor to get). The regular image needs a
# passthrough GPU for its driver.
FLAVORS = {"vgpu": ("g3.medium", "g3.large"), "regular": ("g3.xl", "g4.xl")}
VARIABLE = {"vgpu": "MORPHOCLOUD_IMAGE_VGPU", "regular": "MORPHOCLOUD_IMAGE_REGULAR"}
IMAGE_RE = re.compile(r"^morphocloud-share-(vgpu|regular)-(\d{8})$")
LEAK_RE = re.compile(r"^mc-bake(check)?-\d{8}-\d{6}$")

# A bake that fails for lack of a host is not counted: Jetstream2's GPU
# shortage must not fail a month's bake (maintainer, 2026-11-24). Only real
# failures count, and the run keeps trying for up to RUN_LIMIT.
MAX_TRIES = 9  # real bake failures per image
NO_HOST_RE = re.compile(r"Exhausted all hosts|No valid host")
ROUND_PAUSE = 20 * 60
RUN_LIMIT = 7 * 86400  # first Tuesdays are at least 28 days apart
# First Tuesdays are at most 35 days apart, and a run can wait up to 7 days
# for hosts before it succeeds: 35 + 7 + 1.
OVERDUE_DAYS = 43
LEAK_AGE = 2 * 86400
NO_HOST = 3  # check-image.sh: Jetstream2 gave no host, nothing was checked
CHECK_FAILURES = 2  # a check that fails twice is the image's fault


# --- the parts that decide ---------------------------------------------------


def first_tuesday(day: dt.date) -> bool:
    """The first Tuesday of its month. Cron cannot say it: a line with both a
    day of the month and a weekday fires on either."""
    return day.weekday() == 1 and day.day <= 7  # noqa: PLR2004 — a week


def overdue(last_success: float | None, now: float) -> bool:
    """No successful run for more than OVERDUE_DAYS (or never)."""
    return last_success is None or now - last_success > OVERDUE_DAYS * 86400


def image_name(kind: str, day: dt.date) -> str:
    return f"morphocloud-share-{kind}-{day:%Y%m%d}"


def flavor_for(kind: str, attempts_done: int) -> str:
    """The flavors of a kind in turn, one per attempt."""
    pair = FLAVORS[kind]
    return pair[attempts_done % len(pair)]


def no_host(evidence: str) -> bool:
    """A bake that failed because Jetstream2 had no host for the flavor (the
    build instance's fault message), not because of the bake."""
    return bool(NO_HOST_RE.search(evidence))


def pid_alive(pid: int) -> bool:
    """The process exists and is a bake-monthly run (a dead run's PID can be
    reused by another program)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return b"bake-monthly" in cmdline.read_bytes()
        except OSError:
            return True
    return True


def lock_state(lock: Path, alive=None) -> tuple[str, dict]:
    """("free", {}), ("alive", info) or ("dead", info): a lock whose process
    is gone was left by a run that died."""
    try:
        info = json.loads(lock.read_text())
        pid = int(info["pid"])
    except FileNotFoundError:
        return "free", {}
    except (ValueError, KeyError, TypeError):
        return "dead", {"pid": None, "started": None}
    alive = alive or pid_alive
    return ("alive" if alive(pid) else "dead"), info


def prune_choice(
    images: list[str], in_use: dict[str, str | None], used_by_servers: set[str]
) -> list[str]:
    """Images to delete. For each kind, the image its variable names and the
    one before it stay, so the variable can go back; images newer than the one
    in use (baked, not promoted) stay. An older one is deleted only when no
    server uses it (an unshelve boots from the instance's own image). A kind
    whose variable is unknown or names no image of ours keeps everything."""
    out = []
    for kind in KINDS:
        current = in_use.get(kind)
        mine = sorted(n for n in images if (m := IMAGE_RE.match(n)) and m[1] == kind)
        if current not in mine:
            continue
        older = mine[: mine.index(current)]
        out += [n for n in older[:-1] if n not in used_by_servers]
    return out


def leaked(servers: list[dict], now: float) -> list[str]:
    """Build and check instances older than two days: a run that crashed
    before its own cleanup. Only called after the lock check, so none of them
    belongs to a live run."""
    out = []
    for s in servers:
        created = s.get("created")
        if not LEAK_RE.match(s.get("name", "")) or not created:
            continue
        age = (
            now - dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        )
        if age > LEAK_AGE:
            out.append(s["name"])
    return out


def promote_command(names: dict[str, str]) -> str:
    return "\n".join(
        f"gh variable set {VARIABLE[k]} -R {INSTANCES} -b {names[k]}" for k in KINDS
    )


# --- the run -----------------------------------------------------------------


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)  # noqa: T201


def sh(*args: str, timeout: int = 600, check: bool = True) -> str:
    r = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and r.returncode != 0:
        msg = f"{' '.join(args[:4])}: exit {r.returncode}: {r.stderr.strip()[-300:]}"
        raise RuntimeError(msg)
    return r.stdout


CHILD: subprocess.Popen | None = None  # the bake or check running now


def stop_child(p: subprocess.Popen) -> None:
    """SIGTERM to the child's whole process group (bash and its openstack
    calls), so check-image.sh's trap deletes its instance; SIGKILL after 60 s."""
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=60)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)


def run_child(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    global CHILD  # noqa: PLW0603 — the signal handler must reach the child
    p = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    CHILD = p
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_child(p)
        out, err = p.communicate()
        err += f"\nended after {timeout} s"
    finally:
        CHILD = None
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def os_json(*args: str):
    return json.loads(sh("openstack", *args, "-f", "json") or "null")


def email(subject: str, body: str) -> bool:
    """One email to the admins through admin-alert.yml (the runner cannot send
    mail). A failure is logged in ~/bake/mail-failures.log, where the
    --overdue email points."""
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
            f"DISPATCH admin-alert.yml repo={INSTANCES} issue=- by=bake-monthly.py result={result}"
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


class Run:
    def __init__(self, day: dt.date, workdir: Path):
        self.day = day
        self.dir = workdir
        self.names = {k: image_name(k, day) for k in KINDS}
        self.attempts = dict.fromkeys(KINDS, 0)  # bakes and checks started
        self.tries = dict.fromkeys(KINDS, 0)  # real bake failures
        self.check_failures = dict.fromkeys(KINDS, 0)
        self.started = time.time()
        self.state = dict.fromkeys(KINDS, "bake")  # bake -> check -> done | failed
        self.notes: list[str] = []
        self.mwf_sha = self.exo_sha = "?"

    def note(self, msg: str) -> None:
        log(msg)
        self.notes.append(f"{time.strftime('%H:%M', time.gmtime())} {msg}")

    def clone(self) -> Path:
        src = self.dir / "mwf"
        if src.exists():
            shutil.rmtree(src)
        sh("git", "clone", "--quiet", "--depth", "1", MWF_URL, str(src), timeout=300)
        self.mwf_sha = sh("git", "-C", str(src), "rev-parse", "HEAD").strip()
        m = re.search(
            r'^exosphere_sha="([0-9a-f]{40})"', (src / "cloud-config").read_text(), re.M
        )
        self.exo_sha = m[1] if m else "?"
        self.note(f"MWF main {self.mwf_sha[:7]}, exosphere {self.exo_sha[:8]}")
        return src

    def image_status(self, name: str) -> str | None:
        """None when there is no such image, else its Glance status."""
        rows = os_json("image", "list", "--name", name, "--long")
        return (rows[0].get("Status") or "?").lower() if rows else None

    def image_exists(self, name: str) -> bool:
        return self.image_status(name) == "active"

    def evidence(self, out: str, tag: str) -> str:
        """A failed bake keeps its build instance: save what Nova knows, then
        delete it. Returns what was saved."""
        m = re.findall(r"^Build instance kept for diagnosis: (\S+)$", out, re.M)
        if not m:
            return ""
        name = m[-1]
        path = self.dir / f"evidence-{name}.txt"
        parts = []
        for args in (
            ("server", "show", name, "-f", "json"),
            ("server", "event", "list", name, "-f", "json"),
            ("console", "log", "show", "--lines", "200", name),
        ):
            r = subprocess.run(
                ["openstack", *args],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            parts.append(f"== {' '.join(args)}\n{r.stdout}{r.stderr}")
        text = "\n".join(parts)
        path.write_text(text)
        sh("openstack", "server", "delete", "--wait", name, timeout=600, check=False)
        self.note(f"{tag}: evidence in {path.name}; {name} deleted")
        return text

    def bake(self, kind: str, src: Path) -> None:
        flavor = flavor_for(kind, self.attempts[kind])
        self.attempts[kind] += 1
        name = self.names[kind]
        tag = f"bake {name} on {flavor} (attempt {self.attempts[kind]})"
        if self.image_status(name) not in (None, "active"):
            # A bake that died after Glance created the image leaves it
            # queued, saving or killed: never checked, never promoted.
            sh("openstack", "image", "delete", name, check=False)
            self.note(f"deleted unfinished image {name}")
        r = run_child(
            [
                "bash",
                str(src / "scripts/bake-image.sh"),
                flavor,
                name,
                str(src / "cloud-config"),
            ],
            timeout=3 * 3600,
        )
        (self.dir / f"bake-{kind}-{flavor}-{self.attempts[kind]}.log").write_text(
            r.stdout + r.stderr
        )
        if r.returncode == 0 and self.image_exists(name):
            sh(
                "openstack",
                "image",
                "set",
                "--property",
                f"mc_mwf_sha={self.mwf_sha}",
                "--property",
                f"mc_exosphere_sha={self.exo_sha}",
                name,
            )
            self.note(f"{tag}: done")
            self.state[kind] = "check"
            return
        why = next(
            (x for x in r.stderr.splitlines() if x.startswith("BAKE FAILED")),
            f"exit {r.returncode}",
        )
        saved = self.evidence(r.stdout + r.stderr, tag)
        if no_host(saved + r.stdout + r.stderr):
            self.note(f"{tag}: no host for {flavor}; not counted, will try again")
        else:
            self.tries[kind] += 1
            self.note(f"{tag}: {why} (failure {self.tries[kind]}/{MAX_TRIES})")

    def check(self, kind: str, src: Path) -> None:
        flavor = flavor_for(kind, self.attempts[kind])
        self.attempts[kind] += 1
        name = self.names[kind]
        tag = f"check {name} on {flavor} (attempt {self.attempts[kind]})"
        r = run_child(
            [
                "bash",
                str(src / "scripts/check-image.sh"),
                flavor,
                name,
                str(src / "cloud-config"),
            ],
            timeout=2 * 3600,
        )
        (self.dir / f"check-{kind}-{flavor}-{self.attempts[kind]}.log").write_text(
            r.stdout + r.stderr
        )
        if r.returncode == 0:
            self.note(f"{tag}: passed")
            self.state[kind] = "done"
        elif r.returncode == NO_HOST:
            self.note(f"{tag}: no host for {flavor}; not counted, will try again")
        else:
            why = next(
                (
                    x
                    for x in reversed(r.stderr.splitlines())
                    if x.startswith("CHECK FAILED")
                ),
                f"exit {r.returncode}",
            )
            self.check_failures[kind] += 1
            if self.check_failures[kind] >= CHECK_FAILURES:
                self.note(f"{tag}: {why}")
                self.state[kind] = "failed"
            else:
                # Once more: an SSH or setup timeout on a slow host is not
                # always the image's fault. Twice is.
                self.note(f"{tag}: {why}; will check once more")

    def loop(self, src: Path) -> None:
        end = time.time() + RUN_LIMIT
        while True:
            for kind in KINDS:
                if STOP.exists():
                    self.note("STOP file found; ending the run")
                    return
                if self.state[kind] in ("done", "failed"):
                    continue
                if self.tries[kind] >= MAX_TRIES:
                    self.note(f"{self.names[kind]}: {MAX_TRIES} real bake failures")
                    self.state[kind] = "failed"
                    continue
                if self.state[kind] == "bake":
                    self.bake(kind, src)
                if self.state[kind] == "check":
                    self.check(kind, src)
            if all(s in ("done", "failed") for s in self.state.values()):
                return
            if time.time() + ROUND_PAUSE > end:
                self.note("7 days used")
                return
            time.sleep(ROUND_PAUSE)

    def prune(self) -> list[str]:
        """Old images, by prune_choice. The variables are read now; if they
        cannot be read, nothing is deleted."""
        in_use = {}
        for kind in KINDS:
            r = subprocess.run(
                ["gh", "variable", "get", VARIABLE[kind], "-R", INSTANCES],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env={**os.environ, "GH_TOKEN": TOKEN_FILE.read_text().strip()},
            )
            if r.returncode != 0:
                self.note(
                    f"pruning skipped: cannot read {VARIABLE[kind]} ({r.stderr.strip()[-120:]})"
                )
                return []
            in_use[kind] = r.stdout.strip()
        images = {
            i["Name"]: i["ID"]
            for i in os_json("image", "list", "--private")
            if IMAGE_RE.match(i["Name"])
        }
        used = set()
        for name, iid in images.items():
            if sh(
                "openstack", "server", "list", "--image", iid, "-f", "value", "-c", "ID"
            ).strip():
                used.add(name)
        gone = prune_choice(list(images), in_use, used)
        for name in gone:
            sh("openstack", "image", "delete", images[name], check=False)
            self.note(f"deleted old image {name}")
        return gone

    def inventory(self) -> str:
        sizes = []
        for i in os_json("image", "list", "--private", "--long"):
            if IMAGE_RE.match(i.get("Name", "")):
                sizes.append((i["Name"], int(i.get("Size") or 0)))
        total = sum(s for _, s in sizes) / 1e9
        return f"{len(sizes)} golden images in Glance, {total:,.0f} GB in all"


def bake_servers() -> list[dict]:
    """Build and check instances with their creation time; one that vanishes
    between the list and the show is skipped."""
    out = []
    for s in os_json("server", "list", "--name", "^mc-bake"):
        try:
            info = os_json("server", "show", s["ID"])
        except RuntimeError:
            continue
        out.append({"name": s["Name"], "created": info.get("created")})
    return out


def sweep_leaks(run: Run) -> None:
    """Best effort: a failure here is noted and never stops the run."""
    try:
        for name in leaked(bake_servers(), time.time()):
            sh(
                "openstack",
                "server",
                "delete",
                "--wait",
                name,
                timeout=600,
                check=False,
            )
            run.note(f"deleted {name}, left by a crashed run")
    except Exception as e:
        run.note(f"leak sweep skipped: {type(e).__name__}: {str(e)[:160]}")


def delete_own_leftovers(run: Run) -> None:
    """After a crash or a signal: this run's build and check instances
    (created since it started; only one run at a time, by the lock)."""
    try:
        for s in bake_servers():
            created = s.get("created")
            if (
                created
                and LEAK_RE.match(s["name"])
                and dt.datetime.fromisoformat(
                    created.replace("Z", "+00:00")
                ).timestamp()
                >= run.started - 60
            ):
                sh(
                    "openstack",
                    "server",
                    "delete",
                    "--wait",
                    s["name"],
                    timeout=600,
                    check=False,
                )
                run.note(f"deleted {s['name']}, left by this run")
    except Exception as e:
        run.note(f"could not clean up this run's instances: {type(e).__name__}")


def summary(run: Run, ok: bool, crash: str | None) -> tuple[str, str]:
    names = run.names
    if ok:
        subject = f"golden images ready: {names['vgpu']}, {names['regular']}"
    elif crash:
        subject = "golden-image bake crashed"
    else:
        subject = "golden-image bake did not finish: " + ", ".join(
            f"{k} {run.state[k]}" for k in KINDS
        )
    lines = [
        f"Run of {run.day} on {os.uname().nodename}. MWF main {run.mwf_sha[:7]}, exosphere {run.exo_sha[:8]}.",
        "",
        *(
            f"{names[k]}: {run.state[k]} ({run.attempts[k]} attempts, "
            f"{run.tries[k]} real bake failures)"
            for k in KINDS
        ),
        "",
    ]
    if ok:
        lines += [
            "Both images passed their check. To put them in use (by hand, decision 22):",
            "",
            promote_command(names),
            "",
        ]
    if crash:
        lines += ["The run crashed:", crash, ""]
    lines += ["What happened:", *run.notes, "", f"Logs and evidence: {run.dir}"]
    try:
        lines.append(run.inventory())
    except Exception as e:
        lines.append(f"(image inventory unavailable: {type(e).__name__})")
    return subject, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912, PLR0915 — one run, in order, read top to bottom
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--now", action="store_true", help="run today, whatever the day")
    p.add_argument(
        "--overdue", action="store_true", help="only check that a run succeeded lately"
    )
    a = p.parse_args(argv)
    HOME.mkdir(parents=True, exist_ok=True)
    now = time.time()
    today = dt.datetime.now(dt.UTC).date()

    if a.overdue:
        try:
            last = float(LAST_SUCCESS.read_text()) if LAST_SUCCESS.exists() else None
        except ValueError:
            last = None  # unreadable: treated as never, so the email goes out
        if overdue(last, now) and lock_state(LOCK)[0] != "alive":
            # A run still waiting for hosts is not overdue; it emails when it ends.
            when = time.strftime("%Y-%m-%d", time.gmtime(last)) if last else "never"
            failures = (
                MAIL_FAILURES.read_text()[-1500:] if MAIL_FAILURES.exists() else "none"
            )
            email(
                "golden-image bake overdue",
                f"No golden-image bake has succeeded for more than {OVERDUE_DAYS} days "
                f"(last success: {when}). Check runner-1's crontab and ~/bake/.\n\n"
                f"Emails that could not be sent:\n{failures}",
            )
        return 0

    state, info = lock_state(LOCK)
    if state == "alive":
        if a.now or first_tuesday(today):
            email(
                "golden-image bake skipped: a run is still going",
                f"A run started {info.get('started')} (pid {info.get('pid')}) holds {LOCK}.",
            )
        return 0
    if state == "dead":
        email(
            "golden-image bake: a run died",
            f"A run started {info.get('started')} (pid {info.get('pid')}) died without "
            f"clearing {LOCK}. Its build or check instances, if any, are deleted by this run.",
        )
        LOCK.unlink(missing_ok=True)

    if not (a.now or first_tuesday(today)):
        return 0
    if STOP.exists():
        STOP.unlink()  # a STOP left from an earlier run must not end this one

    LOCK.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    )
    run = Run(today, HOME / "runs" / f"{today:%Y%m%d}")
    run.dir.mkdir(parents=True, exist_ok=True)
    ok, crash = False, None

    def on_signal(signum, _frame):
        if CHILD is not None:
            stop_child(CHILD)
        msg = f"signal {signum}"
        raise SystemExit(msg)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    try:
        sweep_leaks(run)
        src = run.clone()
        for kind in KINDS:
            if run.image_exists(run.names[kind]):
                run.note(f"{run.names[kind]} already exists; checking it")
                run.state[kind] = "check"
        run.loop(src)
        ok = all(s == "done" for s in run.state.values())
        if ok:
            LAST_SUCCESS.write_text(str(time.time()))
            run.prune()
    except BaseException as e:
        crash = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        delete_own_leftovers(run)
    finally:
        subject, body = summary(run, ok, crash)
        email(subject, body)
        LOCK.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
