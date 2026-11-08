# Incident Store — Design & Implementation Plan

Every failure in the MorphoCloud system today ends as an email and a comment,
and then evaporates: GitHub deletes run logs after 90 days, OpenStack state at
the moment of failure is never recorded, and nothing ties a failure to the
version of the workflows that produced it. The postmortems in this repository
(Instances#110, #139, #166, #168) were each reconstructed by hand from whatever
happened to still be around.

This plan adds a durable **incident store**: a private GitHub repository that
receives a structured bundle for every failure and a one-line record for every
command outcome, success included. Two consumers sit on top of it: a **triage
agent** that classifies each incident and decides whether a human needs to be
contacted now, and a scheduled **analysis agent** that looks across months of
incidents for common threads and proposes fixes. Email becomes an output of
triage, not the system of record.

**Status (2026-11-08):** planned, nothing built. Phase 1 (collector + store, no
LLM) is built and soaked on Test-Instances first.

## Problems being fixed

1. **Evidence evaporates.** Run logs expire at 90 days; the OpenStack view
   (server state, instance actions, console log, port/FIP bindings) is never
   captured at failure time; the instance's own cloud-init log is lost with the
   instance.
2. **No denominator.** We know an `/unshelve` failed, not how often `/unshelve`
   fails per flavor, per hour of day, or per workflow version. Patterns cannot
   be told from noise.
3. **No link to code version.** A failure cannot be attributed to a vendorize
   (regression) versus a cloud-side change without this.
4. **Alerting is all-or-nothing.** Every failure emails the admins at the same
   urgency; nothing deduplicates, batches, or escalates on repetition.
5. **Classification lives in people's heads.** `failure_mode` (`no_capacity`,
   `host_error`) and `failure_detail` exist in two actions, but there is no
   shared vocabulary that a human and an agent can label with consistently.

## Decisions

Agreed 2026-11-08. Changing any of these needs the maintainer's approval, and
this table is updated first.

| #   | Decision                                                                                                                                                                                                                                                                                                                                                        |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **The store is a private GitHub repository, `MorphoCloud/Incidents`.** Git history is the audit trail; the agents already have first-class GitHub tooling; DuckDB queries a checkout directly. Jetstream2 object store is the overflow path for raw logs if the repo outgrows git (see Retention). No database server, no daemon.                               |
| 2   | **The collector is deterministic shell, no LLM.** It runs in the existing `if: failure()` paths and must never mask the real failure (`continue-on-error: true`, same rule as the admin-email steps). It is the evidence base; it has to be boring.                                                                                                             |
| 3   | **Agents are stateless; the store is the memory.** No long-lived process. Triage and analysis each run as a GitHub Actions job that reads the store, acts, and writes its conclusions back into the store. Memory is whatever is committed.                                                                                                                     |
| 4   | **Successes are recorded too**, one small file per command outcome. Base rates need the denominator.                                                                                                                                                                                                                                                            |
| 5   | **Redaction is mandatory and happens before anything leaves the runner.** `::add-mask::` only hides values in GitHub's rendered log; the raw text we copy still contains them. Known secret shapes and user emails are scrubbed by the collector. The store is private regardless.                                                                              |
| 6   | **Failure signatures are a controlled vocabulary** kept in this repository (`incident-signatures.yml`, vendorized with everything else). The collector assigns the most specific signature it can; the agent may _propose_ new ones by PR; only the maintainer merges them.                                                                                     |
| 7   | **The agent workflows live in `MorphoCloud/Incidents`, not here.** They are triggered by pushes to the store and by schedule, they need the Claude token (which exists only on MorphoCloudWorkflow today and is excluded from vendorize), and they must not be vendorized into course repos. Collector code lives here and is vendorized like any other action. |
| 8   | **Shadow before switch.** Triage runs for at least 3 weeks writing verdicts into the store while today's emails keep flowing unchanged. Only after a comparison does `MORPHOCLOUD_GITHUB_ADMIN_EMAILS` on Instances move to the alerts mailbox.                                                                                                                 |
| 9   | **Scope: Instances (via Test-Instances first).** Course repos (`MC-*`) and the template get the collector by vendorize later; their incidents go to the same store, tagged by source repo.                                                                                                                                                                      |
| 10  | **Agents are read-mostly.** Triage may comment on issues and send mail. Analysis may open issues and draft PRs against MorphoCloudWorkflow. Neither closes issues, runs `/commands`, or has OpenStack write access. Users author the issue text the agents read, so prompt injection is possible; the tool allowlist bounds the damage to a wrong email or PR.  |

