#!/usr/bin/env python3
"""Record every workflow run of a MorphoCloud source repository in the incident store.

INCIDENT_STORE_PLAN.md ("Data collection", decision 20: capture classes, not
lists) and INCIDENT_STORE_PHASE1_DESIGN.md ("PR A, the harvester side"). Runs
from the acquire runner's crontab every five minutes (the runner is the clock,
decision 21) and is the ONLY writer of the store repository.

One cycle, in order:

1. list the runs created since the cursor (15 minutes of overlap, windows of at
   most 6 hours), then the runs still queued or in progress; every unfinished
   run joins the watch list;
2. fetch every watched run until it completes; a new attempt is a new outcome;
3. once an hour, re-list the last 48 hours and record anything missing (a
   rerun that started and finished between cycles, a cycle that crashed);
4. for every command run, every alert run and every run that did not succeed:
   the jobs and the full log, redacted; the error line of the failed step;
   result, signature and actor_kind;
5. an incident directory for every failure, cancellation, dropped command and
   alert run: the log, the failed step's tail, the late OpenStack snapshot
   (through the read-only wrapper), the outside context (Grafana vacancy,
   ACCESS outages, githubstatus) and the issue's timeline;
6. follow-ups: incidents younger than 24 hours get the issue's new timeline
   events and the later outcomes on the same issue appended;
7. every new byte of the runner-side logs, and the error lines of the Actions
   runner's own _diag files;
   (coverage amendment, INCIDENT_STORE_PHASE1_DESIGN.md)
   K. steps 1-4 and 6 for each repository the source lists under
      `extra_repos` (production: the join health monitor);
   I. the system journal of every host the source lists, from a saved cursor,
      through mc-journal-read.py (locally, or as the read-only `mcjournal`
      account's forced command over SSH);
   J. the Nova state of those hosts, a line on every change;
8. one commit and one push, only when something changed. The cursor moves with
   every sweep tick on production, so a store that has not changed for two
   hours means the harvester stopped OR the runner's clock stopped; the store's
   watchdog.yml reads state/harvester.json for both.

Environment (set on the crontab line, rendered by check-workflow-clock.py):
  MORPHOCLOUD_SOURCE_REPO   owner/repo whose runs are recorded
  MORPHOCLOUD_STORE_REPO    owner/repo of the store (informational; the clone
                            at ~/incidents-store is what is written)
  OS_CLOUD                  the clouds.yaml entry for the late snapshot
  MORPHOCLOUD_STORE_DIR     the clone (default ~/incidents-store)
  MORPHOCLOUD_CFG_DIR       where the files below live (default ~/.config/morphocloud)
Files: ~/.config/morphocloud/harvest-source.pat (Actions:read, Issues:read on
the source), harvest-store.pat (used by git through the clone's credential
helper, installed by install-harvester.sh), hmac.key (optional: without it
every email address becomes "[email]"; the record says so), harvest-local.json
(runner-side state: watch list, log offsets, lookback stamp), harvest.lock.
Beside the script: incident-sources.yml, incident-signatures.yml, redact.sed.

Usage:
  harvest-incidents.py                 one cycle (cron)
  harvest-incidents.py --dry-run       read and classify, write nothing
  harvest-incidents.py --backfill 90   record the last 90 days once, by hand,
                                       under the same lock (about two hours)
  harvest-incidents.py --rederive [--dry-run]
                                       apply today's rules (command, kind,
                                       refusal, the day a runner-log line is
                                       filed under) to what is already stored
"""

from __future__ import annotations

import argparse
import calendar
import fcntl
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
RULES_VERSION = 1  # bump with redact.sed
HERE = Path(__file__).resolve().parent
# MORPHOCLOUD_CFG_DIR lets a dry run from a laptop use a scratch directory.
CFG_DIR = Path(
    os.environ.get("MORPHOCLOUD_CFG_DIR", "~/.config/morphocloud")
).expanduser()
SOURCE_PAT = CFG_DIR / "harvest-source.pat"
HMAC_KEY = CFG_DIR / "hmac.key"
LOCAL_STATE = CFG_DIR / "harvest-local.json"
LOCK_FILE = CFG_DIR / "harvest.lock"
API = "https://api.github.com"
OVERLAP = 15 * 60
WINDOW = 6 * 3600
LOOKBACK = 48 * 3600
LOOKBACK_EVERY = 3600
FOLLOWUP_FOR = 24 * 3600
DISPATCH_JOIN = 60
# A run created within this many seconds of the cycle waits for the next one:
# the dispatcher and the harvester share the */5 clock, so the DISPATCH line
# for a run created at :00:02 may not be in the log the harvester read at :00:01.
GRACE = 120
# A day's _diag extract stops growing here (one marker line, then silence until
# the next day): the runner's own ERR lines are ~10 KB/day, so reaching the cap
# means the pattern or the runner has gone wrong, and the store must not pay for it.
DIAG_DAY_CAP = 2_000_000
# How much of each runner log the DISPATCH join reads. Production's dispatch
# log is 8 MB for five months, so 32 MB covers a 90-day backfill with room.
DISPATCH_LOG_TAIL = 32_000_000
# Coverage amendment (class K): an extra repository seen for the first time is read
# from this far back in the cycle itself; older history is a --backfill --repo.
EXTRA_FIRST_SIGHT = 7 * 86400
# Class I: the journal reader on each host (mc-journal-read.py) is given this
# long; an unreachable or slow host is recorded and retried next cycle.
JOURNAL_TIMEOUT = 180
JOURNAL_READER = HERE / "mc-journal-read.py"
UFW_SRC_RE = re.compile(r"\bSRC=(\d+\.\d+\.\d+)\.\d+")
UFW_DPT_RE = re.compile(r"\bDPT=(\d+)")
TOP_N = 10
TAIL_LINES = 200
PAGE = 100
MAX_PAGES = 10  # the API caps a filtered listing at 1,000 runs
PACE_BELOW = 1000  # backfill sleeps when the hourly budget drops under this
RETRIES = 3  # per API call, on 5xx and connection errors
MIN_WINDOW = 600  # a listing window is never split below ten minutes
HTTP_FORBIDDEN = 403
HTTP_SERVER_ERROR = 500
TIME_AND_VALUE = 2  # a Grafana frame has a time column and a value column
# The read-only wrapper next to this interpreter (the runner's venv); cron has
# no PATH. Every OpenStack call goes through it (decision 18).
OPENSTACK_RO = (
    str(Path(sys.executable).with_name("openstack-ro"))
    if Path(sys.executable).with_name("openstack-ro").exists()
    else "openstack-ro"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ERROR_RE = re.compile(r"##\[error\](.*)")
TS_PREFIX_RE = re.compile(r"^\S+Z ")
REFUSED_RE = re.compile(r"REFUSED reason=(\S+)")
DISPATCH_RE = re.compile(
    r"^(\S+Z) DISPATCH (\S+) repo=(\S+) issue=(\S+) by=(\S+) result=(\S+)"
)
LEGACY_DISPATCH_RE = re.compile(r"^(\S+Z) dispatched (\S+)$")
TITLE_ISSUE_RE = re.compile(r"(?:#|-)(\d+)$")
ISSUE_LINE_RE = re.compile(r"issue(?:_number)?=(\d+)")
# The command as the member typed it: GitHub prints a step's env in the log,
# and record-command-context's step carries `COMMAND: /unshelve`. A run a
# workflow started has no typed command; its action is the control-instance
# input, printed as `command_name: shelve`. Both are anchored to the log line's
# own timestamp, so the help text the workflow posts (which lists every
# command, /delete_all included) and the script source GitHub echoes can never
# match. Nothing else in the log names the command (2026-11-10: the old
# "first command word anywhere" rule recorded 538 of 823 commands as /delete_all).
TYPED_COMMAND_RE = re.compile(r"^\S+Z\s+COMMAND: (/\w+)\s*$", re.M)
ACTION_RE = re.compile(r"^\S+Z\s+command_name: (\w+)\s*$", re.M)
# report-dropped-command prints the dropped command's context JSON.
CONTEXT_COMMAND_RE = re.compile(r'^\S+Z\s+"command": "(/\w+)"\s*$', re.M)
# Logs from before record-command-context (October 2026 and earlier): the
# authorize job runs github/command once per command it serves; every step but
# the typed one prints "no command detected in comment". All of them printing
# it means the comment was not a command at all (94 update-renew-label runs).
GH_COMMAND_STEP = "##[group]Run github/command@"
GH_COMMAND_INPUT_RE = re.compile(r"^\S+Z\s+command: (/\w+)\s*$")
GH_NO_COMMAND = "no command detected"
# The day a shipped line belongs to: an ISO date at its start (dispatch,
# fip-police, token-expiry, journal lines) or `[YYYY-MM-DD ` (the runner's _diag).
LINE_DAY_RE = re.compile(r"^\[?(\d{4})-(\d\d)-(\d\d)[T ]")
OTHER_KIND = "other"
# The runner closes every failed step with this line, so it is the LAST error
# line of nearly every failure; the step's own reason is the error before it
# (2026-11-10: 83 of 91 November failures were recorded with this line only).
GENERIC_EXIT_RE = re.compile(r"^Process completed with exit code \d+\.?$")
TS_LINE_RE = re.compile(r"^\S+Z ", re.M)
# A /create or /unshelve that left the member without an instance because of
# what Jetstream2 did, as the failed step's own error line says it. The cause
# comes from Nova's action list when the log has one (the line's own wording
# predates the schedule_instances check: before 2026-11-04 control-instance
# called every unshelve Error a host failure), from the workflow's lines when
# it does not.
CAPACITY_LINES = (
    (
        "unavailable",
        re.compile(r"^No capacity available for flavor ?(?P<flavor>\S*)\s*$"),
    ),
    (
        "no_host",
        re.compile(
            r"^no (?P<flavor>\S+) capacity on Jetstream2 "
            r"\(unshelve request (?P<req>req-\S+) failed at scheduling\)"
        ),
    ),
    (
        "host_failed",
        re.compile(r"^unshelve failed on the compute host after \d+ attempt"),
    ),
    ("timed_out", re.compile(r"^Instance failed to reach ACTIVE state")),
)
# Unshelve only: the same lines end a shelve or a delete that ran out of time.
UNSHELVE_TIMEOUT_LINES = (
    re.compile(r"^Maximum wait time \(\d+ seconds\) exceeded\.$"),
    re.compile(r"^Server action still in progress after \d+s"),
)
CAPACITY_COMMANDS = ("/create", "/unshelve")
NO_HOST_ATTEMPT_RE = re.compile(
    r"^Attempt \d+ found no host for the flavor \(request (req-\S+)\): no capacity\.$",
    re.M,
)
# "(request R; failed step: S)" since 2026-11-04; "(request R)" before, when
# the workflow did not yet look at which step failed.
HOST_ATTEMPT_RE = re.compile(
    r"^Attempt \d+ failed on the compute host "
    r"\(request (req-[^;)\s]+)(?:; failed step: ([^)]*))?\)\.$",
    re.M,
)
# Nova's fault when every host it tried failed the build.
EXHAUSTED_RE = re.compile(r"Exceeded maximum number of retries|Exhausted all hosts")
# collect-instance-diagnostics' block: the server's fault, then Nova's actions
# newest first, each with its steps (step, result, host id; compute_* steps run
# on a compute host).
NOVA_ACTIONS_RE = re.compile(r"^Recent actions, newest first.*$", re.M)
NOVA_FAULT_RE = re.compile(r"^fault +(.*?)\s*$", re.M)
NOVA_ACTION_RE = re.compile(
    r"^(\d{4}-\d\d-\d\dT[\d:]+)\s+(\w+)\s+(\S+)\s+(req-\S+)\s*$"
)
NOVA_STEP_RE = re.compile(r"^ {4}(\w+)\s+(\S+)(?:\s+(\S+))?")


def log(msg: str) -> None:
    # The cron log is the record of what was done; stdout is the right place.
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)  # noqa: T201


def parse_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0


def to_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def day_path(iso: str) -> str:
    return f"{iso[0:4]}/{iso[5:7]}/{iso[8:10]}"


def month_path(iso: str) -> str:
    return f"{iso[0:4]}/{iso[5:7]}"


# --- configuration ---------------------------------------------------------


