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
        self.commands = list(sources.get("commands") or [])
        self.command_by_workflow = dict(sources.get("command_by_workflow") or {})
        self.runner_logs = list(sources.get("runner_logs") or [])
        self.diag_dir = sources.get("diag_dir")
        self.diag_match = re.compile(sources.get("diag_match") or "ERR")
        self.grafana = sources.get("grafana") or {}
        self.access = sources.get("access_outages") or {}
        self.github_status = sources.get("github_status")
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

    def kind(self, workflow_file: str) -> str:
        return self.kinds.get(workflow_file, "other")


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


# --- what one run is -------------------------------------------------------


def base_result(conclusion: str | None) -> str:
    if conclusion in ("timed_out", "startup_failure"):
        return "failure"
    return conclusion or "unknown"


def needs_detail(kind: str, result: str) -> bool:
    """Jobs and the full log: every command and alert run that ran, and every
    run that did not succeed. A skipped run is a count only (A1)."""
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
    if result == "success":
        return result, kind == "alert"
    if result in ("skipped", "refused"):
        return result, False
    return result, True


def wants_outcome_line(kind: str, result: str) -> bool:
    """One line per command run and per run that did not succeed; skipped
    runs and successful sweeps are rollup counts."""
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


def is_refusal(cfg: Config, failed: dict | None, logtext: str | None) -> str | None:
    """The refusal reason, or None when the failure is real."""
    if logtext:
        m = REFUSED_RE.search(logtext)
        if m:
            return m.group(1)
    if failed:
        if failed.get("step") in cfg.refusal_steps:
            return failed["step"]
        if failed.get("job") in cfg.refusal_jobs:
            return f"job:{failed['job']}"
    return None


