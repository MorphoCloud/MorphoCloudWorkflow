# Incident Store — Phase 1 Design Note

Companion to `INCIDENT_STORE_PLAN.md` ("the plan"). The plan fixes _what_ is
collected and why; this note fixes _how_ Phase 1 is built, in enough detail that
the code is a transcription of it. The plan's Phase 1 row asks for this note
before any code, approved by the maintainer.

**Status (2026-11-09):** PR A approved with #401 (merged 2026-11-09) and
**built** on branch `incident-store-pr-a`; the store repositories, the four PATs
and the mail secrets exist. Where the code departs from the design below, "As
built" at the end of the PR A section says so. PR B (the job side) and PR C (the
portal) get their own sections in this file once PR A has shipped, each approved
before its code (one thing at a time).

**Amended 2026-11-10:** PR A shipped and ran through the Jetstream2 storage
failure of 2026-11-09, and that day showed whole classes of evidence the design
never reached (a dead share on the portal, lost alert emails, paused VMs, course
repositories). "Coverage amendment" below replaces the evidence scope with
classes and adds PR A2 before PR B. It needs the maintainer's approval before
any code.

**Priorities, in order (maintainer, 2026-11-08):** robustness (nothing found
missing from the record weeks later), simplicity (no machinery the record does
not need), cost (Actions minutes and storage: not critical, kept in view).

## PR A — the harvester side

### What PR A delivers

| Item                                                                                            | Where                                                                           |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `scripts/harvest-incidents.py`, one file, stdlib + PyYAML, `gh` and `git` CLI                   | MorphoCloudWorkflow, installed on the acquire runner                            |
| `scripts/test_harvest_incidents.py`, pytest, no network                                         | MorphoCloudWorkflow                                                             |
| `incident-signatures.yml`, `incident-sources.yml`                                               | MorphoCloudWorkflow root, vendorized, copied to the runner                      |
| `.github/actions/record-facts/redact.sed` (rules only; the action is PR B)                      | MorphoCloudWorkflow, vendorized, copied to the runner                           |
| `scripts/openstack-ro`, the read-only wrapper (decision 18 fallback)                            | MorphoCloudWorkflow, copied to the runner                                       |
| `scripts/install-harvester.sh`, the installer both callers use                                  | MorphoCloudWorkflow                                                             |
| the uniform `DISPATCH` line in the three runner-side dispatch scripts                           | `morphocloud-dispatch.sh`, `police-floating-ips.py`, `check-dispatch-tokens.sh` |
| `scripts/check-dispatch-logging.py` + pre-commit hook                                           | MorphoCloudWorkflow                                                             |
| `TARGETS` store field and the harvester line in `RUNNER_SCRIPTS`                                | `scripts/check-workflow-clock.py`, both rendered crontabs                       |
| `check-dispatch-tokens.sh` watches the harvester PAT                                            | MorphoCloudWorkflow                                                             |
| `restore-instances-dispatcher.sh` installs the harvester, drops stray lines                     | MorphoCloudWorkflow                                                             |
| `INSTANCES_RUNNER_REBUILD.md`, `runner-cron-dispatcher.md` updated                              | MorphoCloudWorkflow                                                             |
| `MorphoCloud/Incidents` and `MorphoCloud/Incidents-sandbox`: README, pre-commit, `watchdog.yml` | new private repositories                                                        |

Not in PR A: anything inside a workflow job (`record-facts`, facts sites, the
facts upload step, `run-name`, email links, B13/B14, the egress line), the facts
decryption and HMAC keys, the portal export, verdict issues. Those are PR B and
PR C. Everything PR A ships is exercised on production the day it is installed;
nothing waits for the merge.

### Evidence PR A records

From the plan's inventory: **A1–A3** (envelope, jobs, full log of every command
run and every non-success run), **E1–E4** (Grafana vacancy, ACCESS outages,
githubstatus), **F1–F6** (runner logs in full, late OpenStack snapshot with
`limits show`), **H1–H2** (issue timeline and what happened next). Not yet: B,
C, D (facts, PR B), G (portal, PR C). Classification works from the failed
step's error line only, so `unclassified` will be high until PR B; the plan says
so.

### Facts verified for this design (2026-11-09)

- `MorphoCloud/Instances` and `Test-Instances` are **private**. Members with
  read access can download run artifacts, so the plan's encryption rationale (PR
  B) stands.
- The org is on the **free plan**: 2,000 GitHub-hosted minutes a month for
  private repositories. The watchdog's ≤120 fit inside it.
- Instances produced **1,019 runs in the 24 h before this note**, matching the
  plan's 1,100 a day.
- The ACCESS feed `…/affiliation/access-ci.org/past_outages/` returns **932
  outages back to 2008**. E2 never needs `unknown`.
- `githubstatus.com/api/v2/incidents.json` returns the **last 50 incidents,
  about two months**. Backfilled incidents older than that get
  `github_status: unknown`.
- Nova's `host_id` is `sha224(project_id + host)`; the hypervisor name
  (`rNNgNN`) is recoverable by brute force. The store keeps `host_id` only;
  decoding is an analysis step, not a capture.
- Runner-script tests run by hand: `python -m pytest scripts/test_*.py`. No CI
  runs them today; the harvester's tests follow the same convention and this
  note does not add a CI job for them (simplicity; the pre-commit hooks that
  exist cover lint).

### The harvester

`scripts/harvest-incidents.py`, modeled on `police-floating-ips.py`: one file,
`log()` to stdout (cron appends to `~/harvest-incidents.log`), an `flock` on
`~/.config/morphocloud/harvest.lock` (a held lock skips the tick with one log
line; a backfill holds it for its whole run), `subprocess` to `gh`, `git` and
the OpenStack wrapper, no third-party Python beyond PyYAML (present in `~/venv`
as a dependency of the OpenStack client). `--dry-run` reads and classifies,
writes nothing. `--backfill <days>` is described below.