class Config:
    def __init__(self, sources: dict, signatures: dict):
        self.raw = sources
        self.bots = set(sources.get("bots") or [])
        self.test_accounts = set(sources.get("test_accounts") or [])
        self.admins = set(sources.get("admins") or [])
        self.kinds: dict[str, str] = {}
        for kind, names in (sources.get("workflows") or {}).items():
            for name in names or []:
                self.kinds[name] = kind
        dropped = sources.get("dropped_report") or {}
        self.dropped_report_workflow = dropped.get("workflow")
        self.dropped_report_step = dropped.get("step")
        self.refusal_jobs = set(sources.get("refusal_jobs") or [])
        self.refusal_steps = set(sources.get("refusal_steps") or [])
        self.refusal_errors = [
            (r["reason"], re.compile(r["match"]))
            for r in sources.get("refusal_errors") or []
        ]
        self.command_by_workflow = dict(sources.get("command_by_workflow") or {})
        self.runner_logs = list(sources.get("runner_logs") or [])
        self.diag_dir = sources.get("diag_dir")
        self.diag_match = re.compile(sources.get("diag_match") or "ERR")
        self.grafana = sources.get("grafana") or {}
        self.access = sources.get("access_outages") or {}
        self.github_status = sources.get("github_status")
        # Coverage amendment: I (host journals)
        self.journal_noise = [
            (n["name"], re.compile(n["match"]))
            for n in sources.get("journal_noise") or []
        ]
        self.signature_version = signatures.get("version", 0)
        self.signatures = [
            (s["signature"], s["field"], re.compile(s["match"]))
            for s in signatures.get("signatures") or []
        ]

    def source(self, repo: str) -> dict:
        src = (self.raw.get("sources") or {}).get(repo)
        if not src:
            msg = f"{repo} is not in incident-sources.yml"
            raise ValueError(msg)
        return src

    def extra_source(self, repo: str) -> dict:
        """A repository harvested under class K: listed in `sources` when it
        needs settings, else defaults (no instance prefix, no hosts)."""
        listed = (self.raw.get("sources") or {}).get(repo)
        if listed:
            return listed
        return {"short": repo.split("/")[-1].lower(), "instance_prefix": None}

    def kind(self, workflow_file: str) -> str:
        """The listed kind; an unlisted workflow is `other` (a failure is an
        incident, a success a count) AND is named in last_error until it is
        listed (unmapped_note), so a new workflow cannot pass unclassified."""
        return self.kinds.get(workflow_file, OTHER_KIND)


def data_dir(here: Path = HERE) -> Path:
    """The data files sit beside the script on a runner (install-harvester.sh)
    and at the repository root in a checkout."""
    return here if (here / "incident-sources.yml").is_file() else here.parent


def load_config(here: Path = HERE) -> Config:
    d = data_dir(here)
    sources = yaml.safe_load((d / "incident-sources.yml").read_text("utf-8"))
    signatures = yaml.safe_load((d / "incident-signatures.yml").read_text("utf-8"))
    return Config(sources or {}, signatures or {})


# --- redaction -------------------------------------------------------------