## Architecture

```
  Instances / Test-Instances / MC-* (GitHub Actions, self-hosted runners)
  ┌────────────────────────────────────────────────────────────────────┐
  │ control-instance ─┐                                                 │
  │ create-instance  ─┤ if: failure()  ┌──────────────────┐             │
  │ setup-instance   ─┼───────────────▶│ capture-incident │─ push ──┐   │
  │ reconcile        ─┤                └──────────────────┘         │   │
  │ dormant detector ─┘                                             │   │
  │ every command ──── always() ─────▶ record-outcome ─── push ─────┤   │
  │ archive-run-logs (workflow_run) ── job logs after completion ───┤   │
  └─────────────────────────────────────────────────────────────────┼───┘
                                                                    ▼
  MorphoCloud/Incidents (private)
  ┌────────────────────────────────────────────────────────────────────┐
  │ incidents/YYYY/MM/<id>/   incident.json, narrative.md, logs/*.gz   │
  │ outcomes/YYYY/MM/<run>.json                                        │
  │ triage/<id>.json          ◀── triage-incident.yml (on push)        │
  │ reports/YYYY-Www.md       ◀── analyze-incidents.yml (weekly)       │
  └──────────────┬──────────────────────────────────┬──────────────────┘
                 │ page / digest / suppress          │ issues + draft PRs
                 ▼                                  ▼
          admin mailbox                    MorphoCloud/MorphoCloudWorkflow
```

Three properties fall out of this shape:

- **Nothing on the hot path waits on an LLM.** The collector finishes in seconds
  and the user-facing failure comment is unchanged.
- **The store's own push is the trigger for triage.** No webhook, no poller, no
  second source of truth about what happened.
- **A conclusion is always next to its evidence.** A triage verdict or a pattern
  report cites incident ids that resolve to directories in the same repo.

## The store: `MorphoCloud/Incidents`

### Layout

```
incidents/
  2026/11/
    20261108T142233Z-instances-12345678901-unshelve/
      incident.json          # structured record, schema below
      narrative.md           # human-readable: what, where, links
      openstack/
        server-show.json
        server-event-list.json
        console-log.txt.gz
        port-list.json
        floating-ip-list.json
        volume-show.json
      instance/
        cloud-init-output.log.gz     # only when the instance was reachable
        journal-tail.txt.gz
      runner/
        host.json                    # hostname, labels, load, disk, queue wait
      github/
        command-context.json         # from record-command-context, when present
        job-log.txt.gz               # added later by archive-run-logs
outcomes/
  2026/11/
    12345678901.json       # one file per run: no append conflicts
triage/
  20261108T142233Z-instances-12345678901-unshelve.json
reports/
  2026-W45.md
  patterns/<slug>.md       # one page per recurring thread the analysis tracks
schema/
  incident.schema.json
  outcome.schema.json
  triage.schema.json
README.md
```

**Incident id** =
`<UTC timestamp>-<source repo short name>-<run id>-<command or job>`. Sorts
chronologically, is unique per run, and is readable in a URL.

One directory per incident and one file per outcome means concurrent writers
never touch the same path, so pushes only ever race on the ref, never on
content.

### `incident.json`

The record every consumer keys off. Everything the collector can fill
deterministically; nothing that requires judgment.

```json
{
  "schema_version": 1,
  "id": "20261108T142233Z-instances-12345678901-unshelve",
  "recorded_at": "2026-11-08T14:22:33Z",
  "source": {
    "repo": "MorphoCloud/Instances",
    "workflow": "Control Instance",
    "workflow_file": "control-instance.yml",
    "run_id": 12345678901,
    "run_attempt": 1,
    "job": "control",
    "failed_step": "Poll instance until ACTIVE",
    "run_url": "https://github.com/MorphoCloud/Instances/actions/runs/12345678901",
    "runner": { "name": "js2-control-1", "labels": ["self-hosted", "control"] },
    "workflow_version": "8d37f8c"
  },
  "trigger": {
    "kind": "issue_comment",
    "command": "/unshelve",
    "issue_number": 1234,
    "comment_id": 2345678901,
    "actor": "someuser",
    "actor_association": "MEMBER"
  },
  "instance": {
    "name": "instance-1234",
    "flavor": "g3.xl",
    "image": "MorphoCloud-2026.09",
    "status_at_failure": "SHELVED",
    "task_state_at_failure": "unshelving",
    "floating_ip": null
  },
  "signature": "unshelve.stuck_shelved",
  "failure_mode": "",
  "failure_detail": "instance remained SHELVED for 20 minutes after unshelve",
  "capacity": {
    "flavor": "g3.xl",
    "available": 0,
    "source": "js2_availability"
  },
  "artifacts": ["openstack/server-show.json", "openstack/console-log.txt.gz"],
  "redaction": { "rules_version": 1, "replacements": 3 }
}
```