**Configuration**, from the crontab line (rendered by
`check-workflow-clock.py`):

```
*/5 * * * * OS_CLOUD={cloud} MORPHOCLOUD_SOURCE_REPO={repo} MORPHOCLOUD_STORE_REPO={store} /home/exouser/venv/bin/python /home/exouser/harvest-incidents.py >> /home/exouser/harvest-incidents.log 2>&1
```

plus the two PATs in `~/.config/morphocloud/` (`harvest-source.pat`,
`harvest-store.pat`, mode 600; see "Credentials"), the store checkout at
`~/incidents-store`, and the three data files beside the script
(`incident-signatures.yml`, `incident-sources.yml`, `redact.sed`).

**`incident-sources.yml`** (per source repository): the instance-name prefix
(`instance-` on production, `morpho-cloud-test_instance-` on Test), the test
accounts (`amm554`), the admin logins, the command workflows (the thirteen the
plan names: the six `report-command-outcome` callers, the four command-style
workflows, and the three GitHub-hosted command workflows), the alert workflows
(`admin-alert`, `report-dropped-command`, `on-admin-mention`), the runner-side
log files to ship, and the refusal step names (the authorize steps whose failure
is a refusal, until PR B's `REFUSED` line). A workflow file not in any list is
`kind: other`: counted in the rollups and shown in the weekly numbers, so a new
workflow is visible without being lost.

**One cycle**, in order:

1. **Runs.** `gh api repos/<src>/actions/runs?created=>=<cursor−15m>` paged at
   100, in windows of at most 6 hours (about 275 runs each; the API caps a
   filtered listing at 1,000). Then `?status=queued` and `?status=in_progress`
   with no filter: every run seen joins the watch list. Once an hour the 48-hour
   lookback relists and compares `run_attempt`, `status`, `updated_at`. The
   cursor is the newest `created_at` seen; it is committed in
   `state/cursors.json`.
2. **Watch list.** Each watched run is fetched by id until `status: completed`.
   A new `run_attempt` on a recorded run is a new outcome (and a new incident
   when it fails). Pre-PR B the artifacts are not fetched (none exist).
3. **Envelope and kind.** From `incident-sources.yml`: `command`, `sweep`,
   `alert`, `other`. A successful `sweep` or `other` run is a rollup count only
   (no jobs call); `skipped` likewise. Every `command` run, every `alert` run,
   and every run whose conclusion is not `success` gets
   `runs/<id>/attempts/<n>/jobs` (steps with conclusions, the first failed step)
   and its log.
4. **Log.** `runs/<id>/attempts/<n>/logs` is a zip of per-step files; the
   harvester keeps the whole thing as one gzipped text (the files concatenated
   in step order with a header line each) plus the failed step's last 200 lines.
   **Redaction** in two passes, identical to what PR B will do in the job:
   `sed -f redact.sed` (tokens, key blocks, SMTP and OpenStack secrets, `exoPw`,
   VNC lines, the share `access_key`), then a Python pass that replaces every
   email address with `[email:<hmac16>]` where the HMAC key is
   `~/.config/morphocloud/hmac.key`; with no key file the address becomes
   `[email]` and the record says `email_hash: unavailable` (fails closed, as the
   plan requires). The sed rules are the one shared artifact; PR B's action runs
   the same file with the same `sed`. A test fixture holding one of each secret
   and two addresses must come out clean from both passes.
5. **Issue and instance.** Pre-PR B there is no `run-name`. The issue number
   comes from the issue-comment payload where the event is `issue_comment`
   (`display_title` carries the comment's issue for those runs), else from the
   job log (`<prefix>instance-(\d+)` and `issue_number=`), else `null`. PR B's
   `run-name` makes it exact; the heuristic stays as the fallback for crashed
   runs.
6. **Classify.** `result` ∈ `success`, `failure`, `cancelled`, `skipped`,
   `dropped` (a `report-dropped-command` run), `refused` (a failed authorize
   step named in the refusal list). The error line is the last `##[error]` line
   of the failed step. `incident-signatures.yml` is an ordered list of
   `{signature, on: error_line|failed_step|workflow, match: <regex>}`; the first
   match wins; `unclassified` otherwise. `github.api_5xx` matches
   `HTTP 5\d\d|50[234] |Server Error|Bad Gateway|Gateway Time-?out` **on the
   error line only**.
7. **`actor_kind`** as the plan's five rules, preceded by rule 0 (a `schedule`
   or `workflow_run` event's first attempt is automation; see "As built"). Rule
   1 reads the runner-side logs for `DISPATCH` lines (same workflow, same issue
   where there is one, within 60 s of `created_at`). Rules 2–5 need the run's
   `actor`, `triggering_actor` and the issue's author (`issues/<n>`, cached per
   cycle).
8. **Incidents** (every `failure`, `cancelled`, `dropped`, and every `alert`
   run): the directory of the plan's "Records", `incident.json` with every field
   the collector can fill (`facts: missing` until PR B), `job.log.gz`,
   `failed-step.log`, `late-snapshot.json`, `context.json`, `timeline.json`.
   - **Late snapshot (F6)**, through the read-only wrapper, when the instance
     name is known: `server show -f json` with `--os-compute-api-version 2.62`
     (brings `host_id` and `fault`), `server event list`, `server event show`
     for the last five request ids, `port list --server`, `floating ip list`
     filtered to that port, and `limits show --absolute`. Each call capped at 60
     s; a failure is recorded as `{"error": "..."}` for that key, never fatal.
     `snapshot_age_seconds` is stamped.
   - **Context (E).** Grafana panel query for the flavor's family (panel 14
     A100, 13 general, 15 L40S, 16 H100) over ±30 minutes, the flavor from the
     issue's labels at the time; ACCESS `past_outages` filtered to Jetstream2
     resources overlapping the run; githubstatus incidents overlapping the run,
     `unknown` when the run is older than the oldest incident the feed returns.
     The ACCESS and githubstatus feeds are fetched once per cycle and cached.
   - **Timeline (H1).** `issues/<n>/timeline` from one hour before the run,
     comments redacted. **Follow-up (H2):** for incidents younger than 24 h, the
     timeline is refreshed each cycle and later outcomes on the same issue are
     appended; after 24 h the incident is closed to changes.