class Redactor:
    """The two passes the plan requires: redact.sed, then email -> HMAC.
    Without a key the address is dropped ("[email]"): fails closed."""

    def __init__(self, sed_file: Path, key: bytes | None):
        self.sed_file = sed_file
        self.key = key

    @property
    def email_hash(self) -> str:
        return "hmac16" if self.key else "unavailable"

    def _email(self, m: re.Match) -> str:
        if not self.key:
            return "[email]"
        digest = hmac.new(self.key, m.group(0).lower().encode(), hashlib.sha256)
        return f"[email:{digest.hexdigest()[:16]}]"

    def text(self, text: str) -> tuple[str, int]:
        if not text:
            return text, 0
        r = subprocess.run(
            ["sed", "-E", "-f", str(self.sed_file)],
            input=text,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        out = r.stdout
        replaced = sum(
            out.count(tag) - text.count(tag)
            for tag in ("[token]", "[redacted]", "[private-key]")
        )
        out, n = EMAIL_RE.subn(self._email, out)
        return out, replaced + n


def load_redactor(here: Path = HERE) -> Redactor:
    key = HMAC_KEY.read_bytes().strip() if HMAC_KEY.is_file() else None
    sed = here / "redact.sed"
    if not sed.is_file():  # a checkout rather than an install
        sed = data_dir(here) / ".github" / "actions" / "record-facts" / "redact.sed"
    return Redactor(sed, key or None)


# --- GitHub ----------------------------------------------------------------


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a 3xx as HTTPError: the log download redirects to a signed
    URL that must be fetched WITHOUT our Authorization header."""

    def redirect_request(self, *_args, **_kwargs):
        return None


class GitHub:
    """The REST API over urllib: the response headers (rate limit, token
    expiry) are read directly, no gh process per call."""

    def __init__(self, token: str, pace: bool = False):
        self.token = token
        self.pace = pace
        self.calls = 0
        self.remaining: int | None = None
        self.reset: float | None = None

    def _request(self, url: str, auth: bool = True, accept: str | None = None):
        req = urllib.request.Request(url)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", accept or "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "morphocloud-harvest-incidents")
        opener = urllib.request.build_opener(NoRedirect)
        return opener.open(req, timeout=60)

    def _budget(self, headers) -> None:
        rem = headers.get("x-ratelimit-remaining")
        if rem is not None:
            self.remaining = int(rem)
            self.reset = float(headers.get("x-ratelimit-reset") or 0)
            if self.pace and self.remaining < PACE_BELOW and self.reset:
                wait = max(0.0, self.reset - time.time()) + 5
                log(f"rate limit at {self.remaining}; sleeping {int(wait)} s")
                time.sleep(wait)

    def get(self, path: str, params: dict | None = None, raw: bool = False):
        """JSON (or bytes with raw=True); None on 404/410; retries 5xx."""
        url = API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        for attempt in range(RETRIES):
            try:
                self.calls += 1
                with self._request(url) as resp:
                    self._budget(resp.headers)
                    body = resp.read()
                    return body if raw else json.loads(body or b"null")
            except urllib.error.HTTPError as e:
                self._budget(e.headers)
                if e.code in (301, 302, 303, 307):
                    # a signed download URL: fetch it without our token
                    with urllib.request.urlopen(
                        e.headers["Location"], timeout=120
                    ) as blob:
                        return blob.read()
                if e.code in (404, 410):
                    return None
                if e.code == HTTP_FORBIDDEN and "rate limit" in (
                    e.read() or b""
                ).decode("utf-8", "replace"):
                    wait = max(0.0, (self.reset or time.time() + 60) - time.time()) + 5
                    log(f"rate limited; sleeping {int(wait)} s")
                    time.sleep(wait)
                    continue
                if e.code >= HTTP_SERVER_ERROR and attempt < RETRIES - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < RETRIES - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        return None

    def paged(
        self, path: str, key: str, params: dict | None = None, pages: int = MAX_PAGES
    ):
        out: list = []
        for page in range(1, pages + 1):
            data = self.get(path, {**(params or {}), "per_page": PAGE, "page": page})
            items = (data or {}).get(key) or []
            out.extend(items)
            if len(items) < PAGE:
                break
        return out


# --- the store -------------------------------------------------------------


class Store:
    """The checkout of the store repository. Every write goes through here so
    --dry-run can say what it would have written and write nothing."""

    def __init__(self, root: Path, short: str, dry_run: bool):
        self.root = root
        self.short = short
        self.dry_run = dry_run
        self.writes = 0
        # day -> {"counts": {wf: {result: n}}, "recorded": set("<run>-<attempt>")};
        # kept in memory and written by flush() (once per cycle, or per backfill
        # day), not once per run: a busy day has thousands of runs.
        self._rollups: dict[str, dict] = {}
        self._dirty: set[str] = set()

    # -- paths
    def rel(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def _rollup(self, created_at: str) -> dict:
        day = day_path(created_at)
        if day not in self._rollups:
            p = self.rel("rollups", day, f"{self.short}.json")
            data: dict = {}
            if p.is_file():
                try:
                    data = json.loads(p.read_text("utf-8"))
                except ValueError:
                    log(f"rollup {p} unreadable; starting it over")
            self._rollups[day] = {
                "counts": data.get("counts") or {},
                "recorded": set(data.get("recorded") or []),
            }
        return self._rollups[day]

    def recorded(self, created_at: str, run_id: int, attempt: int) -> bool:
        return f"{run_id}-{attempt}" in self._rollup(created_at)["recorded"]

    def mark(
        self, created_at: str, run_id: int, attempt: int, workflow: str, result: str
    ) -> None:
        """Called LAST for a run, after its files are written, so a failure
        halfway leaves the run unrecorded and the next cycle retries it."""
        roll = self._rollup(created_at)
        roll["recorded"].add(f"{run_id}-{attempt}")
        roll["counts"].setdefault(workflow, {})
        roll["counts"][workflow][result] = roll["counts"][workflow].get(result, 0) + 1
        self._dirty.add(day_path(created_at))

    def recount(self, created_at: str, workflow: str, old: str, new: str) -> None:
        """--rederive: one recorded run's result changed from old to new."""
        counts = self._rollup(created_at)["counts"].setdefault(workflow, {})
        if counts.get(old, 0) > 0:
            counts[old] -= 1
            if not counts[old]:
                del counts[old]
        counts[new] = counts.get(new, 0) + 1
        self._dirty.add(day_path(created_at))

    def flush(self) -> None:
        for day in sorted(self._dirty):
            roll = self._rollups[day]
            self.write_json(
                f"rollups/{day}/{self.short}.json",
                {"counts": roll["counts"], "recorded": sorted(roll["recorded"])},
            )
        self._dirty.clear()

    # -- writes
    def _write(self, rel: str, data: bytes, append: bool = False) -> None:
        self.writes += 1
        if self.dry_run:
            log(f"would write {rel} ({len(data)} bytes{', append' if append else ''})")
            return
        p = self.rel(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab" if append else "wb") as fh:
            fh.write(data)

    def write_json(self, rel: str, obj) -> None:
        self._write(rel, (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode())

    def write_gz(self, rel: str, text: str) -> None:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            gz.write(text.encode("utf-8", "replace"))
        self._write(rel, buf.getvalue())

    def write_text(self, rel: str, text: str) -> None:
        self._write(rel, text.encode("utf-8", "replace"))

    def append_text(self, rel: str, text: str) -> None:
        self._write(rel, text.encode("utf-8", "replace"), append=True)

    def append_jsonl(self, rel: str, obj) -> None:
        self._write(rel, (json.dumps(obj, sort_keys=True) + "\n").encode(), append=True)

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._write(rel, data)

    def remove(self, rel: str) -> None:
        """--rederive only: a file, or a directory with its files."""
        self.writes += 1
        if self.dry_run:
            log(f"would remove {rel}")
            return
        p = self.rel(rel)
        if p.is_dir():
            for child in p.iterdir():
                child.unlink()
            p.rmdir()
        elif p.exists():
            p.unlink()

    def read_json(self, rel: str, default=None):
        p = self.rel(rel)
        if not p.is_file():
            return default
        try:
            return json.loads(p.read_text("utf-8"))
        except ValueError:
            return default

    # -- git
    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )

    def commit_push(self, message: str) -> bool:
        """True when a commit was made. A failed push is logged and left for
        the next cycle (the commit stays local); nothing is lost."""
        self.flush()
        if self.dry_run:
            log(f"would commit: {message}")
            return False
        try:
            self.git("add", "-A")
            if self.git("status", "--porcelain").stdout.strip() == "":
                return False
            self.git("commit", "-q", "-m", message)
        except subprocess.CalledProcessError as e:
            log(f"git commit failed: {e.stderr.strip()[-300:]}")
            return False
        try:
            self.git("pull", "-q", "--rebase")
        except subprocess.CalledProcessError as e:
            # Never leave a rebase in progress: it would wedge every later
            # cycle. Abort it; the commit stays local and the next cycle retries.
            log(
                f"pull --rebase failed ({e.stderr.strip()[-200:]}); aborting the rebase"
            )
            subprocess.run(
                ["git", "-C", str(self.root), "rebase", "--abort"],
                capture_output=True,
                check=False,
                timeout=120,
            )
            return True
        try:
            self.git("push", "-q")
        except subprocess.CalledProcessError as e:
            log(f"push failed, commit kept locally: {e.stderr.strip()[-200:]}")
        return True


class Local:
    """Runner-side state that is not evidence: the watch list, the runner-log
    offsets, the lookback stamp. Losing it costs nothing that the hourly
    lookback and the in-flight listings do not recover."""

    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        self.data = {
            "watch": {},
            "offsets": {},
            "lookback_at": 0,
            "diag_offsets": {},
            "diag_remote_offsets": {},
        }
        if path.is_file():
            try:
                self.data.update(json.loads(path.read_text("utf-8")))
            except ValueError:
                log(f"{path} unreadable; starting local state over")

    def save(self) -> None:
        if self.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class LocalView:
    """One extra repository's slice of the runner-side state (its watch list
    and lookback stamp), kept inside the main Local under `repos`. The main
    Local saves it."""

    def __init__(self, local: Local, repo: str):
        self.data = local.data.setdefault("repos", {}).setdefault(
            repo, {"watch": {}, "lookback_at": 0}
        )

    def save(self) -> None:
        return


# --- what one run is -------------------------------------------------------


def base_result(conclusion: str | None) -> str:
    if conclusion in ("timed_out", "startup_failure"):
        return "failure"
    return conclusion or "unknown"


def needs_detail(kind: str, result: str) -> bool:
    """Jobs and the full log: every command and alert run that ran, and every
    run that did not succeed. A skipped run is a count only (A1), and so is
    every run of kind `code` (CI and code review: counted, never evidence)."""
    if kind == "code":
        return False
    return result not in ("success", "skipped") or (
        kind in ("command", "alert") and result == "success"
    )


def finalize(
    cfg: Config, workflow: str, kind: str, result: str, jobs: list[dict]
) -> tuple[str, bool]:
    """The final result and whether the run is an incident.

    report-dropped-command succeeds whether or not it found a dropped command;
    only a run where the reporting step ran is `dropped`. A successful alert
    run is an incident (something asked for the admins); skipped and refused
    runs never are; everything else that did not succeed is."""
    if workflow == cfg.dropped_report_workflow and result == "success":
        reported = any(
            s.get("name") == cfg.dropped_report_step
            and s.get("conclusion") == "success"
            for j in jobs
            for s in j.get("steps") or []
        )
        return ("dropped", True) if reported else ("success", False)
    if kind == "code":
        return result, False
    if result == "success":
        return result, kind == "alert"
    if result in ("skipped", "refused"):
        return result, False
    return result, True


def wants_outcome_line(kind: str, result: str) -> bool:
    """One line per command run and per run that did not succeed; skipped
    runs, successful sweeps and every `code` run are rollup counts."""
    if kind == "code":
        return False
    return (kind == "command" and result != "skipped") or result not in (
        "success",
        "skipped",
    )


def first_failed_step(jobs: list[dict]) -> dict | None:
    for job in jobs:
        if job.get("conclusion") not in ("failure", "timed_out", "cancelled"):
            continue
        for step in job.get("steps") or []:
            if step.get("conclusion") in ("failure", "timed_out", "cancelled"):
                return {
                    "job": job.get("name"),
                    "step": step.get("name"),
                    "number": step.get("number"),
                    "conclusion": step.get("conclusion"),
                }
        return {
            "job": job.get("name"),
            "step": None,
            "number": None,
            "conclusion": job.get("conclusion"),
        }
    return None


def is_refusal(
    cfg: Config,
    failed: dict | None,
    logtext: str | None,
    error_line: str | None = None,
) -> str | None:
    """The refusal reason, or None when the failure is real.

    A refusal is a request the workflow declined by design (a limit, a state,
    a permission), never a failure. In order: a `REFUSED reason=` line; the
    failed step's own error line matching refusal_errors; then the step and
    job names, only when the step printed no reason of its own (a GitHub 503
    inside the authorize job is a failure, not a refusal)."""
    if logtext:
        m = REFUSED_RE.search(logtext)
        if m:
            return m.group(1)
    for reason, regex in cfg.refusal_errors:
        if error_line and regex.search(error_line):
            return reason
    if error_line and not GENERIC_EXIT_RE.match(error_line):
        return None
    if failed:
        if failed.get("step") in cfg.refusal_steps:
            return failed["step"]
        if failed.get("job") in cfg.refusal_jobs:
            return f"job:{failed['job']}"
    return None


def _plain(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pick_error(errors: list[str]) -> str | None:
    """The last error line that says why, else the last one: the runner's
    closing "Process completed with exit code N." only when nothing else is."""
    lines = [TS_PREFIX_RE.sub("", e).strip() for e in errors]
    lines = [e for e in lines if e]
    specific = [e for e in lines if not GENERIC_EXIT_RE.match(e)]
    return (specific or lines or [None])[-1]


def nova_actions(text: str) -> tuple[str | None, list[dict]] | None:
    """The last diagnostics block of a log (timestamps stripped): the server's
    fault (None when Nova recorded none) and its actions newest first, or None
    when the log has no such block."""
    heads = list(NOVA_ACTIONS_RE.finditer(text))
    if not heads:
        return None
    head = heads[-1]
    faults = list(NOVA_FAULT_RE.finditer(text, 0, head.start()))
    fault = faults[-1].group(1) if faults else None
    if fault == "(none recorded)":
        fault = None
    actions: list[dict] = []
    for line in text[head.end() :].splitlines()[1:]:
        a = NOVA_ACTION_RE.match(line)
        if a:
            actions.append(
                {
                    "request_id": a[4],
                    "action": a[2],
                    "started": a[1] + "Z",
                    "result": a[3],
                    "steps": [],
                }
            )
            continue
        s = NOVA_STEP_RE.match(line)
        if s and actions:
            actions[-1]["steps"].append({"step": s[1], "result": s[2], "host_id": s[3]})
            continue
        break
    return fault, actions


def request_outcome(req: dict, fault: str | None) -> str:
    """What became of one Nova request: no_host (the scheduler placed it
    nowhere), host_failed (placed, and the compute host failed it),
    in_progress (still scheduling or spawning when the run gave up), ok.
    fault is the server's, so it applies to the newest request only."""
    compute = [s for s in req["steps"] if s["step"].startswith("compute_")]
    if compute:
        if (
            any(s["result"] == "Error" for s in compute)
            or req["result"] == "Error"
            or (fault and EXHAUSTED_RE.search(fault))
        ):
            return "host_failed"
        return "in_progress" if any(s["result"] == "-" for s in compute) else "ok"
    if req["result"] == "Error" or any(s["result"] == "Error" for s in req["steps"]):
        return "no_host"
    return "in_progress"


def workflow_requests(text: str, message_req: str | None) -> list[dict]:
    """The run's Nova requests as control-instance's own lines name them (a
    log without the diagnostics block: production until it has one)."""
    reqs: dict[str, dict] = {}
    for m in NO_HOST_ATTEMPT_RE.finditer(text):
        reqs[m[1]] = {"request_id": m[1], "outcome": "no_host"}
    for m in HOST_ATTEMPT_RE.finditer(text):
        step = m[2]
        outcome = (
            "unknown"
            if not step
            else "host_failed"
            if "compute_" in step
            else "no_host"
            if "schedule" in step
            else "unknown"
        )
        reqs[m[1]] = {"request_id": m[1], "outcome": outcome, "failed_step": step}
    if message_req and message_req not in reqs:
        reqs[message_req] = {"request_id": message_req, "outcome": "no_host"}
    return list(reqs.values())


def capacity_said(
    error_line: str | None, command: str | None
) -> tuple[str, dict] | None:
    """What the failed step's error line says happened at Jetstream2, with its
    named groups (flavor, req), or None when the line is about anything else."""
    if command not in CAPACITY_COMMANDS or not error_line:
        return None
    for label, regex in CAPACITY_LINES:
        m = regex.search(error_line)
        if m:
            return label, m.groupdict()
    if command == "/unshelve" and any(
        r.search(error_line) for r in UNSHELVE_TIMEOUT_LINES
    ):
        return "timed_out", {}
    return None


def nova_requests(text: str, since: str) -> tuple[str | None, list[dict]]:
    """The server's fault and the create/unshelve requests Nova shows since
    the run started (newest first), each with its outcome and compute hosts."""
    diag = nova_actions(text)
    if not diag or not since:  # no run start: no way to tell this run's requests
        return None, []
    fault, actions = diag
    reqs = [
        a
        for a in actions
        if a["action"] in ("create", "unshelve") and a["started"][:19] >= since
    ]
    for i, r in enumerate(reqs):
        r["outcome"] = request_outcome(r, fault if i == 0 else None)
        r["hosts"] = sorted(
            {
                s["host_id"]
                for s in r["steps"]
                if s["step"].startswith("compute_") and s["host_id"]
            }
        )
    # The fault is the server's latest; it belongs to this run only when one
    # of this run's requests is there.
    return (fault if reqs else None), reqs


def run_cause(said: str, requests: list[dict]) -> str:
    """One cause for the run: a timeout is timed_out whatever Nova shows; a
    host that failed a request makes it host_failed; then unknown (a request
    the log cannot place, or none at all); else no_host."""
    outcomes = {r["outcome"] for r in requests}
    if said == "timed_out":
        return "timed_out"
    if "host_failed" in outcomes:
        return "host_failed"
    if "unknown" in outcomes or "no_host" not in outcomes:
        return "unknown"
    return "no_host"


def capacity_of(
    logtext: str | None,
    error_line: str | None,
    command: str | None,
    started: str | None,
    flavor: str | None,
) -> dict | None:
    """A /create or /unshelve that left the member without an instance because
    of Jetstream2, with the evidence to tell why; None for every other run.
    Record-only: the queries decide what counts.

    cause: no_host (the scheduler found no host for the flavor), host_failed
    (Nova placed it and the compute host failed: what to report to
    Jetstream2), timed_out (not running when the workflow's wait ran out),
    unknown (the log cannot tell no_host from host_failed). requests: every
    create/unshelve request Nova shows since the run started, each with its
    outcome, steps and compute hosts. evidence: nova (the diagnostics block),
    workflow (control-instance's attempt lines), none."""
    said = capacity_said(error_line, command)
    if said is None:
        return None
    label, groups = said
    text = TS_LINE_RE.sub("", logtext or "")
    fault, requests = nova_requests(text, (started or "")[:19])
    evidence = "nova" if requests else "none"
    if not requests:
        requests = workflow_requests(text, groups.get("req"))
        evidence = "workflow" if requests else "none"
    named = groups.get("flavor")
    return {
        "cause": run_cause(label, requests),
        "flavor": named if named and named != "flavor" else flavor,
        "message": error_line,
        "evidence": evidence,
        "fault": fault,
        "requests": requests,
    }


def assemble_log(zip_bytes: bytes, failed: dict | None) -> tuple[str, str, str | None]:
    """The whole log as one text (the per-job files in order, a header line
    each), the failed step's last TAIL_LINES lines, and its last error line."""
    parts: list[str] = []
    step_tail = ""
    error_line = None
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        job_files = sorted(
            (n for n in names if "/" not in n and n.endswith(".txt")),
            key=lambda n: int(n.split("_", 1)[0])
            if n.split("_", 1)[0].isdigit()
            else 1 << 30,
        )
        for name in job_files:
            parts.append(f"===== {name} =====\n")
            parts.append(zf.read(name).decode("utf-8", "replace"))
            if not parts[-1].endswith("\n"):
                parts.append("\n")
        if failed and failed.get("number") is not None:
            prefix = f"{failed['number']}_"
            # The archive's job folder is the job name with punctuation
            # replaced, so compare on letters and digits only.
            want = _plain(str(failed.get("job") or ""))[:40]
            candidates = [
                n
                for n in names
                if "/" in n
                and n.rsplit("/", 1)[1].startswith(prefix)
                and (not want or _plain(n.split("/", 1)[0]).startswith(want))
            ]
            if candidates:
                text = zf.read(candidates[0]).decode("utf-8", "replace")
                lines = text.splitlines()
                step_tail = "\n".join(lines[-TAIL_LINES:]) + ("\n" if lines else "")
                error_line = pick_error(ERROR_RE.findall(text))
        if not job_files:  # no per-job file: keep whatever text files there are
            for name in sorted(n for n in names if n.endswith(".txt")):
                parts.append(f"===== {name} =====\n")
                parts.append(zf.read(name).decode("utf-8", "replace"))
                if not parts[-1].endswith("\n"):
                    parts.append("\n")
    whole = "".join(parts)
    if error_line is None:
        error_line = pick_error(ERROR_RE.findall(whole))
    # A run cancelled before it started (the concurrency queue) has an EMPTY
    # archive; None says "no log", so no empty job.log.gz is written.
    return (whole or None), step_tail, error_line


def classify(
    cfg: Config, error_line: str | None, failed_step: str | None, workflow: str
) -> str:
    fields = {
        "error_line": error_line or "",
        "failed_step": failed_step or "",
        "workflow": workflow,
    }
    for signature, on, regex in cfg.signatures:
        if regex.search(fields.get(on, "")):
            return signature
    return "unclassified"


def identify(
    run: dict, logtext: str | None, prefix: str, cfg: Config, kind: str = "command"
) -> dict:
    """issue number, instance name and command, from the title and the log.

    The log is consulted for the issue and the instance only for a command
    run, which is about one instance. A sweep or alert run names many
    instances in its log, and the first one would be an arbitrary, wrong
    attribution; those runs get an issue only from their title (an
    `issue_comment` or `issues` event)."""
    issue = None
    m = TITLE_ISSUE_RE.search(run.get("display_title") or "")
    if m and run.get("event") in ("issue_comment", "issues"):
        issue = int(m.group(1))
    name = None
    name_source = None
    command = None
    if logtext and kind == "command":
        nm = re.search(re.escape(prefix) + r"(\d+)\b", logtext)
        if nm:
            name = nm.group(0)
            name_source = "log"
            issue = issue or int(nm.group(1))
        if issue is None:
            im = ISSUE_LINE_RE.search(logtext)
            if im:
                issue = int(im.group(1))
    if name is None and issue is not None and kind == "command":
        name = f"{prefix}{issue}"
        name_source = "derived"
    command = command_of(logtext, workflow_file(run), cfg)
    return {
        "issue": issue,
        "name": name,
        "name_source": name_source,
        "command": command,
    }


def github_command_match(logtext: str) -> tuple[bool, str | None]:
    """(the log has github/command steps, the command of the one that did not
    say GH_NO_COMMAND)."""
    seen = False
    asked: str | None = None
    declined = False
    for line in logtext.splitlines():
        if "##[group]Run " in line:
            if asked and asked != "?" and not declined:
                return True, asked
            asked, declined = ("?", False) if GH_COMMAND_STEP in line else (None, False)
            seen = seen or asked == "?"
        elif asked == "?":
            m = GH_COMMAND_INPUT_RE.match(line)
            if m:
                asked = m.group(1)
        elif asked and GH_NO_COMMAND in line:
            declined = True
    if asked and asked != "?" and not declined:
        return True, asked
    return seen, None


def command_of(logtext: str | None, workflow: str, cfg: Config) -> str | None:
    """The typed command, else the workflow-started action, else the dropped
    command's context, else the github/command step that matched; when that
    step ran and nothing matched, no command; with no log line at all, the
    one command the workflow serves (TYPED_COMMAND_RE says why only these)."""
    if logtext:
        for rx, prefix in (
            (TYPED_COMMAND_RE, ""),
            (ACTION_RE, "/"),
            (CONTEXT_COMMAND_RE, ""),
        ):
            m = rx.search(logtext)
            if m:
                return prefix + m.group(1)
        ran, matched = github_command_match(logtext)
        if ran:
            return matched
    return cfg.command_by_workflow.get(workflow)


def unmapped_note(cfg: Config, seen: set[str], data: dict) -> str | None:
    """Workflows recorded as `other` because incident-sources.yml does not
    list them. Kept in the runner-side state until they are listed, so the
    watchdog's last_error check sees them at every cycle, not only in the
    cycle that happened to record such a run."""
    names = set(data.get("unmapped_workflows") or []) | seen
    names = {n for n in names if n not in cfg.kinds}
    data["unmapped_workflows"] = sorted(names)
    if not names:
        return None
    return (
        "workflows not listed in incident-sources.yml (recorded as kind other): "
        + ", ".join(sorted(names))
    )


def split_by_day(lines: list[str], default: str) -> dict[str, list[str]]:
    """Each line under the day it carries (LINE_DAY_RE). A `# ` header (a
    _diag file name) goes with the line after it; any other undated line (a
    traceback, a JSON status line) stays with the line before it; undated
    lines with nothing dated before them take `default` (the day shipped).
    A first read of a long-lived log (the dispatch log from June) so lands
    in June, not in the day it was read."""
    days: list[str | None] = []
    for line in lines:
        m = LINE_DAY_RE.match(line)
        days.append(f"{m[1]}/{m[2]}/{m[3]}" if m else None)
    after = None
    for i in range(len(lines) - 1, -1, -1):
        if days[i]:
            after = days[i]
        elif lines[i].startswith("# ") and after:
            days[i] = after
    out: dict[str, list[str]] = {}
    current = default
    for line, day in zip(lines, days):
        current = day or current
        out.setdefault(current, []).append(line)
    return out


def workflow_file(run: dict) -> str:
    return (run.get("path") or "").rsplit("/", 1)[-1]


# --- actor_kind --------------------------------------------------------------


class Dispatches:
    """The runner-side DISPATCH lines (and the pre-PR A `dispatched <wf>`
    lines, for the backfill), indexed by workflow, for rule 1."""

    def __init__(self, paths: list[Path]):
        self.paths = paths
        self.by_wf: dict[str, list[tuple[float, str | None, str | None]]] = {}
        self.loaded_at = 0.0
        self.refreshed_for: set[tuple[str, float]] = set()  # one re-read per run
        self.refresh()

    def refresh(self) -> None:
        """Re-read the logs; called once at start and again when a run created
        after the last read finds no line (the dispatcher may have written it
        a moment after we read)."""
        self.by_wf = {}
        self.loaded_at = time.time()
        for p in self.paths:
            if not p.is_file():
                continue
            try:
                with p.open("rb") as fh:
                    fh.seek(max(0, p.stat().st_size - DISPATCH_LOG_TAIL))
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            for line in text.splitlines():
                m = DISPATCH_RE.match(line)
                if m:
                    issue = m.group(4) if m.group(4) != "-" else None
                    self.by_wf.setdefault(m.group(2), []).append(
                        (parse_ts(m.group(1)), m.group(3), issue)
                    )
                    continue
                m = LEGACY_DISPATCH_RE.match(line)
                if m:
                    self.by_wf.setdefault(m.group(2), []).append(
                        (parse_ts(m.group(1)), None, None)
                    )

    def match(
        self, workflow: str, repo: str, created_at: str, issue: int | None
    ) -> bool:
        t = parse_ts(created_at)
        if (
            not self._match(workflow, repo, t, issue)
            and t > self.loaded_at - DISPATCH_JOIN
            and (workflow, t) not in self.refreshed_for
        ):
            self.refreshed_for.add((workflow, t))  # a human dispatch never matches
            self.refresh()
        return self._match(workflow, repo, t, issue)

    def _match(self, workflow: str, repo: str, t: float, issue: int | None) -> bool:
        for when, line_repo, line_issue in self.by_wf.get(workflow, []):
            if abs(when - t) > DISPATCH_JOIN:
                continue
            if line_repo and line_repo != repo:
                continue
            if line_issue and issue is not None and str(issue) != line_issue:
                continue
            return True
        return False


def actor_kind(  # noqa: PLR0913 — the six rules need these five inputs
    cfg: Config,
    run: dict,
    repo: str,
    issue: int | None,
    issue_author: str | None,
    dispatches: Dispatches,
) -> tuple[str, str | None]:
    """Rule 0 (the event) then the plan's five rules, in order. Returns (kind, rule)."""
    actor = (run.get("actor") or {}).get("login") or ""
    trigger = (run.get("triggering_actor") or {}).get("login") or ""
    # GitHub's own scheduler and a workflow_run follow-up are automation by
    # definition; GitHub credits such a run to whoever last committed the
    # workflow file (jcfr on Test), who would otherwise read as a member. A
    # RERUN of such a run keeps the event but its triggering_actor is the
    # person who clicked, so attempt 2+ falls through to the rules below.
    first_attempt = int(run.get("run_attempt") or 1) == 1
    if run.get("event") in ("schedule", "workflow_run") and first_attempt:
        verdict = ("automation", f"event:{run['event']}")
    elif (
        run.get("event") == "workflow_dispatch"
        and first_attempt  # a rerun keeps attempt 1's created_at; the join is attempt 1's
        and dispatches.match(
            workflow_file(run), repo, run.get("created_at") or "", issue
        )
    ):
        verdict = ("automation", "dispatch-line")
    elif actor in cfg.bots or trigger in cfg.bots:
        verdict = ("automation", "bot")
    elif actor in cfg.test_accounts or trigger in cfg.test_accounts:
        verdict = ("test", "test-account")
    elif actor in cfg.admins or trigger in cfg.admins:
        login = trigger or actor
        if issue_author and login == issue_author:
            verdict = ("member", "admin-own-issue")
        else:
            verdict = ("admin", "admin")
    else:
        verdict = ("member", "default")
    return verdict


# --- outside context (E) -----------------------------------------------------


def fetch_json(url: str, data: bytes | None = None, timeout: int = 30):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", "morphocloud-harvest-incidents")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


class Feeds:
    """ACCESS outages and githubstatus, fetched once per cycle on first use."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._access = None
        self._github = None

    def access(self) -> list[dict] | None:
        if self._access is None:
            rows: list[dict] = []
            try:
                for key in ("current", "past"):
                    url = self.cfg.access.get(key)
                    if url:
                        data = fetch_json(url)
                        rows.extend((data or {}).get("results") or [])
                self._access = rows
            except (urllib.error.URLError, ValueError, OSError, TimeoutError) as e:
                log(f"ACCESS feed unavailable: {e}")
                self._access = False
        return None if self._access is False else self._access

    def github(self) -> list[dict] | None:
        if self._github is None:
            try:
                data = (
                    fetch_json(self.cfg.github_status) if self.cfg.github_status else {}
                )
                self._github = (data or {}).get("incidents") or []
            except (urllib.error.URLError, ValueError, OSError, TimeoutError) as e:
                log(f"githubstatus feed unavailable: {e}")
                self._github = False
        return None if self._github is False else self._github


def access_context(
    rows: list[dict] | None, match: str, start: float, end: float
) -> dict:
    if rows is None:
        return {"status": "unavailable", "outages": []}
    hits = []
    for r in rows:
        if match.lower() not in str(r.get("AffectedResources", "")).lower():
            continue
        s = parse_ts(r.get("OutageStart"))
        e = parse_ts(r.get("OutageEnd")) or (end + 1)
        if s <= end and e >= start:
            hits.append(
                {
                    "subject": r.get("Subject"),
                    "type": r.get("OutageType"),
                    "start": r.get("OutageStart"),
                    "end": r.get("OutageEnd"),
                    "urn": r.get("URN"),
                }
            )
    return {"status": "outage" if hits else "operational", "outages": hits}


def github_status_context(
    incidents: list[dict] | None, start: float, end: float
) -> dict:
    if incidents is None:
        return {"status": "unavailable", "incidents": []}
    oldest = min((parse_ts(i.get("created_at")) for i in incidents), default=0)
    if oldest and start < oldest:
        return {"status": "unknown", "incidents": [], "feed_oldest": to_iso(oldest)}
    hits = []
    for i in incidents:
        s = parse_ts(i.get("created_at"))
        e = parse_ts(i.get("resolved_at")) or (end + 1)
        if s <= end and e >= start:
            hits.append(
                {
                    "id": i.get("id"),
                    "name": i.get("name"),
                    "impact": i.get("impact"),
                    "created_at": i.get("created_at"),
                    "resolved_at": i.get("resolved_at"),
                    "components": [c.get("name") for c in i.get("components") or []],
                }
            )
    return {"status": "incident" if hits else "operational", "incidents": hits}


def grafana_vacancy(cfg: Config, flavor: str | None, start: float, end: float) -> dict:
    spec = (cfg.grafana.get("flavors") or {}).get(flavor or "")
    if not flavor:
        return {"status": "no_flavor"}
    if not spec:
        return {"status": "not_tracked", "flavor": flavor}
    url = cfg.grafana["url"].format(panel=spec["panel"])
    body = json.dumps(
        {
            "intervalMs": 1800000,
            "maxDataPoints": 200,
            "timeRange": {
                "from": str(int((start - 1800) * 1000)),
                "to": str(int((end + 1800) * 1000)),
            },
        }
    ).encode()
    try:
        data = fetch_json(url, body)
    except (urllib.error.URLError, ValueError, OSError, TimeoutError) as e:
        return {"status": "unavailable", "flavor": flavor, "error": str(e)[:200]}
    return {
        "status": "ok",
        "flavor": flavor,
        "panel": spec["panel"],
        "points": grafana_points(data, spec["ref"]),
    }


def grafana_points(data: dict, ref: str) -> list[list]:
    frames = ((data or {}).get("results") or {}).get(ref, {}).get("frames") or []
    points: list[list] = []
    for f in frames:
        values = (f.get("data") or {}).get("values") or []
        if len(values) >= TIME_AND_VALUE:
            points.extend([t, v] for t, v in zip(values[0], values[1]))
    return points


# --- the late snapshot (F6) ----------------------------------------------------


def ro_json(*args: str):
    r = subprocess.run(
        [OPENSTACK_RO, *args, "-f", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[-300:], "exit": r.returncode}
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        return {"error": "not json", "stdout": r.stdout[-300:]}


def late_snapshot(name: str, updated_at: str) -> dict:
    snap: dict = {"instance": name, "taken_at": to_iso(time.time())}
    snap["snapshot_age_seconds"] = int(time.time() - parse_ts(updated_at))
    if not os.environ.get("OS_CLOUD"):
        snap["error"] = "OS_CLOUD not set"
        return snap

    def guarded(key, *args):
        try:
            snap[key] = ro_json(*args)
        except (subprocess.TimeoutExpired, OSError) as e:
            snap[key] = {"error": f"{type(e).__name__}: {e}"[:300]}

    guarded("server", "server", "show", name, "--os-compute-api-version", "2.62")
    guarded("events", "server", "event", "list", name)
    events = snap.get("events") if isinstance(snap.get("events"), list) else []
    snap["event_details"] = []
    for ev in events[:5]:
        rid = ev.get("Request ID") or ev.get("request_id")
        if rid:
            try:
                snap["event_details"].append(
                    ro_json("server", "event", "show", name, rid)
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                snap["event_details"].append({"error": str(e)[:300]})
    guarded("ports", "port", "list", "--server", name)
    snap["floating_ips"] = []
    for port in snap.get("ports") if isinstance(snap.get("ports"), list) else []:
        pid = port.get("ID") or port.get("id")
        if pid:
            try:
                fips = ro_json("floating", "ip", "list", "--port", pid)
                snap["floating_ips"].extend(fips if isinstance(fips, list) else [fips])
            except (subprocess.TimeoutExpired, OSError) as e:
                snap["floating_ips"].append({"error": str(e)[:300]})
    guarded("limits", "limits", "show", "--absolute")
    return snap


# --- the issue (H) ---------------------------------------------------------------


def issue_timeline(
    gh: GitHub, repo: str, issue: int, redactor: Redactor, since: float
) -> list[dict]:
    """The issue's timeline from `since` (label events are kept from the
    start, so the labels at any moment can be reconstructed), comments
    redacted. The timeline endpoint returns a bare list, so it is paged here."""
    raw: list = []
    for page in range(1, 11):
        data = gh.get(
            f"/repos/{repo}/issues/{issue}/timeline", {"per_page": PAGE, "page": page}
        )
        if not data:
            break
        raw.extend(data)
        if len(data) < PAGE:
            break
    out: list[dict] = []
    for ev in raw:
        when = ev.get("created_at") or ev.get("submitted_at") or ""
        if (
            when
            and parse_ts(when) < since
            and ev.get("event") not in ("labeled", "unlabeled")
        ):
            continue
        item = {
            "event": ev.get("event"),
            "created_at": when,
            "actor": ((ev.get("actor") or ev.get("user") or {}).get("login")),
        }
        if ev.get("label"):
            item["label"] = ev["label"].get("name")
        if ev.get("body"):
            item["body"], _ = redactor.text(ev["body"])
        if ev.get("event") == "commented" and ev.get("reactions"):
            item["reactions"] = {
                k: v for k, v in ev["reactions"].items() if isinstance(v, int) and v
            }
        out.append(item)
    return out


def labels_at(timeline: list[dict], at: str) -> list[str]:
    t = parse_ts(at)
    labels: set[str] = set()
    for ev in timeline:
        if parse_ts(ev.get("created_at")) > t:
            break
        if ev.get("event") == "labeled" and ev.get("label"):
            labels.add(ev["label"])
        elif ev.get("event") == "unlabeled" and ev.get("label"):
            labels.discard(ev["label"])
    return sorted(labels)


def flavor_of(labels: list[str]) -> str | None:
    for lab in labels:
        if lab.startswith("flavor:"):
            return lab.split(":", 1)[1]
    return None


# --- the cycle ---------------------------------------------------------------------


class Harvester:
    def __init__(  # noqa: PLR0913 — one collaborator per argument, built in main()
        self,
        cfg: Config,
        repo: str,
        gh: GitHub,
        store: Store,
        local: Local | LocalView,
        redactor: Redactor,
        dry_run: bool,
        backfill: bool = False,
        parent: Harvester | None = None,
    ):
        self.cfg = cfg
        self.repo = repo
        # parent set = an extra repository (class K) harvested inside the
        # primary's cycle: it shares the API client, the feeds and the
        # dispatch index, keeps its own cursor file, and never writes the
        # heartbeat (state/harvester.json) itself.
        self.parent = parent
        self.src = cfg.extra_source(repo) if parent else cfg.source(repo)
        self.short = self.src["short"]
        self.prefix = self.src.get("instance_prefix") or "instance-"
        self.gh = gh
        self.store = store
        self.local = local
        self.redactor = redactor
        self.dry_run = dry_run
        self.backfill = backfill
        self.feeds = parent.feeds if parent else Feeds(cfg)
        self.dispatches = (
            parent.dispatches
            if parent
            else Dispatches(
                [Path(str(entry["path"])).expanduser() for entry in cfg.runner_logs]
            )
        )
        self._issues: dict[int, dict | None] = {}
        self.runs_recorded = 0
        self.incidents_recorded = 0
        self.errors: list[str] = []
        self.cursor_rel = (
            f"state/cursors/{self.short}.json" if parent else "state/cursors.json"
        )
        self.cursor: str = (store.read_json(self.cursor_rel) or {}).get(
            "source_cursor"
        ) or ""
        self.cursor_moved = False
        self.extras: dict[str, Harvester] = {}
        self.unreadable: dict[str, str] = {}
        self.unmapped: set[str] = set()

    # -- helpers
    def issue(self, number: int) -> dict | None:
        if number not in self._issues:
            self._issues[number] = self.gh.get(f"/repos/{self.repo}/issues/{number}")
        return self._issues[number]

    def advance_cursor(self, created_at: str) -> None:
        if created_at > self.cursor:
            self.cursor = created_at
            self.cursor_moved = True

    # -- listings
    def list_runs(self, params: dict, pages: int = MAX_PAGES) -> list[dict]:
        return self.gh.paged(
            f"/repos/{self.repo}/actions/runs", "workflow_runs", params, pages
        )

    def list_window(self, start: float, end: float) -> list[dict]:
        """Every run created in [start, end]; a full page-cap answer splits."""
        runs = self.list_runs({"created": f"{to_iso(start)}..{to_iso(end)}"})
        if len(runs) >= MAX_PAGES * PAGE and end - start > MIN_WINDOW:
            mid = (start + end) / 2
            return self.list_window(start, mid) + self.list_window(mid + 1, end)
        return runs

    # -- one run
    def record(self, run: dict) -> bool:
        """Record a completed run (its current attempt). True when recorded."""
        if run.get("status") != "completed":
            return False
        workflow = workflow_file(run)
        kind = self.cfg.kind(workflow)
        if workflow not in self.cfg.kinds:
            self.unmapped.add(workflow)
        run_id = int(run["id"])
        attempt = int(run.get("run_attempt") or 1)
        created = run.get("created_at") or ""
        if self.store.recorded(created, run_id, attempt):
            return False
        result = base_result(run.get("conclusion"))
        d = (
            self.fetch_detail(run_id, attempt)
            if needs_detail(kind, result)
            else self.no_detail()
        )
        refusal = (
            is_refusal(self.cfg, d["failed"], d["logtext"], d["error_line"])
            if result == "failure"
            else None
        )
        if refusal:
            result = "refused"
        result, is_incident = finalize(self.cfg, workflow, kind, result, d["jobs"])

        ident = identify(run, d["logtext"], self.prefix, self.cfg, kind)
        issue_number = ident["issue"]
        issue = self.issue(issue_number) if issue_number else None
        issue_author = ((issue or {}).get("user") or {}).get("login")
        actor = actor_kind(
            self.cfg, run, self.repo, issue_number, issue_author, self.dispatches
        )
        current_labels = sorted(
            lab.get("name", "") for lab in (issue or {}).get("labels") or []
        )
        redacted, replacements = (
            self.redactor.text(d["logtext"]) if d["logtext"] is not None else (None, 0)
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": f"{self.short}-{run_id}-{attempt}" + ("-0" if is_incident else ""),
            "source": self.envelope(run, kind, d, actor),
            "command": ident["command"],
            "issue_number": issue_number,
            "issue_author": issue_author,
            "instance": {
                "name": ident["name"],
                "name_source": ident["name_source"],
                "flavor": flavor_of(current_labels),
            },
            "result": result,
            "refusal_reason": refusal,
            "signature": classify(
                self.cfg, d["error_line"], (d["failed"] or {}).get("step"), workflow
            )
            if result not in ("success", "skipped", "refused")
            else None,
            # From the redacted log, the text --rederive reads back.
            "capacity": capacity_of(
                redacted,
                d["error_line"],
                ident["command"],
                run.get("run_started_at") or created,
                flavor_of(current_labels),
            )
            if kind == "command" and result == "failure"
            else None,
            "markers": current_labels,
            "markers_source": "current",
            "jobs": [job_summary(j) for j in d["jobs"]],
        }

        # -- write (the mark comes last: a failure above leaves the run
        # unrecorded, and the next cycle or the hourly lookback retries it)
        if wants_outcome_line(kind, result):
            self.store.append_jsonl(
                f"outcomes/{day_path(created)}/{self.short}.jsonl", record
            )
        if is_incident:
            self.write_incident(record, redacted, d["step_tail"], replacements)
            self.incidents_recorded += 1
        elif redacted is not None:
            self.store.write_gz(
                f"logs/{day_path(created)}/{run_id}-{attempt}.log.gz", redacted
            )
        self.store.mark(created, run_id, attempt, workflow, result)
        self.runs_recorded += 1
        log(
            f"recorded {workflow} run {run_id}/{attempt} {result} {actor[0]}"
            + (f" issue={issue_number}" if issue_number else "")
            + (" INCIDENT" if is_incident else "")
        )
        return True

    def safe_record(self, run: dict) -> bool:
        """record() with the failure contained to this one run: it is logged,
        counted in state/harvester.json (last_error), left unrecorded for the
        next cycle or the lookback, and the cycle goes on with the next run."""
        try:
            return self.record(run)
        except Exception as e:
            detail = f"run {run.get('id')}/{run.get('run_attempt')}: {type(e).__name__}: {str(e)[:200]}"
            log(f"FAILED to record {detail}")
            traceback.print_exc()
            self.errors.append(detail)
            return False

    @staticmethod
    def no_detail() -> dict:
        return {
            "jobs": [],
            "failed": None,
            "logtext": None,
            "step_tail": "",
            "error_line": None,
            "log": "not_fetched",  # present | none | unreadable | not_fetched
        }

    def fetch_detail(self, run_id: int, attempt: int) -> dict:
        """The jobs (A2) and the full log (A3) of one attempt."""
        d = self.no_detail()
        d["jobs"] = self.gh.paged(
            f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "jobs",
        )
        d["failed"] = first_failed_step(d["jobs"])
        blob = self.gh.get(
            f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/logs",
            raw=True,
        )
        d["log"] = "none"
        if blob:
            try:
                d["logtext"], d["step_tail"], d["error_line"] = assemble_log(
                    blob, d["failed"]
                )
                d["log"] = "present" if d["logtext"] is not None else "none"
            except zipfile.BadZipFile:
                d["log"] = "unreadable"
                log(f"run {run_id} attempt {attempt}: log archive unreadable")
        # The error line goes into incident.json and the outcome line, so it
        # is redacted here; the log and the step tail are redacted when written.
        if d["error_line"]:
            d["error_line"], _ = self.redactor.text(d["error_line"])
        return d

    def envelope(self, run: dict, kind: str, d: dict, actor: tuple) -> dict:
        """The run envelope (A1) with what the jobs and the log added."""
        created = run.get("created_at") or ""
        started = run.get("run_started_at") or created
        updated = run.get("updated_at") or started
        failed = d["failed"] or {}
        return {
            "repo": self.repo,
            "workflow_file": workflow_file(run),
            "workflow_name": run.get("name"),
            "kind": kind,
            "run_id": int(run["id"]),
            "run_attempt": int(run.get("run_attempt") or 1),
            "event": run.get("event"),
            "actor": (run.get("actor") or {}).get("login"),
            "triggering_actor": (run.get("triggering_actor") or {}).get("login"),
            "actor_kind": actor[0],
            "actor_rule": actor[1],
            "runner": next(
                (j.get("runner_name") for j in d["jobs"] if j.get("runner_name")),
                None,
            ),
            "head_sha": run.get("head_sha"),
            "workflow_version": "unknown",
            "created_at": created,
            "run_started_at": run.get("run_started_at"),
            "updated_at": updated,
            "queued_seconds": max(0, int(parse_ts(started) - parse_ts(created))),
            "duration_seconds": max(0, int(parse_ts(updated) - parse_ts(started))),
            "conclusion": run.get("conclusion"),
            "failed_job": failed.get("job"),
            "failed_step": failed.get("step"),
            "error_line": d["error_line"],
            "html_url": run.get("html_url"),
            "facts": "missing",
            "log": d["log"],
            "backfill": self.backfill,
        }

    def write_incident(
        self,
        record: dict,
        redacted: str | None,
        step_tail: str,
        replacements: int,
    ) -> None:
        issue_number = record["issue_number"]
        created = record["source"]["created_at"]
        updated = record["source"]["updated_at"]
        base = f"incidents/{month_path(created)}/{record['id']}"
        start, end = parse_ts(created) - 3600, parse_ts(updated) + 3600
        timeline: list[dict] = []
        if issue_number:
            try:
                timeline = issue_timeline(
                    self.gh, self.repo, issue_number, self.redactor, start
                )
            except (urllib.error.URLError, OSError, ValueError) as e:
                log(f"timeline of #{issue_number} unavailable: {e}")
        if timeline:
            record["markers"] = labels_at(timeline, created)
            record["markers_source"] = "timeline"
            record["instance"]["flavor"] = (
                flavor_of(record["markers"]) or record["instance"]["flavor"]
            )
        record["owner_rule"] = owner_rule(record["markers"])
        flavor = record["instance"]["flavor"]
        context = {
            "grafana": grafana_vacancy(
                self.cfg, flavor, parse_ts(created), parse_ts(updated)
            ),
            "access": access_context(
                self.feeds.access(),
                self.cfg.access.get("resource_match", "jetstream"),
                start,
                end,
            ),
            "github_status": github_status_context(self.feeds.github(), start, end),
        }
        record["context"] = {
            "js2_status": context["access"]["status"],
            "github_status": context["github_status"]["status"],
            "js2_vacancy": (context["grafana"].get("points") or [[None, None]])[-1][1]
            if context["grafana"].get("status") == "ok"
            else context["grafana"].get("status"),
        }
        kind = record["source"]["kind"]
        record["admin_email"] = (
            {"sent": record["result"] == "success", "subject": None}
            if kind == "alert"
            else None
        )
        record["redaction"] = {
            "rules_version": RULES_VERSION,
            "email_hash": self.redactor.email_hash,
            "replacements": replacements,
        }
        record["signature_version"] = self.cfg.signature_version
        self.store.write_json(f"{base}/incident.json", record)
        if redacted is not None:
            self.store.write_gz(f"{base}/job.log.gz", redacted)
        if step_tail:
            tail, _ = self.redactor.text(step_tail)
            self.store.write_text(f"{base}/failed-step.log", tail)
        self.store.write_json(f"{base}/context.json", context)
        self.store.write_json(
            f"{base}/timeline.json",
            {
                "issue": issue_number,
                "since": to_iso(start),
                "events": timeline,
                "followups": [],
            },
        )
        name = record["instance"]["name"]
        if name and not self.backfill:
            snap = late_snapshot(name, updated)
            text, _ = self.redactor.text(json.dumps(snap, indent=1, sort_keys=True))
            self.store.write_text(f"{base}/late-snapshot.json", text + "\n")

    # -- the steps of a cycle
    def new_runs(self, now: float) -> None:
        if not self.cursor:
            back = EXTRA_FIRST_SIGHT if self.parent else OVERLAP
            self.cursor = to_iso(now - back)
            self.cursor_moved = True
            log(
                f"{self.repo}: no cursor, starting at {self.cursor} (run --backfill for history)"
            )
        start = parse_ts(self.cursor) - OVERLAP
        runs: list[dict] = []
        while start < now:
            end = min(start + WINDOW, now + 60)
            runs.extend(self.list_window(start, end))
            start = end + 1
        for run in runs:
            if too_fresh(run, now):  # next cycle; the 15-minute overlap relists it
                continue
            self.advance_cursor(run.get("created_at") or "")
            if run.get("status") == "completed":
                self.safe_record(run)
            else:
                self.local.data["watch"][str(run["id"])] = run.get("created_at")
        for status in ("queued", "in_progress"):
            for run in self.list_runs({"status": status}, pages=2):
                self.local.data["watch"][str(run["id"])] = run.get("created_at")

    def watch(self) -> None:
        for run_id in list(self.local.data["watch"]):
            run = self.gh.get(f"/repos/{self.repo}/actions/runs/{run_id}")
            if run is None:
                del self.local.data["watch"][run_id]
                continue
            if run.get("status") == "completed" and not too_fresh(run, time.time()):
                recorded = self.safe_record(run)
                if recorded or self.store.recorded(
                    run.get("created_at") or "",
                    int(run["id"]),
                    int(run.get("run_attempt") or 1),
                ):
                    del self.local.data["watch"][run_id]  # else: retried next cycle

    def lookback(self, now: float) -> None:
        if now - float(self.local.data.get("lookback_at") or 0) < LOOKBACK_EVERY:
            return
        missing = 0
        for run in self.list_window(now - LOOKBACK, now):
            if too_fresh(run, now):
                continue
            if run.get("status") == "completed" and self.safe_record(run):
                missing += 1
        self.local.data["lookback_at"] = now
        if missing:
            log(f"lookback recorded {missing} run(s) the cycles had missed")

    def followups(self, now: float) -> None:
        """H2: incidents younger than 24 h get the issue's new events and the
        later outcomes on the same issue. Rewritten only when changed."""
        months = {month_path(to_iso(now)), month_path(to_iso(now - FOLLOWUP_FOR))}
        for month in sorted(months):
            root = self.store.rel("incidents", month)
            if not root.is_dir():
                continue
            for inc_dir in sorted(root.iterdir()):
                if not inc_dir.name.startswith(self.short + "-"):
                    continue
                inc = self.store.read_json(
                    f"incidents/{month}/{inc_dir.name}/incident.json"
                )
                if not inc:
                    continue
                created = inc["source"]["created_at"]
                if now - parse_ts(created) > FOLLOWUP_FOR:
                    continue
                issue_number = inc.get("issue_number")
                rel = f"incidents/{month}/{inc_dir.name}/timeline.json"
                current = self.store.read_json(rel) or {
                    "issue": issue_number,
                    "events": [],
                    "followups": [],
                }
                updated = dict(current)
                if issue_number:
                    try:
                        updated["events"] = issue_timeline(
                            self.gh,
                            self.repo,
                            issue_number,
                            self.redactor,
                            parse_ts(created) - 3600,
                        )
                    except (urllib.error.URLError, OSError, ValueError) as e:
                        log(f"follow-up timeline of #{issue_number} unavailable: {e}")
                    updated["followups"] = self.later_outcomes(
                        issue_number, created, inc["id"]
                    )
                if updated != current:
                    self.store.write_json(rel, updated)

    def later_outcomes(
        self, issue_number: int, after: str, exclude_id: str
    ) -> list[dict]:
        out: list[dict] = []
        days = {day_path(after), day_path(to_iso(parse_ts(after) + 86400))}
        for day in sorted(days):
            p = self.store.rel("outcomes", day, f"{self.short}.jsonl")
            if not p.is_file():
                continue
            for line in p.read_text("utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (
                    rec.get("issue_number") != issue_number
                    or rec.get("id") == exclude_id
                ):
                    continue
                if rec["source"]["created_at"] <= after:
                    continue
                out.append(
                    {
                        "id": rec["id"],
                        "workflow_file": rec["source"]["workflow_file"],
                        "created_at": rec["source"]["created_at"],
                        "result": rec["result"],
                        "actor_kind": rec["source"]["actor_kind"],
                        "command": rec.get("command"),
                    }
                )
        return out

    def runner_logs(self, now: float) -> dict:
        """F1-F5: every new byte of each runner log; the error lines of the
        _diag files changed since the last cycle. Offsets live runner-side."""
        host = socket.gethostname()
        day = day_path(to_iso(now))
        offsets = self.local.data.setdefault("offsets", {})
        status: dict = {}
        for entry in self.cfg.runner_logs:
            p = Path(str(entry["path"])).expanduser()
            if not p.is_file():
                continue
            size = p.stat().st_size
            offset = int(offsets.get(entry["name"], 0))
            if size < offset:  # rotated or truncated: start over
                offset = 0
            if size == offset:
                continue
            with p.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
            # keep a partial last line for the next cycle
            cut = chunk.rfind(b"\n") + 1
            if cut == 0:
                continue
            text, _ = self.redactor.text(chunk[:cut].decode("utf-8", "replace"))
            for line_day, part in split_by_day(
                text.splitlines(keepends=True), day
            ).items():
                self.store.append_text(
                    f"runner/{line_day}/{host}/{entry['name']}.log", "".join(part)
                )
            offsets[entry["name"]] = offset + cut
            status[entry["name"]] = offset + cut
        if self.cfg.diag_dir:
            diag_offsets = self.local.data.setdefault("diag_offsets", {})
            lines = diag_new_lines(
                Path(self.cfg.diag_dir).expanduser(), diag_offsets, self.cfg.diag_match
            )
            if lines:
                self.ship_by_day(host, "diag", lines, day)
            control = self.src.get("control_host")
            if control:
                status["diag_remote"] = self.remote_diag(control, day)
        return status

    def ship_capped(self, rel: str, lines: list[str]) -> None:
        """Append redacted lines (_diag, host journals) to the day's file, up
        to DIAG_DAY_CAP: the chunk that crosses the cap is written with a
        marker line after it, and later chunks are dropped until the next
        day's file."""
        p = self.store.rel(rel)
        size = p.stat().st_size if p.is_file() else 0
        if size >= DIAG_DAY_CAP:
            return
        text, _ = self.redactor.text("".join(lines))
        if size + len(text.encode("utf-8", "replace")) > DIAG_DAY_CAP:
            text += (
                f"# capped: {DIAG_DAY_CAP} bytes for today; later lines are dropped\n"
            )
        self.store.append_text(rel, text)

    def ship_by_day(self, host: str, name: str, lines: list[str], today: str) -> None:
        """ship_capped() into the file of the day each line carries."""
        for day, part in split_by_day(lines, today).items():
            self.ship_capped(f"runner/{day}/{host}/{name}.log", part)

    def remote_diag(self, control: str, day: str) -> str:
        """The control runner's _diag error lines over SSH, from per-file
        offsets kept here (`diag_remote_offsets`), so a line is shipped once.
        The offsets advance only when the whole read succeeded; an unreachable
        host loses nothing, the next cycle reads from the same offsets."""
        offsets = self.local.data.setdefault("diag_remote_offsets", {})
        cases = "".join(
            f"    {shlex.quote(name)}) off={int(off)};;\n"
            for name, off in offsets.items()
        )
        # No offsets yet = first sight of this host: record where every file
        # ends and ship nothing, as diag_new_lines() does; history is not evidence.
        prime = "1" if not offsets else ""
        script = (
            f"prime={prime}\n"
            "cd ~/actions-runner/_diag || exit 3\nhostname\n"
            'for f in *; do\n  [ -f "$f" ] || continue\n'
            '  size=$(stat -c %s "$f"); off=0\n'
            f'  case "$f" in\n{cases}  esac\n'
            '  [ "$size" -lt "$off" ] && off=0\n'
            '  echo "==FILE $f $size"\n'  # every file, so a rotated one is forgotten
            '  [ -n "$prime" ] && continue\n'
            '  [ "$size" -eq "$off" ] && continue\n'
            f'  tail -c +$((off+1)) "$f" | grep -E {shlex.quote(self.cfg.diag_match.pattern)} || true\n'
            "done\n"
        )
        try:
            r = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    control,
                    "bash",
                    "-s",
                ],
                input=script,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return "unreachable"
        if r.returncode != 0:
            return "unreachable"
        lines = r.stdout.splitlines()
        host = lines[0].strip() if lines else control.split("@")[-1]
        # Only the files the listing names keep an offset: a rotated file is
        # forgotten, as diag_new_lines() does locally.
        new_offsets: dict[str, int] = {}
        body: list[str] = []
        header = None
        for line in lines[1:]:
            if line.startswith("==FILE "):
                name, size = line[len("==FILE ") :].rsplit(" ", 1)
                new_offsets[name] = int(size)
                header = f"# {name}\n"
            else:
                if header:
                    body.append(header)
                    header = None
                body.append(line + "\n")
        if body:
            self.ship_by_day(host, "diag", body, day)
        offsets.clear()
        offsets.update(new_offsets)
        return "ok"

    # -- class K: the other repositories this source lists -------------------
    def extra_repos(self) -> list[str]:
        """The repositories this source lists under `extra_repos` (production:
        the join health monitor). An explicit list, by the maintainer's
        decision of 2026-11-10: course repositories and repositories holding
        user data are not harvested, and the source token is scoped to
        exactly these repositories."""
        return [r for r in self.src.get("extra_repos") or [] if r != self.repo]

    def extra(self, repo: str) -> Harvester:
        if repo not in self.extras:
            src = self.cfg.extra_source(repo)
            self.extras[repo] = Harvester(
                self.cfg,
                repo,
                self.gh,
                Store(self.store.root, src["short"], self.dry_run),
                LocalView(self.local, repo),
                self.redactor,
                self.dry_run,
                backfill=self.backfill,
                parent=self,
            )
        return self.extras[repo]

    def extras_cycle(self, now: float) -> None:
        """Steps 1-4 and 6 of a cycle for each extra repository, each one
        contained: a repository that fails is named in state/harvester.json
        (`repos_unreadable`) and retried next cycle; the others go on."""
        for repo in self.extra_repos():
            x = self.extra(repo)
            try:
                # a repository the token cannot read answers 404, which the
                # run listing would take for "no runs": name it instead
                if self.gh.get(f"/repos/{repo}") is None:
                    msg = "not readable with the source token (404)"
                    raise LookupError(msg)
                x.new_runs(now)
                x.watch()
                x.lookback(now)
                x.followups(now)
                self.unreadable.pop(repo, None)
            except Exception as e:
                self.unreadable[repo] = f"{type(e).__name__}: {str(e)[:160]}"
                log(f"{repo}: not harvested this cycle: {self.unreadable[repo]}")

    # -- class I: the system journal of every server VM ----------------------
    def read_journal(
        self, host: dict, cursor: str
    ) -> tuple[list[str], str, str | None]:
        """(lines, new cursor, error)."""
        units = ",".join(host.get("units") or ["none"])
        return self.host_call(host, f"journal {cursor} {units}", "##CURSOR ")

    def host_call(
        self, host: dict, request: str, trailer: str
    ) -> tuple[list[str], str, str | None]:
        """One request to mc-journal-read.py: (lines, the trailer's value,
        error). The acquire runner runs it locally; every other host through
        the read-only account, whose forced command it is."""
        target = host.get("ssh") or "local"
        if target == "local":
            cmd = [sys.executable, str(JOURNAL_READER)]
            env = {**os.environ, "SSH_ORIGINAL_COMMAND": request}
        else:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=15", target, request]  # fmt: skip
            env = None
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=JOURNAL_TIMEOUT,
                check=False,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return [], "", f"{type(e).__name__}"
        out = r.stdout.splitlines(keepends=True)
        if r.returncode != 0 or not out or not out[-1].startswith(trailer):
            return [], "", (r.stderr or f"exit {r.returncode}").strip()[-200:]
        return out[:-1], out[-1][len(trailer) :].strip(), None

    def host_files(self, host: dict, st: dict, today: str) -> None:
        """Our services' own log files on the host (`files:`), each from its
        saved offset, appended to the file of that name for the day each
        line carries (today for undated lines)."""
        offsets = st.setdefault("files", {})
        for name in host.get("files") or {}:
            at = offsets.get(name)
            lines, new, err = self.host_call(
                host, f"file {name} {'-' if at is None else int(at)}", "##OFFSET "
            )
            if err:
                log(f"{host['name']} file {name}: {err}")
                continue
            if lines:
                self.ship_by_day(host["name"], name, lines, today)
            offsets[name] = int(new)

    def journal_keep(self, st: dict, day: str, line: str) -> bool:
        """False for a line that is summarized (firewall blocks) or counted
        (the noise list) instead of stored."""
        if "[UFW BLOCK]" in line:
            u = st["ufw"].setdefault(day, {"total": 0, "dpt": {}, "src": {}})
            u["total"] += 1
            if m := UFW_DPT_RE.search(line):
                u["dpt"][m[1]] = u["dpt"].get(m[1], 0) + 1
            if m := UFW_SRC_RE.search(line):
                net = f"{m[1]}.0/24"
                u["src"][net] = u["src"].get(net, 0) + 1
            return False
        for name, rx in self.cfg.journal_noise:
            if rx.search(line):
                n = st["noise"].setdefault(day, {})
                n[name] = n.get(name, 0) + 1
                return False
        return True

    def journal_summaries(self, host: str, st: dict, upto: str) -> None:
        """One line per day read to its end: firewall blocks (count, top
        ports, top source networks) and what each noise filter removed, so a
        scan stays visible and a filter that grows greedy shows. `upto` is the
        day of the last line read (today once caught up): a day is summarized
        only when the read has moved past it, so a day read across several
        calls (a first sight) gets one complete line, not several partial ones."""
        days = sorted(set(st["ufw"]) | set(st["noise"]))
        for day in [d for d in days if d < upto]:
            u = st["ufw"].pop(day, None) or {"total": 0, "dpt": {}, "src": {}}
            n = st["noise"].pop(day, None) or {}

            def top(counts: dict) -> str:
                ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:TOP_N]
                return ",".join(f"{k}x{v}" for k, v in ranked) or "-"

            noise = ",".join(f"{k}={v}" for k, v in sorted(n.items())) or "-"
            self.store.append_text(
                f"runner/{day}/{host}/journal.log",
                f"# daily summary {day}: firewall blocks {u['total']}; top ports "
                f"{top(u['dpt'])}; top sources {top(u['src'])}; noise removed {noise}\n",
            )

    def journals(self, now: float) -> dict:
        """Class I for each host this source lists: new journal lines since
        the saved cursor, filtered, redacted, appended to the day's file of
        the day they were logged (capped like _diag). A host that cannot be
        read is a line in its file when it goes down and when it comes back,
        and `unreachable since <time>` in state/harvester.json (the watchdog
        alerts after two hours)."""
        status: dict = {}
        today = day_path(to_iso(now))
        states = self.local.data.setdefault("journal", {})
        for host in self.src.get("hosts") or []:
            name = host["name"]
            st = states.setdefault(
                name, {"cursor": "-", "up": True, "since": "", "ufw": {}, "noise": {}}
            )
            lines, cursor, err = self.read_journal(host, st["cursor"])
            rel = f"runner/{today}/{name}/journal.log"
            if err:
                if st["up"]:
                    st.update(up=False, since=to_iso(now))
                    self.store.append_text(rel, f"# {to_iso(now)} unreachable: {err}\n")
                status[name] = f"unreachable since {st['since']}"
                log(f"journal of {name}: {err}")
                continue
            if not st["up"]:
                self.store.append_text(
                    rel, f"# {to_iso(now)} reachable again (down since {st['since']})\n"
                )
                st.update(up=True, since=to_iso(now))
            by_day: dict[str, list[str]] = {}
            for line in lines:
                day = day_path(line) if line[:4].isdigit() else today
                if self.journal_keep(st, day, line):
                    by_day.setdefault(day, []).append(line)
            for day, kept in sorted(by_day.items()):
                self.ship_capped(f"runner/{day}/{name}/journal.log", kept)
            st["cursor"] = cursor
            read_to = max((day_path(x) for x in lines if x[:4].isdigit()), default=None)
            # caught up (nothing new) means every earlier day is complete
            self.journal_summaries(name, st, read_to or today)
            self.host_files(host, st, today)
            status[name] = "ok"
        return status

    # -- class J: platform state of the server VMs -----------------------------
    def platform(self, now: float) -> str:
        """Each listed host's Nova state, through the read-only wrapper; a
        line in platform/<day>.jsonl on every change, nothing otherwise."""
        names = {
            h["nova"]: h["name"] for h in self.src.get("hosts") or [] if h.get("nova")
        }
        if not names:
            return "no hosts"
        if not os.environ.get("OS_CLOUD"):
            return "OS_CLOUD not set"
        data = ro_json("server", "list", "--long")
        if not isinstance(data, list):
            return f"unavailable: {str((data or {}).get('error'))[:160]}"
        seen = {d.get("Name"): d for d in data}
        previous = self.store.read_json("state/platform.json") or {}
        current = dict(previous)
        at = to_iso(now)
        for nova in sorted(names):
            d = seen.get(nova)
            state = (
                {
                    "status": d.get("Status"),
                    "task_state": d.get("Task State"),
                    "power_state": d.get("Power State"),
                }
                if d
                else {"status": "MISSING"}
            )
            before = (previous.get(nova) or {}).get("state")
            if before != state:
                self.store.append_jsonl(
                    f"platform/{day_path(at)}.jsonl",
                    {
                        "at": at,
                        "vm": nova,
                        "host": names[nova],
                        "from": before,
                        "to": state,
                    },
                )
                current[nova] = {"state": state, "since": at}
        if current != previous:
            self.store.write_json("state/platform.json", current)
        return "ok"

    # -- the end of a cycle ---------------------------------------------------
    def finish(
        self, now: float, started: float, error: str | None, extra: dict
    ) -> None:
        """state/cursors.json and state/harvester.json, written only when
        something moved (so an idle store means a stopped harvester or clock).
        An extra repository harvested on its own (--backfill --repo) writes
        only its cursor and commits."""
        if self.parent is not None:
            # --backfill --repo: keep what it saw for the primary's next cycle
            unmapped_note(self.cfg, self.unmapped, self.parent.local.data)
            if self.cursor_moved:
                self.store.write_json(
                    self.cursor_rel, {"source_cursor": self.cursor, "repo": self.repo}
                )
            self.store.commit_push(
                f"harvest {to_iso(now)} {self.repo} runs={self.runs_recorded} incidents={self.incidents_recorded}"
            )
            return
        extras = list(self.extras.values())
        runs = self.runs_recorded + sum(x.runs_recorded for x in extras)
        incidents = self.incidents_recorded + sum(x.incidents_recorded for x in extras)
        errors = self.errors + [e for x in extras for e in x.errors]
        if errors and not error:
            error = f"{len(errors)} run(s) could not be recorded; first: {errors[0]}"
        note = unmapped_note(
            self.cfg,
            self.unmapped.union(*(x.unmapped for x in extras)),
            self.local.data,
        )
        error = "; ".join(e for e in (error, note) if e) or None
        for x in extras:
            if x.cursor_moved:
                self.store.write_json(
                    x.cursor_rel, {"source_cursor": x.cursor, "repo": x.repo}
                )
            x.store.flush()
        changed = (
            self.cursor_moved
            or runs
            or self.store.writes
            or any(x.store.writes or x.cursor_moved for x in extras)
        )
        previous = self.store.read_json("state/harvester.json") or {}
        if error != previous.get("last_error"):
            changed = True
        if self.cursor_moved:
            self.store.write_json(
                "state/cursors.json", {"source_cursor": self.cursor, "repo": self.repo}
            )
        if changed:
            self.store.write_json(
                "state/harvester.json",
                {
                    "last_cycle": to_iso(now),
                    "cursor": self.cursor,
                    "last_error": error,
                    "repo": self.repo,
                    "host": socket.gethostname(),
                    "cycle_seconds": round(time.time() - started, 1),
                    "api_calls": self.gh.calls,
                    "rate_limit_remaining": self.gh.remaining,
                    "runs_recorded": runs,
                    "incidents_recorded": incidents,
                    "runs_failed": len(errors),
                    "watching": len(self.local.data.get("watch") or {}),
                    "repos": {x.repo: x.cursor for x in extras},
                    "repos_unreadable": self.unreadable,
                    **extra,
                },
            )
        self.local.save()
        if self.store.commit_push(
            f"harvest {to_iso(now)} runs={runs} incidents={incidents}"
            + (" ERROR" if error else "")
        ):
            log(
                f"committed: runs={runs} incidents={incidents} api_calls={self.gh.calls}"
            )

    def cycle(self) -> int:
        """The steps in order, each contained: a step that raises is named in
        last_error and the later steps still run (a dead host must not stop
        the runs being recorded, nor the other way round)."""
        started = time.time()
        now = started
        errors: list[str] = []
        extra: dict = {}

        def step(name: str, fn):
            try:
                return fn()
            except Exception as e:
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:240]}")
                log(f"cycle step {name} failed: {errors[-1]}")
                traceback.print_exc()
                return None

        step("runs", lambda: (self.new_runs(now), self.watch(), self.lookback(now)))
        step("followups", lambda: self.followups(now))
        extra.update(step("runner_logs", lambda: self.runner_logs(now)) or {})
        step("repositories", lambda: self.extras_cycle(now))
        hosts = step("journals", lambda: self.journals(now))
        if hosts:
            extra["hosts"] = hosts
        plat = step("platform", lambda: self.platform(now))
        if plat:
            extra["platform"] = plat
        error = "; ".join(errors) or None
        self.finish(now, started, error, extra)
        return 1 if error else 0

    def run_backfill(self, days: int) -> int:
        """The last `days` of runs, oldest first, one commit per day of data."""
        now = time.time()
        start = now - days * 86400
        current_day = None
        window_start = start
        while window_start < now:
            window_end = min(window_start + WINDOW, now)
            runs = self.list_window(window_start, window_end)
            for run in sorted(runs, key=lambda r: r.get("created_at") or ""):
                day = day_path(run.get("created_at") or "")
                if current_day and day != current_day:
                    self.store.commit_push(
                        f"harvest backfill {current_day} runs={self.runs_recorded} incidents={self.incidents_recorded}"
                    )
                current_day = day
                if run.get("status") == "completed":
                    self.safe_record(run)
                else:
                    self.local.data["watch"][str(run["id"])] = run.get("created_at")
            log(
                f"backfill {to_iso(window_start)}..{to_iso(window_end)}: {len(runs)} runs, "
                f"total recorded {self.runs_recorded}, incidents {self.incidents_recorded}, "
                f"api calls {self.gh.calls}, remaining {self.gh.remaining}"
            )
            window_start = window_end + 1
        if not self.cursor or self.cursor < to_iso(now):
            self.cursor = to_iso(now - OVERLAP)
            self.cursor_moved = True
        self.finish(now, now, None, {"backfill_days": days})
        return 0