`workflow_version` is the commit of MorphoCloudWorkflow that was vendorized into
the source repo. Vendorize already stamps this into the target; the collector
reads it from the checkout. If it is missing, the collector records `unknown`
rather than failing.

### `outcomes/<run_id>.json`

Written by every command worker in its `always()` reporting step, whatever the
result. Deliberately tiny:

```json
{
  "schema_version": 1,
  "recorded_at": "2026-11-08T14:22:33Z",
  "repo": "MorphoCloud/Instances",
  "run_id": 12345678901,
  "workflow_file": "control-instance.yml",
  "command": "/unshelve",
  "issue_number": 1234,
  "instance": "instance-1234",
  "flavor": "g3.xl",
  "outcome": "failure",
  "signature": "unshelve.stuck_shelved",
  "queued_seconds": 412,
  "duration_seconds": 1388,
  "workflow_version": "8d37f8c",
  "incident_id": "20261108T142233Z-instances-12345678901-unshelve"
}
```

`queued_seconds` is run `started_at` minus `created_at`; it is the single most
useful number for the runner-starvation class of problem.

### `incident-signatures.yml` (lives here, vendorized)

A flat list, dotted names, each with a one-line definition and the detector that
assigns it. Seeded from what the actions already distinguish:

```yaml
version: 1
signatures:
  - name: create.no_capacity
    summary: server create rejected or failed with "No valid host"
    detector: control-instance failure_mode=no_capacity
  - name: create.host_error
    summary: server reached ERROR during build
    detector: control-instance failure_mode=host_error
  - name: unshelve.stuck_shelved
    summary: instance still SHELVED/SHELVED_OFFLOADED after the unshelve poll
    detector: control-instance verify_end_state
  - name: unshelve.no_floating_ip
    summary: instance ACTIVE but FIP associate failed or address never appeared
    detector: control-instance associate_ip / check_instance_has_ip
  - name: shelve.stuck_offloading
    summary: shelve never reached SHELVED_OFFLOADED
    detector: control-instance verify_end_state
  - name: setup.cloud_init_failed
    summary: cloud-init reported a failed stage in the console log
    detector: setup-instance instance_poll
  - name: setup.console_unreadable
    summary: console log could not be fetched from OpenStack
    detector: setup-instance instance_poll
  - name: setup.ssh_unreachable
    summary: sshd never answered after cloud-init finished
    detector: setup-instance
  - name: email.send_failed
    summary: credential or notification email could not be sent
    detector: send-email / course-send-email failure
  - name: command.dropped
    summary: worker cancelled out of the per-issue concurrency queue
    detector: report-dropped-command
  - name: drift.active_without_fip
    summary: reconcile - ACTIVE with no floating IP
    detector: reconcile-instances
  - name: drift.stuck_shelved
    summary: reconcile - SHELVED, offload never completed
    detector: reconcile-instances
  - name: drift.error_state
    summary: reconcile - instance in ERROR
    detector: reconcile-instances
  - name: drift.shelved_with_fip
    summary: reconcile - shelved but a FIP is still bound
    detector: reconcile-instances
  - name: dormant.never_succeeded
    summary: workflow triggered often, never succeeded in window
    detector: detect-dormant-workflows
  - name: dormant.stopped_succeeding
    summary: workflow used to succeed and stopped
    detector: detect-dormant-workflows
  - name: unclassified
    summary: a failure no detector matched; always eligible for triage
    detector: fallback
```

`unclassified` is the important one: it is the queue of things the vocabulary
does not yet describe. A growing `unclassified` count is itself a finding.

### Writing to the store

The GitHub App that already authenticates the workflows
(`MORPHOCLOUD_WORKFLOW_APP_ID`) is installed on `MorphoCloud/Incidents` with
**Contents: Read & Write**. The collector mints an app token for that repo, does
a shallow clone (`--depth 1 --filter=blob:none`, the store will be large), adds
its directory, commits, and pushes. On a non-fast-forward it fetches, rebases,
and retries up to 3 times; because writers never share a path, the rebase is
always clean. If the push still fails, the bundle is uploaded as a run artifact
(90-day retention) and a `::warning` names it, so the evidence is recoverable by
hand and the failure is visible without failing the job.