def _plain(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


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
                errors = ERROR_RE.findall(text)
                if errors:
                    error_line = TS_PREFIX_RE.sub("", errors[-1]).strip()
    whole = "".join(parts)
    if error_line is None:
        errors = ERROR_RE.findall(whole)
        if errors:
            error_line = TS_PREFIX_RE.sub("", errors[-1]).strip()
    return whole, step_tail, error_line


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
    if logtext:
        cm = re.search(r"command_name=(\w+)", logtext)
        if cm:
            command = "/" + cm.group(1)
        elif cfg.commands:
            words = "|".join(re.escape(c) for c in cfg.commands)
            cm = re.search(r"(?<![\w/])/(" + words + r")\b", logtext)
            if cm:
                command = "/" + cm.group(1)
    if name is None and issue is not None and kind == "command":
        name = f"{prefix}{issue}"
        name_source = "derived"
    if command is None:
        command = cfg.command_by_workflow.get(workflow_file(run))
    return {
        "issue": issue,
        "name": name,
        "name_source": name_source,
        "command": command,
    }


def workflow_file(run: dict) -> str:
    return (run.get("path") or "").rsplit("/", 1)[-1]


# --- actor_kind --------------------------------------------------------------


class Dispatches:
    """The runner-side DISPATCH lines (and the pre-PR A `dispatched <wf>`
    lines, for the backfill), indexed by workflow, for rule 1."""

    def __init__(self, paths: list[Path]):
        self.by_wf: dict[str, list[tuple[float, str | None, str | None]]] = {}
        for p in paths:
            if not p.is_file():
                continue
            try:
                with p.open("rb") as fh:
                    fh.seek(max(0, p.stat().st_size - 4_000_000))
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
        for when, line_repo, line_issue in self.by_wf.get(workflow, []):
            if abs(when - t) > DISPATCH_JOIN:
                continue
            if line_repo and line_repo != repo:
                continue
            if line_issue and issue is not None and str(issue) != line_issue:
                continue
            return True
        return False


def actor_kind(  # noqa: PLR0913 — the plan's five rules need these five inputs
    cfg: Config,
    run: dict,
    repo: str,
    issue: int | None,
    issue_author: str | None,
    dispatches: Dispatches,
) -> tuple[str, str | None]:
    """The plan's five rules, in order. Returns (kind, rule)."""
    actor = (run.get("actor") or {}).get("login") or ""
    trigger = (run.get("triggering_actor") or {}).get("login") or ""
    if run.get("event") == "workflow_dispatch" and dispatches.match(
        workflow_file(run), repo, run.get("created_at") or "", issue
    ):
        return "automation", "dispatch-line"
    if actor in cfg.bots or trigger in cfg.bots:
        return "automation", "bot"
    if actor in cfg.test_accounts or trigger in cfg.test_accounts:
        return "test", "test-account"
    if actor in cfg.admins or trigger in cfg.admins:
        login = trigger or actor
        if issue_author and login == issue_author:
            return "member", "admin-own-issue"
        return "admin", "admin"
    return "member", "default"


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
        local: Local,
        redactor: Redactor,
        dry_run: bool,
        backfill: bool = False,
    ):
        self.cfg = cfg
        self.repo = repo
        self.src = cfg.source(repo)
        self.short = self.src["short"]
        self.prefix = self.src.get("instance_prefix") or "instance-"
        self.gh = gh
        self.store = store
        self.local = local
        self.redactor = redactor
        self.dry_run = dry_run
        self.backfill = backfill
        self.feeds = Feeds(cfg)
        self.dispatches = Dispatches(
            [Path(str(entry["path"])).expanduser() for entry in cfg.runner_logs]
        )
        self._issues: dict[int, dict | None] = {}
        self.runs_recorded = 0
        self.incidents_recorded = 0
        self.errors: list[str] = []
        self.cursor: str = (store.read_json("state/cursors.json") or {}).get(
            "source_cursor"
        ) or ""
        self.cursor_moved = False

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
            is_refusal(self.cfg, d["failed"], d["logtext"])
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
        redacted, replacements = (
            self.redactor.text(d["logtext"]) if d["logtext"] is not None else (None, 0)
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
        if blob:
            try:
                d["logtext"], d["step_tail"], d["error_line"] = assemble_log(
                    blob, d["failed"]
                )
            except zipfile.BadZipFile:
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
            self.cursor = to_iso(now - OVERLAP)
            self.cursor_moved = True
            log(f"no cursor: starting at {self.cursor} (run --backfill for history)")
        start = parse_ts(self.cursor) - OVERLAP
        runs: list[dict] = []
        while start < now:
            end = min(start + WINDOW, now + 60)
            runs.extend(self.list_window(start, end))
            start = end + 1
        for run in runs:
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
            if run.get("status") == "completed":
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
            self.store.append_text(f"runner/{day}/{host}/{entry['name']}.log", text)
            offsets[entry["name"]] = offset + cut
            status[entry["name"]] = offset + cut
        if self.cfg.diag_dir:
            diag_offsets = self.local.data.setdefault("diag_offsets", {})
            lines = diag_new_lines(
                Path(self.cfg.diag_dir).expanduser(), diag_offsets, self.cfg.diag_match
            )
            if lines:
                text, _ = self.redactor.text("".join(lines))
                self.store.append_text(f"runner/{day}/{host}/diag.log", text)
            control = self.src.get("control_host")
            if control:
                status["diag_remote"] = self.remote_diag(control, day)
        return status

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
        script = (
            "cd ~/actions-runner/_diag || exit 3\nhostname\n"
            'for f in *; do\n  [ -f "$f" ] || continue\n'
            '  size=$(stat -c %s "$f"); off=0\n'
            f'  case "$f" in\n{cases}  esac\n'
            '  [ "$size" -lt "$off" ] && off=0\n  [ "$size" -eq "$off" ] && continue\n'
            '  echo "==FILE $f $size"\n'
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
        new_offsets: dict[str, int] = dict(offsets)
        body: list[str] = []
        for line in lines[1:]:
            if line.startswith("==FILE "):
                name, size = line[len("==FILE ") :].rsplit(" ", 1)
                new_offsets[name] = int(size)
                body.append(f"# {name}\n")
            else:
                body.append(line + "\n")
        if body:
            text, _ = self.redactor.text("".join(body))
            self.store.append_text(f"runner/{day}/{host}/diag.log", text)
        offsets.clear()
        offsets.update(new_offsets)
        return "ok"

    def finish(
        self, now: float, started: float, error: str | None, extra: dict
    ) -> None:
        """state/cursors.json and state/harvester.json, written only when
        something moved (so an idle store means a stopped harvester or clock)."""
        if self.errors and not error:
            error = f"{len(self.errors)} run(s) could not be recorded; first: {self.errors[0]}"
        changed = self.cursor_moved or self.runs_recorded or self.store.writes
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
                    "runs_recorded": self.runs_recorded,
                    "incidents_recorded": self.incidents_recorded,
                    "runs_failed": len(self.errors),
                    "watching": len(self.local.data.get("watch") or {}),
                    **extra,
                },
            )
        self.local.save()
        if self.store.commit_push(
            f"harvest {to_iso(now)} runs={self.runs_recorded} incidents={self.incidents_recorded}"
            + (" ERROR" if error else "")
        ):
            log(
                f"committed: runs={self.runs_recorded} incidents={self.incidents_recorded} api_calls={self.gh.calls}"
            )

    def cycle(self) -> int:
        started = time.time()
        now = started
        error = None
        extra: dict = {}
        try:
            self.new_runs(now)
            self.watch()
            self.lookback(now)
            self.followups(now)
            extra = self.runner_logs(now)
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:300]}"
            log(f"cycle failed: {error}")
            traceback.print_exc()
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
    A file shorter than its offset starts over; a file that is gone is forgotten."""
    out: list[str] = []
    if not diag_dir.is_dir():
        return out
    seen: set[str] = set()
    for p in sorted(diag_dir.iterdir()):
        try:
            if not p.is_file():
                continue
            seen.add(p.name)
            size = p.stat().st_size
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


# --- main -------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
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
    args = ap.parse_args(argv)

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
        if args.backfill:
            log(f"backfill {args.backfill} days of {repo} into {store_dir}")
            return h.run_backfill(args.backfill)
        return h.cycle()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