def diag_new_lines(diag_dir: Path, offsets: dict, pattern: re.Pattern) -> list[str]:
    """The matching lines added to each _diag file since its offset (updated
    in place), so a line is shipped once however often the file is touched.
    No offsets at all = the first cycle on this runner: every file's end is
    recorded and nothing is shipped (the directory holds months of history that
    is not evidence of anything current; it cost 81 MB on Test the first time).
    A file shorter than its offset starts over; a file that is gone is forgotten."""
    out: list[str] = []
    if not diag_dir.is_dir():
        return out
    prime = not offsets
    seen: set[str] = set()
    for p in sorted(diag_dir.iterdir()):
        try:
            if not p.is_file():
                continue
            seen.add(p.name)
            size = p.stat().st_size
            if prime:
                offsets[p.name] = size
                continue
            off = int(offsets.get(p.name, 0))
            if size < off:
                off = 0
            if size == off:
                continue
            with p.open("rb") as fh:
                fh.seek(off)
                chunk = fh.read(size - off)
        except OSError:
            continue
        cut = chunk.rfind(b"\n") + 1
        if cut == 0:
            continue
        hits = [
            line
            for line in chunk[:cut].decode("utf-8", "replace").splitlines()
            if pattern.search(line)
        ]
        if hits:
            out.append(f"# {p.name}\n")
            out.extend(h + "\n" for h in hits)
        offsets[p.name] = off + cut
    for name in [n for n in offsets if n not in seen]:
        del offsets[name]
    return out