Per-file size cap: **5 MB** compressed. Console logs and job logs beyond that
are truncated head+tail with a marker line, and the full file goes to the
artifact fallback.

## Components

All collector code is in `MorphoCloudWorkflow` and reaches target repos only by
vendorize. Agent workflows are committed directly to `MorphoCloud/Incidents`.

### 1. New action — `.github/actions/capture-incident`

Composite action. Inputs:

| input                     | purpose                                                                                                            |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `app_id`, `private_key`   | to mint the store token                                                                                            |
| `store_repo`              | default `MorphoCloud/Incidents`; repo var so Test-Instances can point at a scratch store                           |
| `issue_number`, `command` | trigger context (may be empty for scheduled jobs)                                                                  |
| `instance_name`           | drives the OpenStack capture; empty skips it                                                                       |
| `signature`               | assigned by the caller from its own step outcomes; defaults to `unclassified`                                      |
| `failure_mode`, `detail`  | passed through from the existing outputs                                                                           |
| `failed_step`             | caller names the step; composite actions cannot see their own step outcomes generically                            |
| `extra_files`             | newline-separated paths the caller wants bundled (e.g. a poll transcript it already wrote to `${{ runner.temp }}`) |

Steps, each guarded so one failed capture never aborts the rest:

1. **Snapshot OpenStack** (only if `openstack` is on `PATH`, i.e. self-hosted):
   `server show -f json`, `server event list -f json`, `console log show`,
   `port list --server`, `floating ip list --port`, `volume show` for attached
   volumes. Each is a separate command with a 60 s timeout.
2. **Snapshot the instance** if `status == ACTIVE` and it has a floating IP:
   over the same restricted SSH path setup-instance uses, fetch
   `/var/log/cloud-init-output.log` and `journalctl -b --no-pager | tail -2000`.
   15 s connect timeout; failure is noted, not fatal.
3. **Snapshot the runner**: hostname, labels, uptime, load, free disk, and the
   run's `created_at`/`started_at` from the Actions API (for `queued_seconds`).
4. **Capacity**: the current availability for the instance's flavor from the
   same read-only endpoint the workshop capacity check uses; recorded with its
   source, or `null`.
5. **Redact** every text file (see Redaction).
6. **Assemble** `incident.json` and `narrative.md`; validate against the schema
   with `check-jsonschema` (already a pre-commit dependency, available via pipx
   on the runner).
7. **Push** as described above; emit `incident_id` and `incident_url` outputs so
   the caller can include the link in its failure comment.

Runs with `continue-on-error: true` at every call site. If `openstack` is absent
(an `ubuntu-latest` job), steps 1-2 are skipped and the bundle still records the
GitHub side.

### 2. New action — `.github/actions/record-outcome`

Tiny composite. Called from the existing `always()` outcome-reporting step of
every command worker (the `report-command-outcome` sites), with `outcome`,
`signature` and `incident_id` as inputs. Writes one `outcomes/<run_id>.json` and
pushes with the same retry logic. Shares its push helper script with
capture-incident (`scripts/incident-store-push.sh`).

### 3. Integration sites