9. **Runner logs (F).** Every new byte of `~/morphocloud-dispatch.log`,
   `~/fip-police.log`, `~/token-expiry.log`, `~/mc-data/pickup.log` (where it
   exists) is appended to `runner/YYYY/MM/DD/<host>/<name>.log`, plain text, one
   file per day per log. Offsets live runner-side in
   `~/.config/morphocloud/harvest-offsets.json`; a file shorter than its offset
   starts over at 0 (rotation). The Actions runner's `_diag` directory: only the
   runner's own ERR-level lines (`^\[\S+ \S+ ERR `), from the files changed
   since the last cycle, capped at 2 MB per day (`DIAG_DAY_CAP`, one marker line
   when crossed); the first cycle on a runner records every file's end and ships
   nothing, so months of history are never dumped. On runner-1 in place; the
   second runner over SSH (`exouser@<control>`, the runner's own key) is
   attempted when `incident-sources.yml` names a control host and recorded as
   `diag_remote: unreachable` when it fails. Whether the acquire runner can
   reach the control runner by key is verified in the test plan, not assumed.
10. **Write.** `git add -A && git commit` with the message
    `harvest <UTC> runs=<n> incidents=<m>`, then `git pull --rebase` and
    `git push`. The harvester is the only writer; the rebase is insurance
    against a hand edit. A push that fails is logged, the commit stays local,
    and the next cycle pushes it. No commit when nothing changed, including
    `state/harvester.json`: that file is written only when the cursor moved or a
    record was added. On production the sweeps move the cursor every 5–30
    minutes, so a two-hour-old store means the harvester stopped **or the clock
    stopped**; the watchdog catches both with one check.

**Backfill**, `--backfill 90`, run once by hand under the lock: lists runs in
6-hour windows for 90 days (about 100k runs, about 1,000 listing pages), takes
jobs and logs for command and non-success runs (about 1,300 of each), commits
once per day of data (about 90 commits), and sleeps whenever
`x-ratelimit-remaining` drops under 1,000. A fine-grained PAT shares its owner's
5,000 requests an hour with the dispatcher tokens and the maintainer's own `gh`,
so the backfill takes about two hours and the live cycles are skipped while it
holds the lock. It adds about 55 MB. Backfilled incidents have no late snapshot
(the instance has moved on) and no facts; `context.json` has ACCESS outages
(full history) and `github_status: unknown` beyond the feed's range.

**API budget, steady state:** about 12 calls a cycle (1–2 listing pages, 2
in-flight listings, the watch list, jobs for about 4 runs, one hourly lookback
spread out), about 3,500 a day. Logs are one call per command run or incident,
about 15 a day. Well inside the shared 5,000 an hour.

### Classification without facts: what to expect

Until PR B, `result` and `actor_kind` are exact (they come from the API and the
runner logs); the **signature** is approximate (error line only) and
`unclassified` will be the largest class. That is acceptable for the pre-merge
weeks because the full log is kept for every one of these runs, so PR B's
classifier, once it exists, can be rerun over them (`--reclassify`, a
read-the-store-and-rewrite-incident.json mode, is in PR B's scope). Nothing
recorded pre-merge is lost or has to be recaptured.

### The read-only OpenStack wrapper (decision 18)

`scripts/openstack-ro` is a 30-line shell script installed beside the venv's
`openstack`. It accepts exactly: `server show`, `server list`,
`server event list`, `server event show`, `console log show`, `port show`,
`port list`, `floating ip show`, `floating ip list`, `limits show`,
`volume show`, `image show`, and refuses anything else with exit 2 before
touching the network. `test_harvest_incidents.py` asserts that every `openstack`
invocation in `harvest-incidents.py` goes through the wrapper and that the
wrapper rejects `server delete`, `server set`, `floating ip create` and
`server unshelve`. The plan's first choice, a Keystone application credential
with access rules, is tried on Test during Phase 1 testing
(`openstack application credential create --access-rules`); if Jetstream2's
Keystone accepts it, the wrapper is kept anyway as defense in depth and the
credential replaces `clouds.yaml`'s for the harvester only. If not, the plan's
recorded risk stands: the harvester process holds the allocation's read-write
credential behind a wrapper.

### The `DISPATCH` line and its check

Every runner-side `gh workflow run` call site writes, as soon as the call
returns:

```
<UTC> DISPATCH <workflow.yml> repo=<owner/repo> issue=<n|-> by=<script> result=ok
<UTC> DISPATCH <workflow.yml> repo=<owner/repo> issue=<n|-> by=<script> result=FAILED <last 200 chars of stderr>
```

- `morphocloud-dispatch.sh`: replaces `dispatched <wf>` /
  `FAILED to dispatch <wf>`; `gh`'s stderr is captured instead of sent to
  `/dev/null`. The two documents that say to grep for `dispatched`
  (`runner-cron-dispatcher.md`, `INSTANCES_RUNNER_REBUILD.md`) and the restore
  script's closing message are updated to `DISPATCH`.
- `police-floating-ips.py`: `note_issue` and `send_alert`, one line each right
  after `subprocess.run`, `issue=` filled for notes.
- `check-dispatch-tokens.sh`: `dispatch_alert`, to `~/token-expiry.log`.
- The portal runner's `dispatch_shelve` is PR C (same format, `by=pickup`).

`scripts/check-dispatch-logging.py` (pre-commit, `files: ^scripts/`): a call
site is either the substring `gh workflow run` on one line (shell) or, in
Python, the string literal `"gh"` followed by the literals `"workflow"` and
`"run"` in that order within the next 5 lines, each on its own line as
`police-floating-ips.py` writes them (`["gh", "workflow", "run", …]` laid out
one element per line). For every call site a line containing the literal
`DISPATCH ` must follow within 30 lines of the same file, else the hook fails
naming the file and line. The hook's own test holds one shell site, one
multi-line Python site with the line, and one without. PR B extends the same
script to `.github/workflows` and `.github/actions`, where the required neighbor
is a `facts.sh` dispatch record.

### Renderer and restore changes

- `check-workflow-clock.py`: `TARGETS` tuples gain a fifth element, the store
  (`"MorphoCloud/Incidents"` for production, `"MorphoCloud/Incidents-sandbox"`
  for Test while Phase 1 is tested, `None` afterwards), and the two places that
  unpack the tuple change with it: `crontab_errors()` unpacks
  `(repo, cloud, hold, extra_rel, store)` and `render()` gains a
  `store: str | None` parameter. `RUNNER_SCRIPTS` gains the harvester line with
  `{store}`. In `render()`'s loop over `RUNNER_SCRIPTS`, before the `format`
  call: `if "{store}" in command and store is None: continue`; the `format` call
  then passes `store=store` beside `cloud`, `repo` and `hold`. Today's single
  fixed `format(cloud=…, repo=…, hold=…)` would otherwise raise `KeyError` on
  the new placeholder or write the word `None` into a crontab, and this renderer
  writes every dispatch line for both repositories, so the change gets a test:
  render both targets with and without a store and assert the harvester line is
  present exactly when the store is set and no other line changed. Both
  committed crontabs are re-rendered in the PR.
- `scripts/install-harvester.sh <user@host> [--outside-block]`: copies the
  script, the three data files and the wrapper; copies the two PATs from
  `MWF_HARVEST_SOURCE_TOKEN` and `MWF_HARVEST_STORE_TOKEN` (defaults
  `~/.ssh/GH_MorphoCloud-incidents-source` and `…-store`) to
  `~/.config/morphocloud/harvest-source.pat` and `harvest-store.pat`; clones the
  store (`MWF_STORE_REPO`) into `~/incidents-store` if absent, with the token
  read by a credential helper from the `.pat` file so it is never written into
  `.git/config`, and sets the clone's identity to
  `MorphoCloud harvester <no-reply@morphocloud.org>`; verifies both tokens
  authenticate and prints their expiry. With `--outside-block` (production
  before the merge) it appends the crontab line after the managed block if no
  `harvest-incidents` line exists (`MWF_SOURCE_REPO`, `MWF_OS_CLOUD`). Without
  the flag it installs files only and leaves the crontab to the restore script.
- `restore-instances-dispatcher.sh`: calls `install-harvester.sh` (files and
  tokens); if either harvester token is missing it prints a warning, strips the
  `harvest-incidents` line from the crontab it installs, and installs everything
  else (the plan: a missing harvester key never blocks the clock). Its "kept
  outside the block" filter gains `harvest-incidents`, so the hand-added
  production line is dropped at the merge and cannot double-fire.
- `check-dispatch-tokens.sh`: two more `check` lines, `harvest-source.pat` and
  `harvest-store.pat`.
- `INSTANCES_RUNNER_REBUILD.md`: the file table gains `harvest-incidents.py`
  - data files (this repo), the two `.pat` files (backups
    `~/.ssh/GH_MorphoCloud-incidents-{source,store}`), `~/incidents-store`
    (re-clone), `harvest-local.json` (a cache), and later PR B's keys.

### The store repositories

Created by the maintainer (org owner) or by the implementer with the
maintainer's `gh`: `MorphoCloud/Incidents` and `MorphoCloud/Incidents-sandbox`,
private, default branch `main`, no Pages, Actions enabled. Contents at creation:

- `README.md`: what is here, the layout from the plan's "Records", how to query
  it (`duckdb` over `outcomes/**/*.jsonl`, two example queries), and the
  statement that the harvester is the only writer.
- `.pre-commit-config.yaml` with `check-added-large-files --maxkb=2048`, and
  `.gitattributes` marking `*.gz` binary.
- `.github/workflows/watchdog.yml`: `on: schedule` every 6 hours **plus**
  `workflow_dispatch` (the plan's stated exception to decision 21). It checks
  out the repo, reads `state/harvester.json`, and emails when `last_cycle` is
  older than 2 hours or `last_error` is set, through `dawidd6/action-send-mail`
  with the store's own `MAIL_USERNAME`/`MAIL_PASSWORD` secrets and an
  `ADMIN_EMAILS` variable (same server settings as `admin-alert.yml`). It alerts
  at every delivery while the condition holds (at most 4 emails a day during an
  outage) and keeps no state, so it is never a second writer. Cost: ≤120 billed
  minutes a month, inside the free plan's 2,000.
- Empty `state/`, `outcomes/`, `incidents/`, `runner/`, `rollups/`, `logs/`,
  `setup/`, `portal/` with `.gitkeep`.

The sandbox is identical and is deleted after Phase 1 (plan, decision 1).

### Credentials PR A needs (maintainer creates; values never in git)

A fine-grained PAT applies **one** permission set to every repository it is
granted, so "one PAT per environment" (the first draft of this table) is not
possible: the source needs Actions:read + Issues:read and the store
Contents:write + Issues:write. Two tokens per environment, created and verified
2026-11-09 (each sees only its one repository; the source tokens cannot push;
all expire 2027-11-10):

| Credential                                       | Permissions                                                     | Where                                                                                     |
| ------------------------------------------------ | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `GH_MorphoCloud-incidents-source` (prod)         | fine-grained PAT, 1 year: `Instances` Actions:read, Issues:read | `~/.ssh/` backup on the maintainer's machine; `harvest-source.pat` on the acquire runner  |
| `GH_MorphoCloud-incidents-store` (prod)          | `Incidents` Contents:read/write, Issues:read/write              | `~/.ssh/`; `harvest-store.pat` on the acquire runner (read by git's credential helper)    |
| `GH_MorphoCloud-test-incidents-source`           | same as the prod source, on `Test-Instances`                    | `~/.ssh/`; `harvest-source.pat` on the Test acquire runner                                |
| `GH_MorphoCloud-test-incidents-store`            | same as the prod store, on `Incidents-sandbox`                  | `~/.ssh/`; `harvest-store.pat` on the Test acquire runner                                 |
| `MAIL_USERNAME`, `MAIL_PASSWORD`, `ADMIN_EMAILS` | the same values `admin-alert.yml` uses (set 2026-11-09)         | secrets and variable on both store repositories; verified with one sandbox watchdog email |
| HMAC key (`hmac.key`)                            | 32 random bytes                                                 | PR B's concern; PR A fails closed without it (`[email]`)                                  |

Issues:write on the store is unused until Phase 2 (verdict issues) but is
granted now so the PAT is not re-issued in two weeks. No token has Actions write
anywhere: the harvester cannot start a workflow.

### Test plan on Test-Instances (as amm554; a test plan the maintainer approves first)

Setup: both store repos exist; the Test PAT exists;
`install-harvester.sh exouser@149.165.154.162` with the Test token; the Test
crontab re-rendered and installed with `restore-instances-dispatcher.sh` (the
Test variables as in `runner-cron-dispatcher.md`); `--backfill 90` run once on
Test-Instances into the sandbox.

| #   | Provoke (actor)                                                                                | Expect in the store                                                                                                                                                     |
| --- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1  | amm554, through the portal: create, wait for ready, shelve, unshelve, delete                   | five outcome lines, `actor_kind: test`, full logs, issue number and instance name filled from the log, each within 10 minutes of the run's end                          |
| T2  | amm554: `/unshelve` on an already-active instance (refusal)                                    | `result: refused`, no incident directory, `actor_kind: test`                                                                                                            |
| T3  | maintainer sets `MORPHOCLOUD_MAINTENANCE`; amm554 presses a button                             | `result: refused`, reason step named                                                                                                                                    |
| T4  | a crontab sweep tick (happens on its own)                                                      | rollup count only; `actor_kind: automation` on any acting sweep, matched to a `DISPATCH` line                                                                           |
| T5  | maintainer: `gh workflow run reconcile-instances.yml` from a laptop                            | `actor_kind: admin` (no `DISPATCH` line within 60 s)                                                                                                                    |
| T6  | maintainer: `gh run rerun --failed` on a failed run older than 48 h                            | a second outcome line for attempt 2, within 5 minutes of its end                                                                                                        |
| T7  | maintainer: a create with a flavor the project cannot satisfy, on amm554's issue               | incident with `late-snapshot.json` (server show with `fault`, events, limits), `context.json` with Grafana vacancy for the flavor and ACCESS status, `timeline.json`    |
| T8  | maintainer: lower `MORPHOCLOUD_FIP_HOLD` so the policing script releases and re-mints          | `fip-police.log` lines in `runner/…`, the `DISPATCH` line if it alerts, that alert run as `automation`                                                                  |
| T9  | maintainer: comment out the harvester crontab line for 2.5 hours, then dispatch `watchdog.yml` | one watchdog email; restore the line; the next cycle records the backlog with no gap in `outcomes/`                                                                     |
| T10 | maintainer: run `restore-instances-dispatcher.sh` with `MWF_HARVEST_TOKEN=/nonexistent`        | warning printed, crontab installed without the harvester line, dispatcher lines intact; rerun with the token restores it                                                |
| T11 | redaction fixture (pytest, no network)                                                         | a file with a `ghp_`, a `github_pat_`, a private-key block, `OS_PASSWORD=`, `exoPw:`, `access_key`, and two emails comes out with none of them; HMAC stable across runs |
| T12 | wrapper test (pytest)                                                                          | `openstack-ro server delete x` exits 2; every `openstack` call in the harvester goes through the wrapper                                                                |
| T13 | `openstack application credential create --access-rules` on BIO240357 (maintainer)             | recorded in this note: accepted (and the harvester uses it) or refused (the wrapper alone, risk recorded)                                                               |
| T14 | measurements over one week on Test                                                             | cycle duration p50/p95, API calls per cycle, repo growth per day, cursor lag p95; all reported in the PR                                                                |

Exit for PR A, from the plan's Phase 1 row restricted to PR A's scope: every run
in the Actions UI is in the store as a row or a count (checked for one full
day), T1–T14 pass, the production install procedure below has been rehearsed end
to end on Test.

### Production install (before the merge; needs the maintainer's explicit go-ahead)

**Done 2026-11-09** on the maintainer's "merged, proceed with your tests and
installs; I want the collection to start now": steps 1–2 earlier that day, step
3 with `install-harvester.sh exouser@149.165.152.104 --outside-block` (store
cloned, both tokens verified, the crontab line added after the managed block),
step 4 (dry run: 2,125 runs over 48 hours, 9 would-be incidents, about 3.5
minutes), step 5 (the 90-day backfill, started 06:12 UTC), step 7
(`check-dispatch-tokens.sh` copied). Step 6 (the watchdog) follows the backfill,
since `state/harvester.json` is written at its end. The Test install the same
morning went through `restore-instances-dispatcher.sh` with the Test variables
and backfilled `Incidents-sandbox` in 24 minutes (21,326 runs, 189 incidents, 22
MB).

Production runs `main`; nothing in this PR touches a workflow, so installing the
harvester there is a runner-host change only. Steps, each one visible:

1. `gh repo create MorphoCloud/Incidents --private` (or the maintainer does).
   Push the bootstrap contents; set the mail secrets and `ADMIN_EMAILS`.
2. The maintainer creates `GH_MorphoCloud-incidents-harvester` and saves it to
   `~/.ssh/`.
3. `scripts/install-harvester.sh exouser@149.165.152.104 --outside-block` (never
   `restore-instances-dispatcher.sh` from `share-prototype` against production
   before the merge).
4. `ssh … 'harvest-incidents.py --dry-run'` once; read the log.
5. `ssh … 'harvest-incidents.py --backfill 90'` in a `tmux`; about two hours.
6. Watch three cycles' commits; dispatch `watchdog.yml` once and confirm no
   email (healthy).
7. Add the `check-dispatch-tokens.sh` entry on the production runner (it is in
   the PR's script; the installer copies it).

At the 2026-11-23 merge, `restore-instances-dispatcher.sh` installs the rendered
crontab (harvester line inside the block) and drops the hand-added line.

### Cost of PR A

| Item                                    | Cost                                               |
| --------------------------------------- | -------------------------------------------------- |
| harvester and backfill (runner crontab) | 0 Actions minutes                                  |
| watchdog, every 6 h, GitHub-hosted      | ≤120 min/month, inside the free plan's 2,000       |
| store growth (A, E, F, H; no facts yet) | ~0.4 MB/day; backfill ~55 MB once                  |
| API requests                            | ~3,500/day shared with the maintainer's 5,000/hour |

### Decisions this note asks the maintainer to confirm

1. The two store repositories are created now (PR A cannot be tested without the
   sandbox). _Done 2026-11-09._
2. The two harvester PATs are created by the maintainer as specified. _Done
   2026-11-09, as four (see "Credentials")._
3. The test plan T1–T14 as written, run as amm554 where a member acts.
4. The production install is a **separate go-ahead**, asked for after PR A's
   exit criteria are met, not implied by approving this note.

### As built (where the code departs from the text above, and why)

- **The GitHub API is called with `urllib`, not the `gh` CLI.** The harvester
  needs the response headers (rate-limit budget for the backfill's pacing, the
  token's expiry) and the redirect of the log download, which are awkward
  through `gh api`; stdlib `urllib` gives them directly and removes one process
  per call. `gh` is still used by the installer to print each token's expiry.
- **`incident-signatures.yml` keys the field as `field`, not `on`:** PyYAML
  reads an unquoted `on` as the boolean `True` (the same trap
  `check-workflow-clock.py` documents for workflow files).
- **Runner-side state is one file, `~/.config/morphocloud/harvest-local.json`**
  (the watch list, the log offsets, the hourly lookback stamp, the `_diag`
  high-water mark), not `harvest-offsets.json`. None of it is evidence: lost on
  a rebuild, the in-flight listings re-seed the watch list and the hourly
  lookback re-records anything missed.
- **Dedupe lives in the store, not runner-side.** Each day's rollup file
  (`rollups/YYYY/MM/DD/<repo>.json`) carries
  `recorded: ["<run_id>-<attempt>", …]` beside the counts (about 13 KB a day on
  production). The lookback and a rebuilt runner both ask the store whether a
  run is recorded, so a lost local state can never double-count a rollup or
  duplicate an outcome line.
- **Skipped runs are counts only, and `dropped` is read from the reporting
  step.** Every `issue_comment` fires every command workflow, and all but one
  skip; the first dry run showed those skipped runs would have earned outcome
  lines and full logs (about 450 in 48 hours on Test). Now a skipped run of any
  kind is a rollup count (A1), never a detail fetch, an outcome line or an
  incident (`on-admin-mention` without a mention, `report-dropped-command`
  without a cancellation). `report-dropped-command` succeeds whether or not it
  found a dropped command, so its run is `dropped` only when its "Tell the user
  the command was dropped" step ran (`dropped_report` in
  `incident-sources.yml`); otherwise it is a plain success and no incident.
- **`actor_kind` has a rule 0**: a run whose event is `schedule` (GitHub's own
  scheduler, the backstop) or `workflow_run` (a follow-up such as
  `report-dropped-command`) is `automation` whoever the actor is. GitHub credits
  such a run to the last person who committed the workflow file, who is not an
  admin on Test (jcfr), so the first backfill read August's scheduled ticks as
  `member`. Found and fixed on the day of the Test install; the sandbox was
  reset and backfilled again. Rule 0 applies to the first attempt only: a rerun
  keeps the event but its `triggering_actor` is the person who clicked, and the
  five rules below decide it. Rule 1 (the `DISPATCH` join) is gated the same
  way: a rerun keeps attempt 1's `created_at`, so the line it would match is
  attempt 1's (test T6 found this: the rerun read as `automation`).
- **Backfill limitation, stated**: the runner's dispatch log starts when the
  dispatcher was (re)installed (2026-11-03 on Test), so a crontab-dispatched run
  older than that has no `DISPATCH` line to join and reads as `admin` (the
  dispatcher token's owner). The record keeps `actor`, `event` and `created_at`,
  so PR B's `--reclassify` can revisit them; the production dispatch log reaches
  further back.
- **Refusals** are recognized by a failed job named in `refusal_jobs`
  (`authorize`, the job every `report-command-outcome` caller fails
  deliberately) as well as by the step names in `refusal_steps`, and by PR B's
  `REFUSED reason=` line when present.
- **Labels (`markers`)**: for an incident they are reconstructed from the
  issue's timeline at the run's `created_at` (`markers_source: timeline`); for a
  plain outcome line they are the issue's labels at harvest time
  (`markers_source: current`), one issue read per distinct issue per cycle
  rather than a timeline read per command run.
- **The instance name** comes from the log where it appears
  (`name_source: log`); when only the issue number is known the name is derived
  from the source's prefix (`name_source: derived`) so the late snapshot can
  still be taken.
- **The harvester's own log is not shipped** (a first draft did): every cycle
  writes a line, so shipping it would make every cycle a commit and silence the
  watchdog's "quiet store" signal. A cycle's failure reaches the store as
  `last_error` in `state/harvester.json` (one bad run is contained to that run,
  counted in `runs_failed`, and retried by the next cycle or the hourly
  lookback); the traceback stays in `~/harvest-incidents.log` on the runner.
- **Order of writes**: a run's files are written first and the rollup's
  `recorded` mark last, so a failure halfway leaves the run unrecorded and
  retried; rollups are flushed once per cycle (or per backfill day), not once
  per run. A failed `git pull --rebase` is aborted at once so no cycle finds a
  rebase in progress; the commit stays local for the next push.
- **`_diag` lines are shipped once**: per-file offsets (local and, over SSH, for
  the control runner) rather than "files modified since", which would re-ship
  every matching line of the active runner log each cycle; the remote offsets
  advance only after a complete read. Found on day one (2026-11-09): the first
  pattern `ERR|Exception|Unhandled|lost connection|Terminated` matched
  `STDOUT/STDERR stream read finished`, .NET long-poll cancellation stack frames
  and job-message JSON literals, and the first cycle shipped the whole directory
  (81 MB on Test, 9.7 MB on production). Now: ERR level only, prime-then-ship, 2
  MB per day.
- **`MORPHOCLOUD_CFG_DIR`** overrides `~/.config/morphocloud` so a `--dry-run`
  can be made from a laptop with a scratch token file (used for the first dry
  run against Test-Instances on 2026-11-09: no cursor, so the 48-hour lookback
  ran: 2,946 runs, 150 would-be incidents, about ten minutes, nothing written).
- **Data files** are read beside the script on a runner and from the repository
  root in a checkout, so the same script runs in both places.
- **Tests**: `scripts/test_harvest_incidents.py` (redaction fixture T11, the
  wrapper T12, the installer's crontab line equals the renderer's, signatures,
  refusals, log assembly, identification, the six actor rules, context feeds,
  labels at a moment, store dedupe and dry run, log offsets and rotation),
  `scripts/test_check_dispatch_logging.py` (one shell site, one multi-line
  Python site with the line and one without, the 30-line window, and the
  repository's own scripts), `scripts/test_check_workflow_clock.py` (both
  targets rendered with and without a store).

## Coverage amendment (2026-11-10): every host, every repository, platform state

### What the outage showed

On 2026-11-09 Jetstream2 had storage failures from about 17:45Z. Afterwards 14
of the 18 user shares on the data portal were dead: still listed as mounted,
"Permission denied" even for root, because the Ceph metadata server had
blocklisted the portal's sessions (kernel: `mds7 rejected session`, about
20:00Z). The portal's check saw the gap at 19:42Z, then stopped flagging it once
the dead mounts showed as mounted again. Its alert emails all failed with
Dynadot's `524 You sent it too frequently`. Every MorphoCloud server VM was
`PAUSED` by Jetstream2 for hours. None of it reached the store, and the store as
designed could not have recorded it.

**Root cause in this design.** The evidence inventory was swept from our code:
every log line, table and file our scripts write. That cannot find what our code
never writes: kernel and systemd messages, a mount that looks healthy but is
dead, a mail server's rejection that only reaches a journal, a VM's state in
Jetstream2, or runs in repositories other than the two sources. The rule below
is drawn from where MorphoCloud **runs**, not from what its code logs.

### The rule (replaces "PR A records" as the scope of the store)

MorphoCloud's evidence is three source classes and two rules for the code that
emits it. A new host, repository or sender falls under a class by default; it is
left out only by an explicit, written exclusion.

| Class | Evidence                                                                                                               | New inventory items |
| ----- | ---------------------------------------------------------------------------------------------------------------------- | ------------------- |
| I     | the system journal of **every MorphoCloud server VM**: all lines from our own units, `warning` or worse from the rest  | I1–I3               |
| J     | **platform state**: every server VM's Nova state and every self-hosted runner's online status, on each change          | J1–J2               |
| K     | the workflow runs of **every MorphoCloud repository** that acts on users or infrastructure, not only the two sources   | K1                  |
| R1    | rule: a health check exercises the resource (reads the share, opens the page), never only its state (mounted, running) | (emitter change)    |
| R2    | rule: every outbound email writes one result line, sent or failed, with the server's reply, wherever it is sent from   | (emitter change)    |

**Out of scope:** MorphoDepot (a separate project on the same allocation; its
one shared failure mode, an object-storage outage, reaches the store through E2
and through our own runs failing). **Known blind spots,** recorded so nobody
mistakes them for coverage: Apps Script execution logs (they live in Google),
Dynadot's own logs, and course runner VMs on other allocations unless the build
verifies a key reaches them.

### New inventory items

| Item | Evidence                                                                                                                                                                                                                                                                                                            |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| I1   | each host's journal from a saved cursor (`journalctl --after-cursor`, so a line ships once): every line of the units `incident-sources.yml` lists for that host; `warning` or worse from everything else, minus the noise list (I3)                                                                                 |
| I2   | per host per cycle: reachable or not. An unreachable host is a line in its day file and a count in `state/harvester.json`; the watchdog alerts when a host has been unreachable for two hours                                                                                                                       |
| I3   | the noise list, in `incident-sources.yml`, each entry with the count it removed that day so a filter that grows too greedy shows: `[UFW BLOCK]` (~900 lines per host per 8 h), PipeWire `jackdbus`, Exosphere `automount-volume@loop*`, `networkd-dispatcher`                                                       |
| J1   | each server VM's `status`, `OS-EXT-STS:vm_state`, `task_state`, power state, through `openstack-ro server list`, one line per change in `platform/YYYY/MM/DD.jsonl` (on 2026-11-09: `ACTIVE` to `PAUSED` at 17:54Z for six VMs)                                                                                     |
| J2   | each self-hosted runner's GitHub status (`online`, `offline`, `busy`), one line per change in the same file                                                                                                                                                                                                         |
| K1   | runs of every org repository except a deny-list of code-only repositories (their CI): today the course repositories (`MC-*`, own runner, 128 runs a week), `morphocloud-monitor` (hourly join health check, opens outage issues), `MorphoCloudAnalytics`, `MorphoCloudCourseTemplate`, `morphocloud-intake-backups` |

### How each class is built

**I, the journals (PR A2, harvester).** `incident-sources.yml` gains `hosts:`
per source: name, SSH target, the units that ship in full. Each cycle the
harvester reads `journalctl -o short-iso --after-cursor=<saved>` per host,
filters, redacts with the existing redactor, and appends to
`runner/YYYY/MM/DD/<host>/journal.log` under the same 2 MB per-day cap and
marker as `_diag`. The cursor is saved runner-side and advances only after the
read completed, as `remote_diag` does. The acquire runner reads its own journal
locally. Other hosts are read through a **read-only journal account**: user
`mcjournal`, group `systemd-journal`, whose `authorized_keys` entry is a forced
command allowing only `journalctl` with a cursor (the pattern of `mcrunner` on
the portal). First sight of a host ships the last seven days through the same
filter and cap, so the record starts before the install (the portal keeps only
about eight days of journal).

Hosts today, from the 2026-11-10 sweep:

| Environment | Hosts                                                                                                                                                                   |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Production  | `morphocloud-instances-runner` (local), `morphocloud-instances-runner-2`, `join-morphocloud` (join app, welcome-email cron, Jetstream2 notifier, nginx, monthly reboot) |
| Test        | `mc-test-runner` (local), `mc-data-portal` (moves to production at the migration), `mc-join-test`                                                                       |

Size: the outage window (8 h, the worst day) left about 490 lines on the portal
after the noise list and under 20 on each other host, so well under 1 MB a day
in total.

**J, platform state (PR A2, harvester).** One `openstack-ro server list` per
cycle for the source's allocation, kept to the hosts above, and one GitHub
runners listing per source repository. Written only on change, so a quiet day
writes nothing. The GitHub listing needs `Administration: read` on the source
PATs (a credential change for the maintainer).

**K, every repository (PR A2, harvester).** `sources:` becomes the whole
organization minus `deny:`, read once a cycle from the org's repository list, so
a new course repository is harvested from its first run. Unknown workflows are
kind `other` (counted; a failure is an incident), as today. The source PATs are
re-issued for **all** repositories of the organization (read-only Actions and
Issues), or a new repository would be invisible to them. Cost: about one listing
call per repository per cycle, a few hundred calls an hour on a 5,000 limit.

**R1, checks that exercise the resource.** The portal's share check reads each
share (a `stat` and a directory listing under a timeout) and logs one line per
failure and per recovery; that line reaches the store through I1 (and as G9 when
PR C ships). Every other health check is audited against R1 in the PR that
touches it, with the result as a table in that PR: today `morphocloud-monitor`'s
`/healthz`, the in-job Jetstream2 check (B13) and the reconcile sweep's
reachability test.

**R2, mail result lines.** One line per send at every sender:
`<UTC> MAIL result=ok|FAILED to=<count> subject=<subject> reply=<SMTP reply>`.
Senders: the portal (`deliver`), the join app (`_send_bcc` and the single
sends), `morphocloud-monitor`, the store watchdog, and the workflows that send
(their failures already fail the job). A check like `check-dispatch-logging.py`
keeps new senders honest in each repository. Apps Script senders are the blind
spot named above.

### Where each piece lands

| Piece                                                                                             | PR                              | Order             |
| ------------------------------------------------------------------------------------------------- | ------------------------------- | ----------------- |
| I, J, K in the harvester; `hosts:`, `deny:`, noise list; `mcjournal` installer                    | **PR A2** (MorphoCloudWorkflow) | next, before PR B |
| R1 and R2 on the portal                                                                           | PR C (morphocloud-data-portal)  | with PR C         |
| R2 in the join app and `morphocloud-monitor`                                                      | one small PR in each repository | after A2          |
| R2 in the workflows                                                                               | PR B                            | with PR B         |
| one-time import of the 2026-11-09 outage window, already saved from all seven hosts on 2026-11-10 | PR A2's install                 | at A2's install   |

### Open: the watchdog's own clock

The store watchdog did not run once between 17:18Z and at least 19:52Z on
2026-11-09, despite two cron lines: GitHub did not deliver the schedule. The
outage therefore went unalerted by this system. Its only clocks are Jetstream2
(the runners, down in this outage) and GitHub's schedule (unreliable, decision
21). A clock outside both is a separate decision for the maintainer; until then
this is a stated limit, not coverage.

### Tests (presented for approval with PR A2's build, as amm554 where a member action is involved)

`logger -p err` on each host appears in the store within one cycle; an
unreachable host is recorded and alerted after two hours; pausing a Test VM
records `ACTIVE` to `PAUSED` and back; stopping the Test runner service records
`offline`; a new throwaway repository's first run is harvested; a filtered noise
line is counted, not stored.

## PR B — the job side

_To be written after PR A ships. Scope per the plan: `record-facts` with
redaction and encryption, every facts site (B, C, D), the facts upload as the
last step of every self-hosted job, `run-name`, the version file, the incident
link in every admin email, the three CI checks, the harvester's facts and
`--reclassify` modes, the keys in the restore script. Its design note carries
the per-capture lock-time estimates and measurements the plan requires._

## PR C — the portal

_To be written after PR A ships. Scope per the plan: `portal_events`,
`export-events`, the `pickup` `DISPATCH` line, the harvester's portal step._