def too_fresh(run: dict, now: float) -> bool:
    """Created within GRACE seconds of now: its DISPATCH line may not be
    written yet, so it is left for the next cycle (plan, actor_kind rule 1)."""
    return now - parse_ts(run.get("created_at")) < GRACE


def job_summary(job: dict) -> dict:
    return {
        "name": job.get("name"),
        "conclusion": job.get("conclusion"),
        "runner": job.get("runner_name"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "steps": [
            {
                "name": s.get("name"),
                "conclusion": s.get("conclusion"),
                "number": s.get("number"),
            }
            for s in job.get("steps") or []
        ],
    }


def owner_rule(markers: list[str]) -> str | None:
    if any(m == "followup:setup" for m in markers):
        return "setup-watch"
    if any(m.startswith("followup:") for m in markers):
        return "follow-through"
    if "invariant-violation" in markers:
        return "reconcile"
    return None


# --- --rederive: today's rules over what is already stored -------------------------------


def stored_log(root: Path, rec: dict) -> str | None:
    """The run's log as the store holds it (an incident's job.log.gz, or
    logs/<day>/<run>-<attempt>.log.gz), or None."""
    s = rec["source"]
    created = s.get("created_at") or ""
    for rel in (
        f"incidents/{month_path(created)}/{rec['id']}/job.log.gz",
        f"logs/{day_path(created)}/{s['run_id']}-{s.get('run_attempt', 1)}.log.gz",
    ):
        p = root / rel
        if p.is_file():
            return gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
    return None


def stored_step_tail(root: Path, rec: dict) -> str | None:
    """An incident's failed-step.log (the failed step's last lines), or None."""
    created = rec["source"].get("created_at") or ""
    p = root / f"incidents/{month_path(created)}/{rec['id']}/failed-step.log"
    return p.read_text("utf-8", "replace") if p.is_file() else None


def rederive_record(
    cfg: Config, rec: dict, logtext: str | None, step_tail: str | None = None
) -> dict:
    """The record as record() would write it today, from what it stored:
    the kind, the command (from the stored log; unchanged without one), an
    error line that is only the runner's "exit code" line (from the failed
    step's stored tail, else the whole log), refused-versus-failure in both
    directions, and the capacity facts. A record that would now become an
    incident keeps its id (an incident needs the context only a fresh harvest
    can gather)."""
    new = json.loads(json.dumps(rec))
    s = new["source"]
    workflow = s["workflow_file"]
    s["kind"] = cfg.kind(workflow)
    if logtext is not None:
        new["command"] = command_of(logtext, workflow, cfg)
    if GENERIC_EXIT_RE.match(s.get("error_line") or ""):
        source = step_tail if step_tail is not None else logtext
        better = pick_error(ERROR_RE.findall(source)) if source is not None else None
        if (better is None or GENERIC_EXIT_RE.match(better)) and logtext:
            # The stored tail is the step's last TAIL_LINES lines; a reason
            # above them is the last error before the run's first exit-code
            # line (the line that closes the first failed step).
            first_exit = re.search(
                r"##\[error\]Process completed with exit code", logtext
            )
            if first_exit:
                better = (
                    pick_error(ERROR_RE.findall(logtext[: first_exit.start()]))
                    or better
                )
        if better and better != s["error_line"]:
            s["error_line"] = better
    if new["result"] in ("failure", "refused"):
        failed = (
            {"job": s.get("failed_job"), "step": s.get("failed_step")}
            if s.get("failed_job") or s.get("failed_step")
            else None
        )
        reason = is_refusal(cfg, failed, logtext, s.get("error_line"))
        if reason:
            new.update(result="refused", refusal_reason=reason, signature=None)
        elif new["result"] == "refused":
            new.update(result="failure", refusal_reason=None)
    if new["result"] == "failure" and (
        new.get("signature") is None
        or s.get("error_line") != rec["source"].get("error_line")
    ):
        new["signature"] = classify(
            cfg, s.get("error_line"), s.get("failed_step"), workflow
        )
    new["capacity"] = (
        capacity_of(
            logtext,
            s.get("error_line"),
            new.get("command"),
            s.get("run_started_at") or s.get("created_at"),
            (new.get("instance") or {}).get("flavor"),
        )
        if s["kind"] == "command" and new["result"] == "failure"
        else None
    )
    result, incident = finalize(
        cfg, workflow, s["kind"], new["result"], new.get("jobs") or []
    )
    new["result"] = result
    was_incident = rec["id"].endswith("-0")
    if was_incident and not incident:
        new["id"] = rec["id"].removesuffix("-0")
    return new


class Rederive:
    """Apply today's rules to everything already stored, in four passes:
    outcome lines, incident directories (a run that is no longer an incident
    keeps its log under logs/), the follow-up lists that name an outcome, and
    the runner logs (each line into the day it carries). Says what changed;
    a dry run writes nothing. Running it twice changes nothing the second time."""

    def __init__(self, cfg: Config, root: Path, dry_run: bool):
        self.cfg = cfg
        self.root = root
        self.dry_run = dry_run
        self.w = Store(root, "rederive", dry_run)
        self.rollups: dict[str, Store] = {}
        self.changes: dict[str, int] = {}
        self.new_by_id: dict[str, dict] = {}

    def count(self, what: str, n: int = 1) -> None:
        self.changes[what] = self.changes.get(what, 0) + n

    def diff(self, old: dict, new: dict) -> None:
        for label, get in (
            ("command", lambda r: r.get("command")),
            ("kind", lambda r: r["source"]["kind"]),
            ("result", lambda r: r["result"]),
        ):
            if get(old) != get(new):
                self.count(f"{label} {get(old)} -> {get(new)}")
        if old["source"].get("error_line") != new["source"].get("error_line"):
            self.count("error_line: the step's own error, not the exit-code line")
        if old.get("refusal_reason") != new.get("refusal_reason"):
            self.count(
                f"refusal_reason {old.get('refusal_reason')} -> {new.get('refusal_reason')}"
            )
        cap, was = new.get("capacity"), old.get("capacity")
        if cap != was:
            self.count(
                f"capacity {cap['cause']} ({cap['evidence']})"
                if cap
                else "capacity -> null"
            )

    def outcomes(self) -> None:
        for p in sorted(self.root.glob("outcomes/*/*/*/*.jsonl")):
            kept: list[str] = []
            dirty = False
            for line in p.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                new = rederive_record(
                    self.cfg,
                    rec,
                    stored_log(self.root, rec),
                    stored_step_tail(self.root, rec),
                )
                self.new_by_id[rec["id"]] = new
                if new == rec:
                    kept.append(line)
                    continue
                dirty = True
                self.diff(rec, new)
                if new["result"] != rec["result"]:
                    roll = self.rollups.setdefault(
                        p.stem, Store(self.root, p.stem, self.dry_run)
                    )
                    roll.recount(
                        rec["source"]["created_at"],
                        rec["source"]["workflow_file"],
                        rec["result"],
                        new["result"],
                    )
                if wants_outcome_line(new["source"]["kind"], new["result"]):
                    kept.append(json.dumps(new, sort_keys=True))
                else:
                    self.count(
                        "outcome line dropped (a count only under today's rules)"
                    )
            rel = str(p.relative_to(self.root))
            if dirty and kept:
                self.w.write_text(rel, "".join(k + "\n" for k in kept))
            elif dirty:
                self.w.remove(rel)

    def incidents(self) -> None:
        for p in sorted(self.root.glob("incidents/*/*/*/incident.json")):
            rec = json.loads(p.read_text("utf-8"))
            rel_dir = str(p.parent.relative_to(self.root))
            new = rederive_record(
                self.cfg,
                rec,
                stored_log(self.root, rec),
                stored_step_tail(self.root, rec),
            )
            if new["id"] == rec["id"]:
                if new != rec:
                    self.w.write_json(f"{rel_dir}/incident.json", new)
                    self.count("incident.json rewritten")
                continue
            s = rec["source"]
            log_gz = p.parent / "job.log.gz"
            if log_gz.is_file():
                self.w.write_bytes(
                    f"logs/{day_path(s['created_at'])}/{s['run_id']}-{s.get('run_attempt', 1)}.log.gz",
                    log_gz.read_bytes(),
                )
            self.w.remove(rel_dir)
            self.count(
                "incident directory removed (no longer an incident; log kept under logs/)"
            )

    def followups(self) -> None:
        for p in sorted(self.root.glob("incidents/*/*/*/timeline.json")):
            data = json.loads(p.read_text("utf-8"))
            changed = False
            for fu in data.get("followups") or []:
                new = self.new_by_id.get(fu.get("id"))
                if not new:
                    continue
                for key, value in (
                    ("id", new["id"]),
                    ("command", new.get("command")),
                    ("result", new["result"]),
                ):
                    if fu.get(key) != value:
                        fu[key] = value
                        changed = True
            if changed:
                self.w.write_json(str(p.relative_to(self.root)), data)

    def runner_logs(self) -> None:
        targets: dict[str, list[str]] = {}
        seen: set[str] = set()
        for p in sorted(self.root.glob("runner/*/*/*/*/*.log")):
            if p.name == "journal.log":  # filed by line day since it was written
                continue
            rel = p.relative_to(self.root)
            seen.add(str(rel))
            folder = "/".join(rel.parts[1:4])
            lines = p.read_text("utf-8", "replace").splitlines(keepends=True)
            for day, part in split_by_day(lines, folder).items():
                if day != folder:
                    self.count("runner log line re-filed under its own day", len(part))
                target = f"runner/{day}/{p.parent.name}/{p.name}"
                targets.setdefault(target, []).extend(part)
        for rel in sorted(set(targets) | seen):
            text = "".join(targets.get(rel, []))
            p = self.root / rel
            if p.is_file() and p.read_text("utf-8", "replace") == text:
                continue
            if text:
                self.w.write_text(rel, text)
            else:
                self.w.remove(rel)

    def run(self) -> int:
        self.outcomes()
        self.incidents()
        self.followups()
        self.runner_logs()
        for st in self.rollups.values():
            st.flush()
        for what, n in sorted(self.changes.items()):
            log(f"rederive: {n:6d}  {what}")
        if not self.changes:
            log("rederive: nothing to change")
        elif not self.dry_run:
            self.w.commit_push(f"rederive: {sum(self.changes.values())} change(s)")
        return 0


def rederive(cfg: Config, root: Path, dry_run: bool) -> int:
    return Rederive(cfg, root, dry_run).run()


# --- main -------------------------------------------------------------------------------


def main(argv: list[str]) -> int:  # noqa: PLR0911 — one early exit per usage error
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--dry-run", action="store_true", help="read and classify, write nothing"
    )
    ap.add_argument(
        "--backfill", type=int, metavar="DAYS", help="record the last DAYS days once"
    )
    ap.add_argument(
        "--store",
        type=Path,
        default=None,
        help="the store checkout (default ~/incidents-store)",
    )
    ap.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="with --backfill: one of the source's `extra_repos` (class K) instead of the source",
    )
    ap.add_argument(
        "--rederive",
        action="store_true",
        help="apply today's rules to the records and runner logs already stored",
    )
    args = ap.parse_args(argv)
    if args.rederive and args.backfill:
        print("--rederive and --backfill are separate runs", file=sys.stderr)  # noqa: T201 — CLI usage error
        return 2
    if args.repo and not args.backfill:
        print("--repo needs --backfill", file=sys.stderr)  # noqa: T201 — CLI usage error
        return 2

    repo = os.environ.get("MORPHOCLOUD_SOURCE_REPO", "")
    if not repo:
        print("MORPHOCLOUD_SOURCE_REPO is not set", file=sys.stderr)  # noqa: T201 — CLI usage error
        return 2
    store_dir = (
        args.store
        or Path(
            os.environ.get("MORPHOCLOUD_STORE_DIR", "~/incidents-store")
        ).expanduser()
    )
    if not (store_dir / ".git").is_dir():
        print(f"{store_dir} is not a git checkout of the store", file=sys.stderr)  # noqa: T201
        return 2
    if not SOURCE_PAT.is_file():
        print(f"{SOURCE_PAT} missing", file=sys.stderr)  # noqa: T201
        return 2

    CFG_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(
            "another harvest holds the lock (a backfill, or the previous cycle); skipping this tick"
        )
        return 0

    cfg = load_config()
    if args.rederive:
        try:
            log(f"rederive {store_dir}{' (dry run)' if args.dry_run else ''}")
            return rederive(cfg, store_dir, args.dry_run)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
    token = SOURCE_PAT.read_text("utf-8").strip()
    gh = GitHub(token, pace=bool(args.backfill))
    store = Store(store_dir, cfg.source(repo)["short"], args.dry_run)
    local = Local(LOCAL_STATE, args.dry_run)
    h = Harvester(
        cfg,
        repo,
        gh,
        store,
        local,
        load_redactor(),
        args.dry_run,
        backfill=bool(args.backfill),
    )
    try:
        if args.backfill and args.repo:
            log(f"backfill {args.backfill} days of {args.repo} into {store_dir}")
            x = h.extra(args.repo)
            rc = x.run_backfill(args.backfill)
            local.save()
            return rc
        if args.backfill:
            log(f"backfill {args.backfill} days of {repo} into {store_dir}")
            return h.run_backfill(args.backfill)
        return h.cycle()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