| site                                                  | what changes                                                                                                                                                                  |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.github/actions/control-instance`                    | `capture-incident` step beside "Notify admins by email (failure)" with the same `if:`; signature chosen from `failure_mode` and which `check_*`/`verify_*` step failed        |
| `.github/actions/create-instance`                     | capture on `ip_create`/`server create` failure                                                                                                                                |
| `.github/actions/setup-instance`                      | capture on `instance_poll` failure with `failure_detail` and the console-log text it already fetched passed via `extra_files`                                                 |
| `create-instance.yml`, `create-course-instance.yml`   | capture on the provisioning failure branch and on `send_email` failure                                                                                                        |
| `send-email.yml`, `send-renewal-email.yml`            | `email.send_failed`                                                                                                                                                           |
| `reconcile-instances.yml`                             | one `drift.*` incident per _new_ violation (the existing "already flagged" check prevents re-recording); clearing a label records an `outcomes` file with `outcome: resolved` |
| `detect-dormant-workflows.yml`                        | one `dormant.*` incident per finding                                                                                                                                          |
| `report-dropped-command.yml`                          | `command.dropped` incident                                                                                                                                                    |
| every command worker's outcome step                   | `record-outcome`                                                                                                                                                              |
| failure comments that say "admins have been notified" | append `Incident: <url>` so a human reading the issue can jump to the evidence                                                                                                |

### 4. New workflow — `.github/workflows/archive-run-logs.yml`

`workflow_run` on `completed` for every workflow that calls `capture-incident`,
filtered to `conclusion != 'success'`. Job logs are only downloadable after the
run completes, so the collector cannot fetch its own. This workflow downloads
the failed job's log via the Actions API, redacts it, gzips it, and commits it
into the matching incident directory (matched by `run_id` in the path). Same
pattern as `report-dropped-command.yml`, same caveat: `workflow_run` only fires
for the default-branch copy, so it does nothing until merged and vendorized.

If no incident directory exists for the run (the collector itself failed), it
creates a minimal one with signature `unclassified` and a note. That is the
collector's own safety net, in the spirit of the dormant detector.

### 5. Triage agent — `triage-incident.yml` in `MorphoCloud/Incidents` (Phase 2)

Trigger: `push` to `main` touching `incidents/**/incident.json`. One job per new
incident. Runs `anthropics/claude-code-action` on `ubuntu-latest` with the
Claude token (copied as a secret into the Incidents repo) and an app token that
can read the source repo's issues and write comments.

The prompt gives the agent the incident directory, the last 30 days of `triage/`
records and `outcomes/` for the same instance, and the signature vocabulary. It
must write `triage/<id>.json`:

```json
{
  "incident_id": "...",
  "verdict": "page | digest | suppress",
  "signature": "unshelve.stuck_shelved",
  "signature_confidence": "high | medium | low",
  "proposed_signature": null,
  "duplicate_of": null,
  "user_waiting": true,
  "summary": "two sentences, plain language",
  "reasoning": "what in the bundle supports the verdict",
  "actions": ["commented on Instances#1234"]
}
```

Verdict policy, stated in the prompt and enforceable by a human reading the
record:

- **page** - a user is waiting on a failed create/unshelve; an instance is in
  ERROR; anything on a delete path; a credential email failed; a reconcile
  violation on an active instance; or the agent cannot classify. A following
  step sends one email to the real admin address with the summary and links.
- **digest** - dormant findings, failures the user already retried successfully,
  drift on a shelved instance. Collected by a twice-daily `digest.yml` into one
  email.
- **suppress** - duplicate of an open incident on the same instance and
  signature; known-transient GitHub API errors; user-caused failures.

Two rules override the verdict and are implemented in shell, not left to the
agent: a **repeat** (same instance and signature within 24 h) is promoted to
`page`; any `suppress` or `digest` older than 12 h whose instance is still not
healthy in the latest `outcomes/` is promoted to `page`. A **weekly heartbeat**
email lists everything suppressed; its absence is the alarm.

Tool allowlist: `gh api` GET on the source repo, `gh issue comment`,
`gh issue view`, and file writes inside `triage/`. Nothing else.

Shadow mode is the same workflow with the email step disabled. The comparison at
the end of shadow is manual: for each week, the verdicts versus the emails you
actually acted on.

### 6. Analysis agent — `analyze-incidents.yml` in `MorphoCloud/Incidents` (Phase 3)

`schedule` weekly (off the hour) and `workflow_dispatch`. The job installs
DuckDB, checks out the store, and gives the agent a ready-made set of views over
`incidents/**/incident.json`, `outcomes/**/*.json` and `triage/*.json`. The
prompt asks for, in order:

1. **Base rates** for the window and the trailing 90 days: failure rate per
   command, per signature, per flavor, per `workflow_version`, and queue-wait
   distribution. Pure SQL; the agent reports, it does not estimate.
2. **Changes.** Signatures whose rate moved materially versus the prior window,
   with the first `workflow_version` they appear on.
3. **Threads.** For each cluster worth attention, read the narratives and logs
   of a sample, and state a hypothesis with the incident ids that support and
   contradict it.
4. **Proposals.** Where a hypothesis points at this repository, open an issue on
   MorphoCloudWorkflow (or a draft PR when the fix is small and local) citing
   the incidents. Where it points at Jetstream2, write it up for the ACCESS
   ticket the maintainer will file.
5. **Vocabulary.** A PR adding any `proposed_signature` from triage that
   recurred at least 3 times.

Output: `reports/<ISO week>.md` committed to the store, plus the issues/PRs.
`reports/patterns/<slug>.md` pages are living documents the agent updates when a
thread gains or loses evidence, so a thread's history is one file.

Tool allowlist: DuckDB, read access to the store, `gh issue create` and
`gh pr create` on MorphoCloudWorkflow only, and `gh api` GET.

## Redaction

Applied by the collector to every text file before it is written to the clone,
and again by `archive-run-logs` to job logs. Rules are a versioned list in
`scripts/incident-redact.sed` (stamped into `incident.json` as
`redaction.rules_version`):

- GitHub tokens (`ghp_`, `ghs_`, `github_pat_`), app private-key blocks, the
  SMTP password if it ever echoes, OpenStack application-credential secrets,
  `OS_PASSWORD`.
- The per-instance passphrase and VNC password lines as they appear in
  cloud-init output and setup-instance logs.
- Email addresses, replaced by a stable hash so the same user can still be
  correlated across incidents without storing the address.
- Anything between `::add-mask::` markers in a run log.

A unit test in `ci.yml` runs the rules against a fixture containing one of each
and asserts nothing survives. The test is the only place the secret shapes are
spelled out with examples.

## Retention and size

- Incidents and outcomes: **kept 2 years**, then a scheduled job in the store
  moves the year to a `cold/` branch and removes it from `main`. Reports are
  kept indefinitely.
- Budget: a bad week is perhaps 20 incidents at 1-3 MB compressed; a year is on
  the order of 1 GB worst case, comfortably under GitHub's soft limits. If a
  quarterly size check in the store exceeds 3 GB, raw `logs/` move to Jetstream2
  object store and the incident directory keeps a `logs.url` pointer. That is
  the only change the consumers would see.
- `check-added-large-files` in the store's pre-commit is set to the 5 MB cap so
  a mistake in the collector cannot bloat the repo silently.

## Phased rollout (each tested on Test-Instances first)

| phase | scope                                                                                                                                                                    | exit criterion                                                                                                                                                                                                                |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0     | Create `MorphoCloud/Incidents` (private), install the GitHub App on it, add schema files and README. Create a scratch `MorphoCloud/Incidents-test` for Test-Instances.   | App token can push to both.                                                                                                                                                                                                   |
| 1     | `capture-incident`, `record-outcome`, `incident-signatures.yml`, redaction rules + test, `archive-run-logs.yml`, integration at every site. Vendorize to Test-Instances. | Two weeks of Test-Instances traffic, including deliberately provoked failures (shelve a fake instance, kill a create, request an impossible flavor), each producing a complete, redacted bundle. Then vendorize to Instances. |
| 2     | Triage agent in shadow mode on the real store. Twice-daily digest and weekly heartbeat wired but emailing only the alerts mailbox.                                       | 3 weeks; the verdict log agrees with what the maintainer would have paged on, and `unclassified` is below 10% of incidents. Then flip `MORPHOCLOUD_GITHUB_ADMIN_EMAILS`.                                                      |
| 3     | Analysis agent weekly. First run is backfilled over everything collected so far.                                                                                         | A report with at least one hypothesis the maintainer judges worth a code change.                                                                                                                                              |
| 4     | Vendorize the collector into `MorphoCloudCourseTemplate` and existing `MC-*` repos. Course incidents land in the same store with `source.repo` set.                      | One course cycle.                                                                                                                                                                                                             |

## Parameters to confirm (recommended defaults)

- **Store repo names:** `MorphoCloud/Incidents` and
  `MorphoCloud/Incidents-test`.
- **Alerts mailbox:** a new address on the existing mail domain receives the
  unchanged direct emails after Phase 2 as an audit trail; the maintainer's
  address receives only what triage pages.
- **Repeat window** for forced promotion to `page`: 24 h. **Stale window**: 12
  h.
- **Digest cadence:** 08:00 and 17:00 in the maintainer's local timezone.
- **Heartbeat:** Monday morning, same slot as the dormant detector.
- **Instance-side capture over SSH:** on by default; off via a repo variable if
  it ever proves slow or noisy.
- **Model for the agents:** whatever the Claude subscription token provides
  (same as the PR reviewer); no per-token bill.

## Out of scope for now

- Self-remediation by the agent (re-dispatching the setup watchdog, retrying an
  unshelve). Revisit after Phase 3 with evidence of which retries are safe.
- A dashboard over the store. DuckDB in a checkout and the weekly report cover
  the need; the data portal (INSTANCE_DASHBOARD_PLAN.md) is for users, not
  operators.
- Capturing anything from successful runs beyond the outcome line.
- Reading the mailbox. Every email the system sends already has a GitHub-side
  event; the store is fed from those, never from mail.
