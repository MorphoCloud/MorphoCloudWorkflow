# Incident Store and Operations Agent — Design & Plan

**Goal.** An autonomous operations agent for MorphoCloud that, for every
failure, decides correctly whether to fix it on its own or contact the
maintainer, and that finds the recurring themes across failures and proposes the
rule that would end each one. This plan builds the record that agent learns
from, the agent itself, and the gate that lets it act.

**Status (2026-11-08):** proposed, nothing approved, nothing built. Built on
`share-prototype` (c9711a2), which reaches production with the 2026-11-23
migration. Supersedes the `main`-based draft on `ccr-028ab0f6-ximhmd`.

**Amended 2026-11-08 (collection only).** The maintainer delegated the open
collection decisions with two priorities: robustness first (nothing found
missing from the record weeks later), simplicity second. Both repositories were
re-swept against the inventory, this one at `share-prototype` a159be7 and the
portal at its `share-prototype` abbbd74. The sweep found twelve reads, logs and
tables this repository's inventory had missed, six claims the code contradicts,
and a section G that would have lost most of what it promised. The changes are
marked **(amended)** below and summarized in decision 20. The maintainer's third
priority, named afterwards, is **cost** (Actions minutes and storage): not
critical, kept in view in "Retention, size, cost". The amendment was merged
2026-11-08 (#400), which approves the collection part. Phase 1 is designed PR by
PR in `INCIDENT_STORE_PHASE1_DESIGN.md`, each PR's design approved before its
code.

**Under review now: data collection only.** That is decisions 1–6 and 18–20,
"Data collection", "Signatures", "Redaction and privacy", "Retention, size,
cost" and Phase 1. Decisions 7–17 and every section about the agent (triage,
playbooks, analysis, the agent among the other actors, Phases 2 onward) record
the intended direction. They are **not** under review, and approving collection
does not approve them.

**The order is fixed: record first, judge later.** An agent that starts deciding
on day one meets every case for the first time, and first encounters are where
it gets things wrong. So for the first two months nothing but data is collected:
every outcome (successes included), every incident, and the maintainer's call on
each. Before the agent sees a live case, it is replayed over that history and
graded against what the maintainer actually did. After that, a case type with
too little history still goes to the maintainer (decisions 16 and 17).

## Where we start

The prototype already runs a set of deterministic automatic repairs. Each one
was written by hand after an incident someone traced:

| Mechanism                                    | Decision (INSTANCE_DASHBOARD_PLAN.md) | Born from                 |
| -------------------------------------------- | ------------------------------------- | ------------------------- |
| Unreachable 30 min → shelve, tell both sides | 23, 26                                | Test-Instances#503        |
| Floating-IP policing                         | 24                                    | Jetstream2 address policy |
| Bind the address only after ACTIVE           | 25                                    | 17 host-side failures     |
| Abandoned command finished by follow-through | 28                                    | Instances#214             |
| ACTIVE without address repaired              | 34                                    | Instances#214             |
| Slow setup watched, not failed               | 33                                    | review backlog #228       |
| One diagnosed email per incident             | 27                                    | months of silent alerts   |

The pattern is always the same: an incident, a diagnosis, then a rule. The
maintainer is the step in between. The agent automates that step. It does not
replace these rules; it decides the cases they do not cover, and proposes the
next rule when a case keeps coming back.

The prototype also has the right points to record from:

- `notify-admins` (decision 27): every workflow-detected incident, with a
  failure class read from step outcomes (host failure, no address, address not
  attached, wrong end state, API error, timeout, setup not finished, credentials
  email failed, unhandled). A failed send leaves `admin-email-failed`.
- `admin-alert.yml`: everything the runner has to say (address drought,
  non-routable address, unreachable shelve, "could NOT be shelved"), plus
  decision 35's member notes.
- `report-command-outcome`: the 👍/👎 of every command (7 workflows).
- `record-command-context` + `report-dropped-command.yml`: commands evicted from
  the per-issue queue.
- `collect-instance-diagnostics`: Nova's fault and action log, the only record
  of why a build or unshelve failed, which disappears with the server.
- Markers on the issue: `followup:<cmd>`, `followup-count:<n>`, `needs-admin`,
  `invariant-violation`, `admin-email-failed`.
- The runner clock (decision 21) and report-only-first for new automation on
  production (decision 35).
- The actor map in AUTOMATION_AUDIT_2026-11.md.

## What is missing for an agent

1. **Evidence evaporates.** Run logs expire at 90 days; the Nova action log goes
   with the server; `~/fip-police.log` and the portal's database live on single
   VMs. Postmortems (Instances#110, #139, #166, #168, #214) were reconstructed
   by hand.
2. **No denominator.** We know an unshelve failed, not how often unshelves fail
   per flavor, hour, or workflow version.
3. **No link to the code version.** Vendorize records the MorphoCloudWorkflow
   SHA only in a commit subject (`fix: Update to MorphoCloud/…@<sha>`), which a
   depth-1 checkout does not see unless that commit is HEAD.
4. **No record of what the maintainer decided.** Whether an email needed a
   human, and what the human did, exists only in the maintainer's head. Without
   it there is nothing to train the agent's judgment on, and no way to measure
   it before trusting it.
5. **Admin email has many senders.** Besides `notify-admins` and
   `admin-alert.yml`, these send directly: `follow-through`,
   `reconcile-instances`, `delete-instance-and-volume`, `expire-shares`,
   `guard-issue-close`, `detect-dormant-workflows` and `on-admin-mention`. The
   portal mails on its own. No single place sees them all.

## Decisions (proposed — none approved)

1. **The store is a private repository, `MorphoCloud/Incidents`, and it collects
   production only (Instances).** Production is where the members and the
   failures are. Test-Instances is the test bed where collection is built and
   tested, writing to a throwaway `MorphoCloud/Incidents-sandbox` that is
   deleted after Phase 1. It is not a data source. Everything lives in git, full
   logs included (gzipped): about 330 MB a year (see "Volume"), so no object
   store is needed. DuckDB over a checkout is the query engine.

2. **One writer, pulling.** Jobs never write to the store. They write their
   facts into a run artifact. **One harvester**, on production's acquire runner,
   reads every completed Instances run through the Actions API, along with the
   runner host's logs and, after the migration, the portal's events. It is the
   store's only writer, so nothing races. Nothing is added to the per-issue
   lock, and no workflow holds a store credential. Every run is recorded even
   when its job died before its last step. Each admin-email sender records the
   subject and body in its facts where it already sends (B10); a CI check finds
   the senders, so the list is never kept by hand. No sender is moved.
   Consolidating the senders onto `notify-admins` changes production alerting
   and is a separate PR, outside this plan.

3. **The collector is deterministic, no LLM, and never masks the real failure**
   (`continue-on-error: true` on every facts step). Its gaps are visible, never
   silent: a run without facts is recorded as `facts: missing`; a harvester that
   falls behind alerts once; the crontab dispatcher's log records the sweeps
   that never became a run. Facts are **redacted inside the job, before the
   upload**, and again by the harvester, because a run artifact can be
   downloaded by anyone with read access to the source repository, members
   included; GitHub's masking covers the rendered log, not artifacts. The
   artifact is also encrypted to the harvester's public key and kept 7 days (see
   "Redaction and privacy").

4. **Successes are recorded, with the same depth where it matters.** Every
   command run is a row. Every create and unshelve keeps its Nova compute-host
   id, stage timings and, for creates, the full setup console. A failure can
   only be called abnormal against what normal looks like.

5. **The evidence inventory is the contract.** "Evidence inventory" below lists
   every place evidence exists at the moment of a failure, built from the code
   (every diagnostic read, every log file, every portal table), not from memory.
   **(amended)** The first draft claimed that and was not: a second sweep on
   2026-11-08 found twelve reads, logs and tables it had missed (the
   **(amended)** rows below). So the inventory is kept honest by three CI checks
   and by capturing classes rather than named items (decision 20), not by
   diligence. Each entry is captured, deliberately excluded with a reason, or
   marked as a blind spot. A new workflow, log file or table is added to it in
   the same PR. When a postmortem finds it needed something that was not
   captured, the inventory gets the entry first.

6. **The maintainer's verdict is recorded and is the ground truth.** Every
   incident that reached the maintainer can be marked with what should have
   happened and what was done (see "The maintainer's verdict"). Admin-dispatched
   commands on the same issue within 24 h are recorded as implicit labels. The
   agent is measured against these records before it is trusted, and keeps being
   measured after.

_Decisions 7–17 describe the agent: direction only, not under review now._

7. **The agent proposes; a deterministic executor disposes.** The agent holds no
   credential that can change anything: it reads a case file prepared for it and
   writes a decision record. Acting is done by `incident-act.yml` (vendorized,
   in the source repository). It reads the decision, checks it against the
   playbook catalog, re-reads live state, enforces the budget, and dispatches
   the existing workflow that does the work. The agent's only reach is the
   decision it writes. A prompt injection through an issue's text can cost a
   wrong decision record, never an action outside the catalog.

8. **Autonomy is earned per playbook, never granted globally.** Each playbook (a
   named action with preconditions, an existing workflow to dispatch, and a
   post-check) climbs from shadow to confirm to act on measured agreement with
   the maintainer. It drops back automatically on an override or a failed
   post-check. Decision 35 applies on top: a week report-only on production
   before any playbook acts there.

9. **The agent's actions are the existing ones.** A playbook dispatches what the
   prototype already runs (`control-instance-from-workflow`, `resume-setup`,
   `admin-alert` notes, a rerun of a failed job). It never calls OpenStack,
   edits labels it does not own, deletes, touches a share, or changes approval
   or expiry. Member-facing text is templated per playbook, never free text from
   the model.

10. **The runner is the clock (decision 21).** The digest, the stale check, the
    completeness audit and the analysis are `workflow_dispatch` jobs in the
    store, fired from lines `check-workflow-clock.py` renders into **both**
    runner crontabs (not the production-only `.extra` file), each with a
    `# dispatch:` marker. Triage is event-driven: a push to the store.

11. **Agents run on a self-hosted runner, as a separate unix user with no
    OpenStack credentials and no dispatcher token** (label `incidents-agent`, on
    the Test runner host). This costs no Actions minutes; `ubuntu-latest` in a
    private repo bills against the $50 budget. The only secret the agent step
    sees is `CLAUDE_CODE_OAUTH_TOKEN`.

12. **Bursts are episodes, not pages.** Before any agent run, a deterministic
    grouper puts incidents of one signature family on 3 or more instances within
    30 minutes into one episode, as it does when `check-js2-status` reports a
    Jetstream2 incident. One episode is one triage run and at most one page;
    later members of an open episode attach to it without a new run.

13. **Safety prerequisites come before any action.** The executor acts only
    after audit classes 2 and 3 are fixed: every automated dispatch carries a
    reason, `control-instance` re-checks it under the per-issue lock, and a
    dropped automated run is re-dispatched rather than lost. Without those, the
    agent is one more actor deciding from a stale read. Recording and shadow
    triage do not need them.

14. **Scope: Instances (production).** Test-Instances is the test bed, not a
    source (decision 1). Course repositories (`MC-*`) get the collector later.
    They have no runner clock (decision 21 exempts them), so their scheduled
    parts are out of scope until that changes.

15. **Kill switch.** A repository variable `MORPHOCLOUD_AGENT_MODE` on each
    source repository (`off`, `shadow` or `act`) is read by the executor on
    every run. `off` stops all actions immediately; recording continues.

16. **Baseline before judgment.** No agent runs, not even in shadow, until the
    store holds a baseline: **8 weeks of full recording, counted from the
    2026-11-23 merge** (the first day production records facts, so no earlier
    than 2027-01-18), **and** at least 100 incidents carrying a maintainer
    verdict other than `test-induced`. What is recorded before the merge (run
    history, logs, the 90-day backfill) is context and does not count toward the
    8 weeks. The later of the two ends the baseline. During the baseline,
    today's emails are unchanged and each one gains a link to record the
    verdict. The baseline then serves as a test set: the agent is replayed over
    it before it sees a live case (Phase 4), with each case file rebuilt **as of
    the incident's time**: only what was recorded before it, never the verdict
    or what happened next.

17. **Novelty means a human, permanently.** A signature with fewer than 10
    verdicts, or whose verdicts disagree with each other (under 90% the same
    call), is _novel_. A novel case is always paged, whatever the agent decided.
    The agent's summary may ride along, but its decision is recorded only as
    shadow. `unclassified` is always novel. This holds after launch, not only at
    the start: a new failure type arriving in month six is treated exactly like
    one in week one. A playbook can leave shadow only on a signature that is not
    novel.

18. **Collection runs on the runner host; the portal VM only answers a read.**
    The harvester is a crontab script on each allocation's runner host. The jobs
    write their facts where they already run. The portal VM gains one read-only
    gate command and one table, and holds no GitHub credential (decision 20 of
    INSTANCE_DASHBOARD_PLAN.md). See "Where it runs". **The runner's OpenStack
    credential is not read-only:** its `clouds.yaml` is the allocation's
    credential, and the floating-IP policing script creates and deletes
    addresses with it. The harvester gets its own application credential,
    restricted by Keystone access rules to read calls on compute and network, if
    Jetstream2 allows it. Otherwise it uses the runner's credential through a
    wrapper that permits only a fixed list of `show`, `list`, `event list` and
    `console log show` calls, enforced by a test, and that risk stands recorded
    here.

19. **Collection does not slow what members see.** On every success path, no new
    network call before the 👍/👎. What can be read afterwards is read by the
    harvester. Failure captures come after the failure comment. There is one
    stated exception: the evidence a shelve would destroy (D1, D2) is taken
    before the shelve, even when the shelve comes before the command's outcome.
    Every capture in a job is capped. Measured on Test before merge: no
    measurable change in time to 👍/👎, and the extra lock time within the table
    in "What it costs member operations".

20. **Capture classes, not lists (amended 2026-11-08).** Every list in the first
    draft (nine email senders, six command workflows, five dispatch reasons, six
    markers, nine portal tables) was wrong when checked against the code, and a
    list is wrong again the first time a workflow changes. So wherever the plan
    named items it now names the class and a check that enumerates it:
    - the **full job log of every command run** is kept, not only of incidents
      and creates, so anything a job printed that the facts did not name (the
      image, the project instance count, a volume self-heal, a share id) is
      still in the record. The facts are an index into the logs, never the only
      copy;
    - **every label** on the issue at the time is recorded, not a listed subset;
    - **every line of every runner-side log** is shipped from its last offset,
      not filtered for the lines the plan expected;
    - the portal writes **one append-only `portal_events` table** at every
      event, and that one table is exported, instead of nine tables with five
      different retention rules;
    - three **CI checks** in MorphoCloudWorkflow enumerate the sites the plan
      used to list: every `gh workflow run` call site logs or records its
      dispatch (`scripts/check-dispatch-logging.py`); every job on a self-hosted
      runner ends with the facts upload (`scripts/check-facts-upload.py`); every
      admin `action-send-mail` step records its subject and body
      (`scripts/check-admin-email-facts.py`). A new workflow fails CI until it
      is in the inventory.

    The cost is about 11 MB a month more in the store (see "Volume").

## Architecture

```
 Instances (jobs)                             production acquire runner
 ┌─────────────────────────────────┐         ┌──────────────────────────────────┐
 │ facts.sh: redact, then encrypt  │         │ crontab (decision 21)            │
 │   → mc-facts-<job>-<attempt>    │         │  harvest-incidents.py */5        │
 │ (no store credential, no push)  │◀── API ─│   1 runs + jobs + facts + logs   │
 └─────────────────────────────────┘  read   │   2 runner logs (dispatch, fip,  │
                                             │     pickup, runner _diag)        │
 data portal VM                              │   3 portal events (SSH gate)     │
 ┌─────────────────────────────────┐         │   4 late OpenStack snapshot      │
 │ export-events --since (gate)    │◀── SSH ─│   5 classify, redact, write      │
 │ portal_events (new, append-only)│         └───────────────┬──────────────────┘
 └─────────────────────────────────┘                         │ one commit / cycle
                                                             ▼
 MorphoCloud/Incidents (private): production       watchdog.yml (GitHub-hosted)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ outcomes/ rollups/ incidents/ portal/ runner/ verdicts/  (collection)    │
 │ episodes/ decisions/ reports/                            (agent, later)  │
 └──────────────────────────────────────────────────────────────────────────┘
```

## Data collection

### Two layers, one writer

| Layer        | What                                                                                                                            | Source                                                                                                     | Covers                                                                      |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **Envelope** | that a run happened: workflow, trigger, actor, attempt, timings, conclusion, jobs and steps, the first failed step, runner, log | Actions API, after the run completes                                                                       | every run, including crashed, cancelled and dropped ones                    |
| **Facts**    | what the run was about and what it saw, including files (consoles, diagnostics, the admin email)                                | the job, through `.github/actions/record-facts` (`facts.sh set <key> <value>`, `facts.sh add-file <path>`) | every run of an instrumented workflow; uploaded in the last `always()` step |

`record-facts` writes into `$RUNNER_TEMP/mc-facts/`, redacting every value and
file as it is written (the same rules file the harvester uses). Before the
upload the directory is encrypted to the harvester's public key
(`openssl cms -encrypt`; the certificate is a repository variable, the private
key stays on the runner host). The upload goes to artifact
`mc-facts-<job>-<run_attempt>` with `overwrite: true` (a rerun reuses the run,
and a repeated name would fail), 7-day retention, `continue-on-error` and
`timeout-minutes: 1`. **(amended)** The upload is a workflow-level step, not a
step inside `report-command-outcome`: composite actions accept
`continue-on-error` but not `timeout-minutes`, and the cap is what keeps a
GitHub incident from holding the per-issue lock for minutes. It is the last
`if: always()` step of **every job that runs on a self-hosted runner**, placed
**after** `report-command-outcome` where that is called, so the 👍/👎 comes
first. The class is every self-hosted job, and the CI check
`scripts/check-facts-upload.py` (decision 20) is the authority on which
workflows that is; the list here is a snapshot of `share-prototype` a159be7,
where 22 workflows run on a self-hosted runner: the six that call
`report-command-outcome` (control-instance, create-instance,
create-course-instance, delete-instance-and-volume, extend-session, send-email);
four command-style workflows that do instance work without it
(`control-instance-from-workflow`, `create-instance-from-workflow`,
`resume-setup`, `migrate-volume`); and twelve sweeps and alerts
(`automatic-instance-shelving`, `close-expired-issues`,
`collect-instance-uptime`, `expire-shares`, `follow-through`,
`guard-issue-close`, `reconcile-instances`, `send-renewal-email`,
`test-workshop-deletion`, `update-request-status-label`, `update-workshop`,
`workshop-backfill`). Among the sweeps, `close-expired-issues` is the one that
changes OpenStack itself (it detaches and deletes the volume before it
dispatches the delete), so its B11 items carry those two actions. Three command
workflows (`create-workshop`, `approve-workshop`, `update-renew-label`) run on
`ubuntu-latest`, only edit labels and comments or hand the work to
`create-instance-from-workflow`, and upload nothing; their runs are recorded
from the envelope. Among the GitHub-hosted jobs only the admin-email senders
upload (see "Where it runs"). Every `run-name:` gains the issue number:
`#${{ github.event.issue.number || inputs.issue_number }} ${{ github.workflow }}`.

### Where it runs

| Where                    | What runs there                                                                                                                                                               | Machine                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| inside the workflow jobs | the facts (B), the setup log (C), the pre-shelve capture (D): local file writes and one artifact upload                                                                       | the runner each job already uses: the self-hosted control runner for instance work, `ubuntu-latest` for the small jobs that send admin email (`admin-alert`, `on-admin-mention`, `detect-dormant-workflows`, `request-notify-admin`, `request-notify-admin-workshop`; **amended:** `report-dropped-command` sends no email and uploads nothing, its runs are classified `dropped` from the envelope). The authorize jobs upload nothing; they print one `REFUSED` line (B8). Nothing new is scheduled. |
| **the harvester**        | everything else: reads the Actions API, classifies, redacts, takes the late snapshot, reads the runner logs, pulls the portal events, reads outside context, writes the store | **production's acquire runner**, where the dispatcher lives, from its crontab                                                                                                                                                                                                                                                                                                                                                                                                                          |
| the portal VM            | only the read-only gate command `export-events`, and the new append-only `portal_events` table written at every event (G)                                                     | the portal VM, reached over the SSH gate the runner's `pickup` already uses                                                                                                                                                                                                                                                                                                                                                                                                                            |

The runner host and not the portal VM because:

- the runner host already has what the harvester needs: OpenStack access for its
  allocation (not read-only; see decision 18), the logs it reads (F), the
  crontab clock (decision 21) and the SSH path to the portal;
- the portal VM is member-facing and by design holds no GitHub credential
  (decision 20), so a write credential and a batch job there would widen what
  the web app exposes;
- production has a runner now and no portal VM until the migration.

New on the runner host: one crontab line, the script, a fine-grained token and
two keys (see "The harvester"). Production runs two runners. The production
harvester reads the second runner's `_diag` logs (F5) over SSH, and installs
nothing there.

### Production only, and before the merge

Production runs `main` plus hotfixes until the 2026-11-23 merge. `record-facts`,
the crontab renderer (`check-workflow-clock.py`) and `admin-alert.yml` exist
only on `share-prototype`. So collection on production comes in two steps:

| Production                      | Recorded                                                                                            | How the harvester is installed                                                                                                                                                                                                                                                                                                                                                                                                                | Alerts                                                                                                                                                              |
| ------------------------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **before the merge**            | what needs no workflow change: A (runs, jobs, logs), E, F (runner logs), H, and the 90-day backfill | **by hand**: the script copied to the acquire runner and its crontab line added outside the dispatcher's managed block, the way the Test runner's portal `pickup` line is kept. `restore-instances-dispatcher.sh` is never run from `share-prototype` against production before the merge: it would render production's crontab from the prototype's workflows and schedule workflows `main` does not have (follow-through, expire-shares, …) | the store watchdog (see "When collection fails"); token expiry through `check-dispatch-tokens.sh`, which already alerts through Analytics' `token-expiry-alert.yml` |
| **from the merge (2026-11-23)** | everything: the facts (B, C, D) and, with the migration, the portal (G)                             | the harvester lines move into the rendered crontab (`RUNNER_SCRIPTS`) and the hand-added lines are removed in the same step                                                                                                                                                                                                                                                                                                                   | the same                                                                                                                                                            |

Runs by a test account (amm554, listed in `incident-sources.yml`) are recorded
as `actor_kind: test` at the source and kept out of the base rates
automatically, so a deliberate test on production needs no label.

### What it costs member operations

What the member sees is when the 👍/👎 reaches the portal card. What the next
command waits on is the per-issue lock. The rules:

1. **No new network call before the 👍/👎 on any success path.** The job only
   writes local files (milliseconds): the console `setup-instance` already reads
   every poll is kept rather than discarded, and `hostId` is parsed from the
   `server show` it already does. The artifact upload comes after the reaction,
   capped at one minute: GitHub's upload retries on its own, and uncapped it
   could hold the lock for minutes during a GitHub incident.
2. **What can be read later is read by the harvester, not the job.** Capacity at
   the time comes from Jetstream2's Grafana vacancy history for every outcome,
   success or failure (**amended:** the in-job availability read exists at one
   site only, the no-capacity unshelve, so it cannot be the source; it stays
   where it is for the member's message and is kept as a fact there); the labels
   at the time come from the issue's timeline events. Jetstream2 outages come
   from the ACCESS outages feed. GitHub incidents come from githubstatus.com's
   incident history.
3. **Failure captures (C3, C4) come after the member's failure comment,** under
   one 30-second cap, so a failure is reported as fast as today. **(amended)**
   There is no SSH session to reuse: each `ssh` in `setup-instance` is its own
   connection, and the poll fails before the first one. So the capture opens a
   connection only after `fip.py verify` has passed on that address (the
   identity gate of audit class 1), inside the same cap; when verify fails or
   times out, the facts record `cloud_init_logs: unverified-address` and nothing
   is read.
4. **The one exception: evidence a shelve would destroy is taken before the
   shelve**, capped at 30 seconds.
   - In `control-instance`, the shelve-again of an unshelve or create that ended
     without an address or unreachable (D2) runs before the command's outcome,
     so the member's 👎 on that path comes a few seconds later.
   - In an automatic shelve of an unreachable instance (D1), the shelve starts
     about 10 seconds later, after 30 minutes of unreachability.
   - Before the address repair of decision 34, `reconcile-instances` takes the
     capture before it dispatches; no member command is waiting.
5. **The portal is not slowed.**
   - `export-events` is its own SSH command, separate from the `state` and
     `connection` pushes.
   - The portal's database runs in WAL mode, so a read never blocks a write, and
     each call is limited in rows.
   - The harvester is a separate crontab entry and never takes the `pickup`
     lock.
6. **The harvester stays off the member path.** It is a cron script with its own
   token, so it does not draw on the workflows' GitHub rate limit, and it makes
   about 4 OpenStack calls per incident.

| Operation                                             | Before the 👍/👎       | Lock held longer                      |
| ----------------------------------------------------- | ---------------------- | ------------------------------------- |
| create, unshelve, shelve, delete, email: success      | ~0                     | ~2–5 s (upload)                       |
| create fails in setup                                 | 0                      | ~5–15 s, at most 30 s                 |
| unshelve or create ends without address / unreachable | a few seconds (rule 4) | ~10 s, at most 30 s                   |
| automatic shelve of an unreachable instance           | n/a                    | ~10 s before the shelve, at most 30 s |
| portal card and buttons                               | 0                      | none                                  |

The estimates are measured on Test before anything merges (Phase 1 exit).

### Evidence inventory

Built from the code on `share-prototype` (a159be7) and the portal's
`share-prototype` (abbbd74): every `openstack` read, every SSH read, every log
file a crontab writes, every portal table, every admin-email step, every
`gh workflow run` call site. Re-swept 2026-11-08; rows marked **(amended)** were
wrong or missing in the first draft. **Lost** says when the evidence is gone if
it is not taken. ✔ = captured by this plan, ✘ = deliberately not, with the
reason.

**A. The GitHub run** (harvester)

| #   | Evidence                                                                                                    | Lost                       | When                                                                                                                                                                                 |
| --- | ----------------------------------------------------------------------------------------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A1  | run envelope: workflow, event, actor, triggering actor, attempt, created/started/updated, conclusion, issue | never (until runs deleted) | ✔ every run (no-op sweep ticks and skipped runs as daily counts)                                                                                                                     |
| A2  | jobs: runner name, start/end, every step's name and conclusion, the first failed step                       | never                      | ✔ every command run; sweeps when they failed or acted                                                                                                                                |
| A3  | full job log, redacted, gzipped (~32 KB), and the failed step's last 200 lines                              | 90 days                    | ✔ **(amended)** every command run and every run that is not a no-op success, not only incidents and creates: the log is the safety net for what the facts did not name (decision 20) |
| A4  | the facts artifact                                                                                          | 7 days (encrypted)         | ✔ every run that has one                                                                                                                                                             |

**B. What the job knows** (facts; lost when the job ends)

| #   | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | When                                          |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| B1  | `command` as typed, `issue`, `instance`, `flavor`, `storage_mode`, `workflow_version`, and `dispatch_reason`. **(amended)** The reason is an open vocabulary, not the five values the first draft listed: the sweep found eleven in-workflow dispatch sites (SHUTOFF shelve, uptime timeout, workshop due, unreachable, issue-close shelve and delete, expiry delete, setup-stall shelve, retry, repair, renewal email), and `control-instance-from-workflow` has no reason input today. Until audit class 2 adds one, every dispatching workflow records the dispatch it made in its own facts (B11: `dispatch workflow=… issue=… reason=…`) and the harvester joins the child run to it (same workflow, same issue, created within 60 s), the same join it does for runner-side `DISPATCH` lines. A self-hosted sweep that dispatches for many issues in one run (`workshop-backfill`, `close-expired-issues`) records one dispatch fact per child and is joined the same way. The one dispatcher that runs on `ubuntu-latest` and uploads no facts (`create-workshop`) is joined by its run instead: the parent run in progress on the same issue when the child was created, which its `run-name` carries | ✔ every command                               |
| B2  | Nova `status` and `task_state` before and after; `failure_mode`; the failure class `notify-admins` uses; the failed Nova step (`schedule_instances` / `compute_unshelve_instance`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | ✔ every command                               |
| B3  | **`hostId`**, Nova's per-project hash of the compute host. Today read only inside the failure branch of `create-instance` and `control-instance` and thrown away. With it on successes too, failure rate per host becomes a query (the sick-host pattern behind the g3 unshelve failures). Nova hashes the host together with the project, so ids compare only within the production allocation. **(amended)** From the same `server show`: the **server id** (the create prints no id today), the **image id and name** (the image is chosen by name through `IMAGE_VGPU`/`IMAGE_REGULAR`, and the golden-image automation will re-point names, so the id is the only stable key for "which image failed"), and in volume mode the **volume id, its status before attach and whether the `reserved` self-heal ran** (today a `::warning` only); in share mode the **share id** from the lookup                                                                                                                                                                                                                                                                                                               | ✔ every create and unshelve, success included |
| B4  | address outcome: taken, attached, released, shelved again (decision 25). **(amended)** The address work is done by `fip.py` (openstacksdk, called from create, control, setup, retrieve-metadata and extend-session) and today its result lives only in the job log: the job records its exit code and output as a fact at every call, so a mint, a non-routable delete, a lost CAS race and the `verify` identity check (exit 4: the address answers as another server; exit 5: could not ask) each have a row. None of these reach `fip-police.log`, which sees only the policing script's own actions                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | ✔ every create and unshelve                   |
| B5  | stage timestamps: requested, BUILD done, ACTIVE, address bound, SSH up, setup complete, share mounted, reboot verified, email sent                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | ✔ every create, unshelve, resume              |
| B6  | capacity for the flavor at the time, read afterwards by the harvester from Jetstream2's Grafana vacancy history for every outcome (no call in the job). **(amended)** The in-job `morphocloud.org/api/availability` read runs at one site only, the no-capacity unshelve; it is kept as a fact where it runs but is not the source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | ✔ every create and unshelve                   |
| B7  | **every** label on the issue at the time, reconstructed by the harvester from the issue's timeline events (no call in the job). **(amended)** Not a listed subset: the first draft named six (`followup:*`, `followup-count:*`, `needs-admin`, `invariant-violation`, `admin-email-failed`, `status:*`) and the sweep found `error:system-capacity`, `error:not-authorized`, `needs-fix`, `volume:*`, `renewal-notice:*`, `email-sent`, `workflow:*`, `storage:migrated` and `request-source:portal` besides                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | ✔ every command                               |
| B8  | refusal reason (not approved, does not exist, in flight, maintenance, unauthorized): the authorize job prints one line `REFUSED reason=<reason>` before the deliberate `exit 1`, and the harvester reads it from the job log (no facts upload, cost rule 1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | ✔ every refusal                               |
| B9  | `collect-instance-diagnostics`: Nova fault and recent actions with each step's result and host. **(amended)** It runs at three sites only (the create failure and two control-instance failure paths). Reconcile's ERROR violations, follow-through's hand-overs, setup and resume-setup failures collect nothing in the job; for those the harvester's late snapshot (F6, within 5 minutes, now with the full `server show` and the last five `event show`s) is the record, and a server deleted inside that window is the stated residual                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | ✔ when the workflow runs it; else F6          |
| B10 | the admin email: subject, class, body, recorded by `record-facts` at each sender where it sends. **(amended)** The senders are found by the CI check `scripts/check-admin-email-facts.py` (every `action-send-mail` step addressed to the admin list has a facts step beside it), not listed by hand. Today that is `notify-admins` (called from control-instance once (delete stalled; its failure email was removed 2026-11-16, #426), create-instance, resume-setup) and the direct senders `admin-alert.yml`, `follow-through`, `reconcile-instances` (two emails: violations and catch-up), `expire-shares`, `guard-issue-close`, `detect-dormant-workflows`, `on-admin-mention`, `request-notify-admin`, `request-notify-admin-workshop`. `delete-instance-and-volume` is not a sender; it goes through `notify-admins`. Two failure paths send no admin email at all (`create-instance-from-workflow`, `create-course-instance`): they are still incidents, because an incident is a run's conclusion, never an email                                                                                                                                                                                  | ✔ every admin email                           |
| B11 | sweeps: what each tick did (`acted=[{item, issue, action, reason}]`); reconcile's violations and the dormant detector's findings, one item each; follow-through's attempts; **(amended)** every `gh workflow run` the tick made (`dispatch workflow=… issue=… reason=…`, the parent side of B1's join)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | ✔ every tick that acted or failed             |
| B12 | runner host at job time: free disk, load, uptime (`df`, `/proc/loadavg`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | ✔ every command on a self-hosted runner       |
| B13 | **(amended)** the in-job Jetstream2 status check's real result: `operational`, `outage` or `unreachable`. Today `check-js2-status` reports an unreachable ACCESS API as `outage_detected=false`, the same as operational; the action gains a three-valued output beside the boolean and the fact records it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | ✔ every create and unshelve                   |
| B14 | **(amended)** at create, the project instance count and the cap it is checked against (`TOTAL_COUNT`, `MAX_TOTAL=90`), today printed and discarded; the `error:system-capacity` label marks the hit                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | ✔ every create                                |

**C. The instance's setup log** (lost when the server is shelved, deleted or
recreated, so it is taken during the run, never later)

| #   | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | When                                              |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| C1  | **the full console log** (cloud-init and ansible output) that `setup-instance` already reads every poll as `$log_output` and today discards after extracting one line. Nova returns only the tail, so each poll's read is merged into a growing copy. Each merge records whether it found an overlap with the copy; where it did not, a `[gap]` marker goes into the copy and `console_complete: false` into the facts, so a gap never looks like a complete log. **(amended)** The boothook's egress probe (`cloud-config` lines 48–60) leaves no console line by design, so a boot-time network failure that later recovers is erased from the instance; it gains one line, `MC_EGRESS_PROBE ok=<true\|false>`, chosen not to match the `{"status":` parser | ✔ every create and resume-setup, success included |
| C2  | the console at follow-through's hand-over (resume, error, or the stall/ceiling shelve of decision 33): follow-through reads it every tick and discards it today                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | ✔ every hand-over                                 |
| C3  | `/var/log/cloud-init-output.log` and `/var/log/cloud-init.log` from the instance, over a new SSH connection opened only after `fip.py verify` passes (**amended:** there is no session to reuse, see cost rule 3)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | ✔ setup failure, when the address verifies        |
| C4  | `journalctl -b` for the cloud-init units, last 2,000 lines                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | ✔ same                                            |
| C5  | derived: status, reason, last ansible task, failed item (computed today), time to first console line, time to `complete`, tasks seen, `failed_extensions` from the completion line (**amended:** today a comment only) → signature `setup.error:<task>`, `setup.stalled:<task>`, `setup.timeout:<task>`                                                                                                                                                                                                                                                                                                                                                                                                                                                       | ✔ every create                                    |

**D. An unreachable or address-less instance** (lost at the shelve that fixes
it)

| #   | Evidence                                                                                                                                                                                                                                                                                          | When                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | console log, `port show` for the server's port (status, fixed IP), the floating IP's status, `server event list`, the last SSH error text. Today auto-shelve (decisions 23 and 26) reads the port and the address, fails SSH, and shelves: the network-orphan evidence is destroyed by the repair | ✔ in `control-instance` when `dispatch_reason` is `unreachable`, before it shelves; in `automatic-instance-shelving` before it dispatches |
| D2  | the same for an unshelve or create that ends ACTIVE without an address or unreachable (decision 25's shelve-again path, inside `control-instance`) and before decision 34's repair (inside `reconcile-instances`, before it dispatches)                                                           | ✔ before the shelve-again or the repair dispatch                                                                                          |

**E. Outside context at the moment** (read afterwards by the harvester; these
sources keep their history)

| #   | Evidence                                                                                                                                                                                                                                                                                                             | When                                                    |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| E1  | Jetstream2 availability for the flavor                                                                                                                                                                                                                                                                               | ✔ (B6)                                                  |
| E2  | Jetstream2 outages: the ACCESS outages feed `check-js2-status` reads, including its past outages, read by the harvester. How far back the past listing reaches is to be confirmed; outside its range the record says `unknown`, never `operational`. The in-job check's own three-valued result is B13 **(amended)** | ✔ every incident on an OpenStack step                   |
| E3  | GitHub incidents from githubstatus.com, read by the harvester; they tell a GitHub-wide outage from our bug. The public feed lists only recent incidents, so backfilled incidents outside its range get `unknown`                                                                                                     | ✔ every incident whose error line is a GitHub API error |
| E4  | Jetstream2 Grafana vacancy history                                                                                                                                                                                                                                                                                   | ✔ the source of B6 on successes                         |

**F. The runner host** (the harvester runs there and reads these in place, from
its last offset; **amended:** every line is shipped, unfiltered, decision 20)

| #   | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                       | When             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------- |
| F1  | `~/morphocloud-dispatch.log`: **a crontab dispatch that failed never becomes a run**, so this is the only record that a sweep did not fire. **(amended)** Today the line has no repository (Analytics and Instances dispatches share the log) and `gh`'s error goes to `/dev/null`, so a 5xx and a bad token look the same; the uniform `DISPATCH` line adds `repo=` and, on failure, the last 200 characters of `gh`'s stderr | ✔ every line     |
| F2  | `~/fip-police.log`: mints, releases (with issue), droughts, non-routable addresses, and **(amended)** the per-tick pool-state line (`held/running/starting/spares/…`, 288 a day), the only time series of the address pool                                                                                                                                                                                                     | ✔ every line     |
| F3  | the portal runner's `pickup.log` (Test now, production after the migration): uncaught SSH timeouts, lost claims, failed restores (audit class 8)                                                                                                                                                                                                                                                                               | ✔ every line     |
| F4  | `~/token-expiry.log`                                                                                                                                                                                                                                                                                                                                                                                                           | ✔ every line     |
| F5  | the Actions runner service's `_diag` logs (runner crashes, lost connections), on runner-1 in place and on production's second runner over SSH                                                                                                                                                                                                                                                                                  | ✔ error lines    |
| F6  | the late OpenStack snapshot of an incident's instance (full `server show` JSON including `fault`, `event list` and the last five `event show`s, port, floating IP) and **(amended)** the project's `limits show --absolute` (no workflow reads quotas today, and a quota hit looks like capacity; MC-PCOM's volume-count ceiling was found by hand), stamped with its age                                                      | ✔ every incident |

**G. The portal: what the member experienced (amended 2026-11-08).** The first
draft listed nine tables to export over the SSH gate. The sweep of the portal's
`share-prototype` (abbbd74) showed that export would have lost most of it:
`chatbot_escalations` rows are deleted the moment the email sends (seconds, not
the 7 days the draft assumed); `alerts` is a throttle stamp and `alert_outbox`
and `alert_digest` hold only unsent mail, so a delivered alert leaves no row;
`failures` is pruned at 30 days; `instance_commands.outcome` is set only when
the member's own card loads; `instance_requests.state` is overwritten in place;
and six tables were not listed at all (`shares`, `resets`, `instance_sessions`,
`reconcile_runs`, `instance_checks`, `instance_recreates`). So the portal gains
**one append-only table, `portal_events`** (`id`, `ts`, `kind`, `issue`,
`github_id`, `login`, `payload` JSON), written in the same transaction as each
event below, and **one read-only gate command, `export-events --since <id>`**,
that reads only that table (a gate `case` arm, a sudoers line, an `admin/cli.py`
subcommand, and `provision.sh` re-run on Test and production). The working
tables stay as they are. Rows are pruned after 400 days. The harvester keeps one
cursor. Test now, production after the 2026-11-23 migration.

**Amended 2026-11-25 (built):** the portal's existing `audit` table already is
an append-only log, so it is the event log; there is no `portal_events` table.
`export-events` reads it by row number. See INCIDENT_STORE_PHASE1_DESIGN.md, "PR
C — the portal".

| #   | Event kind (`portal_events.kind`)                                                                                                                                                                                                                                                    | Replaces                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| G1  | `command.posted`, `command.outcome` (when the portal sees the 👍/👎; the harvester joins the GitHub side by `comment_id` and never reads a missing outcome as pending). The member-side denominator, including presses that never produced a run                                     | `instance_commands`                                                                        |
| G2  | `request.<state>`, one row per transition, so stuck-then-failed is visible                                                                                                                                                                                                           | `instance_requests`                                                                        |
| G3  | `failure` (kind, detail)                                                                                                                                                                                                                                                             | `failures` (pruned at 30 days)                                                             |
| G4  | `autoshelve.requested`, `autoshelve.dispatched`, `autoshelve.failed`                                                                                                                                                                                                                 | `auto_shelves`                                                                             |
| G5  | `reachability` (issue, name, from, to), written inside `replace_instance_states` from the rows it is about to delete; **reachability history is kept nowhere today**. Flavor and hostId are joined by the harvester from the outcomes, so the runner's state payload does not change | the planned `reachability_events`                                                          |
| G6  | `alert.sent`, `alert.outbox`, `alert.digest` at every `deliver` call, with subject and body                                                                                                                                                                                          | `alerts`, `alert_outbox`, `alert_digest`                                                   |
| G7  | `help.requested` at queue time, with subject and body (redacted by the harvester)                                                                                                                                                                                                    | `chatbot_escalations` (deleted on send)                                                    |
| G8  | `audit` (action, detail)                                                                                                                                                                                                                                                             | `audit`                                                                                    |
| G9  | `share.<state>`, `reset.<state>`, `recreate.<state>`, `check.<state>`, `session.started`: the storage and card events the first draft did not list                                                                                                                                   | `shares`, `resets`, `instance_recreates`, `instance_checks`, `instance_sessions`           |
| G10 | `error` at every `log.exception` site in the web app and the worker (eight today), with the exception text                                                                                                                                                                           | the systemd journal, which the first draft did not list                                    |
| G11 | `connections` (passphrase, web link, address) and `reconcile_runs`                                                                                                                                                                                                                   | ✘ never read: secrets, and a one-row timestamp the harvester already has from `pickup.log` |

**H. The issue, afterwards** (harvester, for 24 h after an incident)

| #   | Evidence                                                                                                                                    | When             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| H1  | the timeline: comments (text, redacted), labels added and removed, reactions. A member's "still can't connect" is evidence                  | ✔ every incident |
| H2  | what happened next: the member's next command and its result, any admin-dispatched run (the maintainer's hand action), any automatic action | ✔ every incident |

**Blind spots, stated:**

- A command GitHub never queued (an outage). The portal's `command.posted` event
  (G1) records the press, so it shows on production after the migration.
- An instance that is reachable and set up but broken for the member (desktop,
  Slicer, the share). It shows only as a help request (G7, `on-admin-mention`).
- Course repositories (`MC-*`): no harvester until they have a runner clock.
- Runs older than 90 days: envelope only, no log. Backfilled runs never have
  facts (B, C, D).
- A rerun of a run created more than 48 hours earlier that starts and finishes
  inside one five-minute cycle (see "The harvester", step 1). **(amended)** The
  first draft closed this with a one-minute `--watch` pass; it was dropped as a
  second cron line for a case that is almost always the maintainer's own hand
  rerun.
- **(amended)** A server deleted within five minutes of an incident that
  collected no in-job diagnostics (B9): its Nova fault and events are gone
  before the late snapshot (F6).
- **(amended)** Already kept elsewhere, not copied:
  `collect-instance-uptime.yml` pushes every instance's uptime to the
  `instance-uptime-data` branch of the source repository every 15 minutes; the
  store links to it.

### The harvester

`scripts/harvest-incidents.py`, on production's acquire runner, where the
dispatcher lives. It has one crontab line: the full cycle every 5 minutes
(**amended:** the one-minute `--watch` pass of the first draft is dropped, see
step 1).

- **Before the merge** they are added by hand (see "Production only, and before
  the merge").
- **From the merge** they are runner-side scripts in
  `scripts/check-workflow-clock.py`'s `RUNNER_SCRIPTS`, next to the floating-IP
  policing and not in the `.extra` file, templated per target. **(amended)**
  `TARGETS` gains a store field (`MorphoCloud/Incidents` for production,
  `MorphoCloud/Incidents-sandbox` for Test during Phase 1, `None` afterwards,
  which omits the line), and `restore-instances-dispatcher.sh` drops
  `harvest-incidents` lines found outside the managed block the way it drops
  stray dispatcher lines, so the hand-added production line cannot double-fire
  beside the rendered one.

The Test runner gets the same line writing to `Incidents-sandbox`, but only
while Phase 1 is tested.

Credentials:

- **GitHub:** a fine-grained PAT. On the source repository: Actions read (runs,
  jobs, logs, artifacts) and Issues read (labels, timeline, comments). On the
  store: Contents and Issues write. It holds no Actions write, so it cannot
  start a workflow, and it sends no alerts itself:
  - token expiry is watched by `check-dispatch-tokens.sh`, which gains this
    token in its list and already alerts through Analytics'
    `token-expiry-alert.yml`;
  - a stopped or failing harvester is caught by the store watchdog.
- **OpenStack:** see decision 18 (a restricted application credential, or the
  runner's credential behind a read-only wrapper).
- **The facts private key** (to decrypt artifacts) and the email HMAC key, in
  the harvester user's home, mode 600. Needed from the merge, when facts begin.
  Losing the private key makes up to 7 days of facts unreadable. Changing the
  HMAC key breaks the correlation of a member across incidents.

**Rebuild inventory.** The harvester PAT, the facts private key, the HMAC key
and the restricted OpenStack credential (decision 18, if it exists) are added to
`INSTANCES_RUNNER_REBUILD.md`'s file table, with offline backups in `~/.ssh` as
for the dispatcher token values. From the merge,
`restore-instances-dispatcher.sh` installs them (`MWF_HARVEST_TOKEN`,
`MWF_FACTS_KEY`, `MWF_HMAC_KEY`, with defaults under `~/.ssh`). **A missing
harvester key never blocks the restore.** The script restores the clock that
drives automatic shelving, which matters most in an emergency rebuild. Without a
key it installs everything else, leaves the harvester lines out, and prints a
warning. The store watchdog then reports the harvester as stopped. The facts
public certificate and the HMAC key are also set in each source repository
(repository variable and Actions secret) by the same procedure, so the job and
the harvester can never hold different keys.

Each cycle:

1. **Find runs.** The runs API filters by creation time, not completion time,
   and a rerun keeps its original creation time. So creation time alone would
   miss runs that queued or ran long, and every rerun of an older run. Four
   mechanisms cover it, all inside the five-minute cycle (**amended:** the first
   draft's separate one-minute `--watch` pass is gone):
   - **New runs:** every cycle lists the runs created since the last cycle, with
     a 15-minute overlap, in narrow windows (the API returns at most 1,000 runs
     per filtered query, and Instances produces about 1,100 a day).
   - **In-flight runs:** every cycle also lists the runs that are `queued` or
     `in_progress` with no creation filter (two calls), so a rerun of a run of
     any age that is still running at a cycle boundary joins the watch list.
   - **Lookback window:** once an hour, the runs created in the last 48 hours
     are re-listed and each run's `status`, `run_attempt` and `updated_at`
     compared with what is recorded. Hourly rather than every cycle: it only
     catches very fast reruns of recent runs, and every 5 minutes it would pull
     about 2,200 run records each time.
   - **Watch list:** every run seen queued or in progress is kept and checked
     every cycle until it completes, whatever its age. A new `run_attempt` on a
     recorded run is a new outcome line, and a new incident when it fails.

   The rerun playbook planned for the agent records each run it reruns. The
   residual blind spot (a rerun of a run older than 48 hours that starts and
   finishes inside one cycle) is stated under "Evidence inventory".

2. **Jobs, facts, logs.** For each command, alert, failed or acting run: fetch
   the jobs, the `mc-facts-*` artifacts and the job log (A3; **amended:** every
   such run, not only incidents and creates).
3. **Classify** (deterministic): `result` is `skipped`, `dropped`, `cancelled`,
   `refused` (facts, or a failed step on the refusal list), `failure`,
   `success-with-incident` (an admin email or a violation in the facts) or
   `success`. The signature comes from the facts first, else from the failed
   step's error line against `incident-signatures.yml` (a GitHub 5xx only on the
   error line, never by grepping the whole log), else `unclassified`.

   **`actor_kind`** is `member`, `admin`, `automation` or `test`. It is not read
   from the run's actor alone. The runner host's dispatches (the crontab
   dispatcher, the portal's auto-shelve, the address policing's alerts) use a
   fine-grained PAT owned by a person, so the run's actor is that person: on
   production every sweep tick shows `muratmaga`. Counted as admin, every tick
   would flood the implicit labels with false hand actions. So, in order:
   1. A `workflow_dispatch` run that matches a dispatch line in a runner-side
      log (`~/morphocloud-dispatch.log`, the portal runner's `pickup.log`,
      `~/fip-police.log`: same workflow, same issue when there is one, created
      within 60 seconds of the line) is `automation`, **whoever the actor is**.
      From the merge, the dispatch reason (audit class 2) confirms it. This
      needs every runner-side dispatch to log a line **at the moment it
      dispatches**. Today only the crontab dispatcher does:
      - the policing script logs nothing when a member note is dispatched
        successfully;
      - it logs an alert only after the alert's run has finished, which can be
        well past 60 seconds;
      - `pickup` logs its auto-shelve dispatch at once, but without the
        workflow's name.

      Phase 1 therefore makes every `gh workflow run` call site on the runner
      write one uniform line as soon as the call returns:
      `<UTC time> DISPATCH <workflow> repo=<repo> issue=<n or -> by=<script> [FAILED <stderr tail>]`.
      That covers `morphocloud-dispatch.sh` (its existing line reformatted,
      keeping the words `dispatched` and `FAILED` the rebuild notes grep for),
      the policing script (notes and alerts), `check-dispatch-tokens.sh`
      (**amended:** a fourth site the first draft missed) and the portal runner
      (the shelve). In-workflow dispatch sites record the same fields as a fact
      instead (B11). The CI check `scripts/check-dispatch-logging.py` in each
      repository fails when a `gh workflow run` call site has neither, so a new
      dispatcher cannot reintroduce the gap.

   2. A run by the workflow app (`morphocloud-workflow-app[bot]`) or
      `github-actions[bot]` is `automation`.
   3. A run whose actor or triggering actor is a test account (amm554, listed in
      `incident-sources.yml`) is `test`.
   4. A run by an admin-team login that matched no runner-side dispatch (a
      command typed on an issue, a dispatch from the UI or the CLI) is `admin`,
      **unless that login opened the issue**: an admin working their own
      instance is a member doing a member's thing, and is `member`. Otherwise a
      maintainer's retry on their own instance would read as a hand fix and
      enter the implicit labels.
   5. Anything else is `member`.

4. **Incidents** (every `failure`, `cancelled`, `dropped`,
   `success-with-incident`, and every alert run):
   - fetch the log;
   - redact;
   - take the late snapshot (F6) and read the outside context (E1–E4);
   - **when the incident sent an admin email** (B10), open the issue
     `incident:<id>` in the store, labeled `needs-verdict`. That is about 30–35
     a month, about 8 a week. Other incidents are recorded without an issue.
     Their verdicts come from a standing verdict per signature, which the weekly
     numbers email prompts for: it lists each signature with no admin email and
     no standing verdict, with its count.
5. **Runner logs** (F1–F5, every line since the last offset) and **portal
   events** (`export-events --since <id>`, G1–G10, one cursor).
6. **Follow-ups**: for open incidents younger than 24 h, append the issue's new
   timeline and what happened next (H).
7. **Write**: one commit and one push per cycle.

**Backfill on day one:** the API still holds 90 days of runs and logs. That
gives envelopes, logs and classification (A1–A3) for about 375 production
incidents and 900 command runs before the baseline formally starts.

### Records

```
outcomes/YYYY/MM/DD/<repo>.jsonl   one line per command run, and per sweep run
                                   that acted or failed
rollups/YYYY/MM/DD/<repo>.json     counts per workflow × result for no-op sweep
                                   ticks and skipped runs; failed dispatches (F1)
incidents/YYYY/MM/<id>/
  incident.json                    envelope + facts + result + signature
  narrative.md                     the admin email body, or a generated summary
  diagnostics.md                   B9
  setup/console.log.gz             C1 / C2
  setup/cloud-init-output.log.gz   C3, C4
  network/                         D1, D2
  job.log.gz, failed-step.log      A3
  late-snapshot.json               F6
  context.json                     E1–E3
  timeline.json                    H1, H2
setup/YYYY/MM/<run_id>.json.gz     C1 + C5 for successful creates (the baseline
                                   of a normal setup)
portal/YYYY/MM/DD/events.jsonl     G1–G10, one portal_events row per line
runner/YYYY/MM/DD/<host>/<log>.gz  F1–F5, every line since the last offset
logs/YYYY/MM/DD/<run_id>-<attempt>.log.gz
                                   A3 for command runs that are not incidents
                                   (incidents keep theirs in their directory)
state/cursors.json
```

**Incident id:** `<repo short>-<run_id>-<attempt>-<item>`. `item` is `0` for a
run-level incident. A run that finds several things (a reconcile tick flagging
three instances, a dormant-workflow report with several findings) numbers them
in its facts (B11), and each becomes its own incident with at most one instance.
Portal and runner events use `<source>-<row id>`. The job knows its own id, but
the store issue does not exist when the email is sent. So the email links to a
search, which finds the issue once the harvester has opened it (within 5
minutes):

- The id is **quoted**, and the search is limited to issues: GitHub search
  splits words on hyphens, and an unquoted id with parts like `1` and `0` would
  match unrelated issues. For example,
  `https://github.com/MorphoCloud/Incidents/issues?q=is%3Aissue+in%3Atitle+%22instances-12345678901-1-0%22`.
- An email that reports **one** item links to that item's id.
- An email that covers **several** items (one reconcile email for all its
  violations) links to the run-level prefix, `"instances-12345678901-1"`, which
  finds every item's issue from that run and attempt.

Emails the portal sends itself (G6) carry no link in Phase 1. Their incidents
still get a verdict issue and appear in the `needs-verdict` list.

`incident.json`, with what the collector fills deterministically and nothing
that needs judgment:

```json
{
  "schema_version": 1,
  "id": "instances-12345678901-1-0",
  "source": {
    "repo": "MorphoCloud/Instances",
    "workflow_file": "control-instance.yml",
    "run_id": 12345678901,
    "run_attempt": 1,
    "runner": "instances-runner-1",
    "workflow_version": "c9711a2",
    "dispatch_reason": "member",
    "actor_kind": "member | admin | automation | test",
    "queued_seconds": 41,
    "duration_seconds": 388,
    "failed_step": "Poll instance until ACTIVE",
    "facts": "present | missing"
  },
  "command": "/unshelve",
  "issue_number": 214,
  "instance": {
    "name": "…_instance-214",
    "flavor": "g3.xl",
    "host_id": "9c1f…",
    "before": { "status": "SHELVED_OFFLOADED", "task_state": null },
    "after": { "status": "SHELVED_OFFLOADED", "task_state": null },
    "nova_failed_step": "compute_unshelve_instance",
    "address": "none"
  },
  "setup": null,
  "result": "failure",
  "failure_class": "host_failure",
  "failure_mode": "host_error",
  "signature": "unshelve.host_failure",
  "markers": ["followup:unshelve", "followup-count:1"],
  "owner_rule": "follow-through",
  "context": {
    "js2_available": 0,
    "js2_status": "operational",
    "github_status": null
  },
  "admin_email": { "sent": true, "subject": "…" },
  "redaction": { "rules_version": 1, "replacements": 2 }
}
```

- `owner_rule` is the existing mechanism that owns the case, read from the
  markers: `followup:*` belongs to follow-through, `invariant-violation` with a
  repair pending to reconcile (decision 34), `followup:setup` to the setup watch
  (decision 33).
- `workflow_version` comes from `.github/morphocloud-workflow-version`, a
  one-line file vendorize gains. `unknown` when absent.

An outcome line carries the same `source`, `command`, `issue_number`,
`instance.flavor`, `instance.host_id`, stage timestamps and `result`, with no
files.

### When collection fails

- **The facts upload fails:** the envelope still records the run, marked
  `facts: missing`, and the weekly numbers count them.
- **The harvester stops, fails or falls behind** (a dead cron line, a down host,
  an expired token): nothing is lost while the API holds the runs (artifacts 7
  days, logs 90). The harvester writes `state/harvester.json` every cycle: last
  cycle, the newest run it has seen (its cursor), last error. The **store
  watchdog**, `watchdog.yml` in `MorphoCloud/Incidents`, checks the **cursor**,
  not the newest outcome line. Outcome lines come from about 10 command runs a
  day, and a quiet night has none, which would alarm falsely. The cursor moves
  with the sweeps every 5 minutes, so it is always recent while the harvester is
  healthy. The watchdog emails the admins once per episode when the cursor is
  more than two hours old or the last cycle ended in an error.
  - It runs on GitHub, not on the runner, so it still fires when the runner host
    is down.
  - It runs on `on: schedule` **every 6 hours**, with `workflow_dispatch`. This
    is a deliberate exception to decision 21: it must not depend on the runner.
    Given GitHub's throttling (3–5 deliveries a day for any cron), every 6 hours
    detects about as fast as hourly would, and keeps the cost fixed.
  - It costs at most about 120 billed minutes a month: 4 runs a day, each
    rounded up to a minute, in a private repository on `ubuntu-latest`. An
    hourly schedule could reach about 720 if GitHub delivered every run.
  - A stopped harvester is reported at the watchdog's next delivery after the
    two hours have passed: within about 6 hours, well inside the 7-day artifact
    retention.
  - The mail credentials are the store's own secrets.
  - The only silent case left is GitHub's scheduler and the runner failing
    together.
- **The portal export fails** (from the migration): its cursor stays put.
  `portal_events` keeps its rows for 400 days, so nothing is lost; the watchdog
  also checks the newest portal record, at one day.

### Storage usage (daily snapshot, weekly report)

Asked for by the maintainer on 2026-11-24: is 100 GB per member (decision 1 of
STORAGE_REDESIGN_PLAN.md) too large or too small? The answer comes from one more
source of this harvester, not a separate pipeline. The portal serves a small
read-only report. The harvester owns the schedule, the storage, the retention
and the analysis. Rows are keyed by GitHub id. No login, name or email is
stored. Approved by the maintainer on 2026-11-24, with the review of #455 folded
in.

**What the portal serves.** One read-only command through the runner's existing
forced-command gate (`mcrunner`, `deploy/mc-data-runner-gate`): `usage`. The
gate passes it only with no arguments, and the command only reads the `shares`
table. It prints one JSON document,
`{"schema": 1, "at": <time>, "shares": [...]}`, with a row for each share in
state `ready`, `unavailable` or `read_only` (the states that hold data):

| Field        | From the portal's `shares` table                                 |
| ------------ | ---------------------------------------------------------------- |
| `github_id`  | the key                                                          |
| `used_bytes` | measured by the worker (CephFS `rbytes`); null if never measured |
| `size_bytes` | the share's size (`max_bytes`); null if never measured           |
| `usage_at`   | when the worker last measured it; null if never                  |
| `state`      | `ready`, `unavailable` or `read_only`                            |
| `start`      | the day the member asked for the storage (`requested_at`, UTC)   |
| `end_date`   | the storage term's end date                                      |
| `extensions` | approvals applied so far                                         |

`start` is the day the member asked for the storage. The share is created within
a minute of that, so it is also the day the 120-day term starts
(STORAGE_REDESIGN_PLAN.md says "from the day it was created": the same day).

The worker measures every mounted share (`ready` and `read_only`) on its health
pass, once a minute. An `unavailable` share is not mounted, so its figures are
from before it went down.

**What the harvester does.** A new step, `storage_usage`, in the cycle after
`platform`, for a source that sets `storage_usage: true` in
`incident-sources.yml` (production only):

- **When:** **(amended 2026-11-27, maintainer's decision for the admin
  dashboard; was weekly)** the first cycle after 13:00 UTC of each UTC day. The
  harvester's 5-minute cycle is the clock, so there is no new cron line.
  `state/storage-usage.json` records the last day taken (`day`, `YYYY-MM-DD`). A
  state file from the weekly build (`week` only) counts as "no day taken".
- **How:** SSH to the target named in `~/mc-data/portal_target` (the file the
  workflows already read), with the runner's key and `BatchMode`,
  `StrictHostKeyChecking=yes`. No new key or token.
- **Validation:** the reply must be one JSON document with `schema` 1 and a
  `shares` list. Every row needs a unique integer (not boolean) `github_id`, a
  known `state`, byte counts and `extensions` that are null or not negative, a
  real `start` date, and an `end_date` and `usage_at` that are null or well
  formed. An empty list after a day whose snapshot had shares is refused too,
  because it would record every share as gone. A large drop from one day to the
  next is accepted, since shares created on the same day end on the same day
  (the 44 shares of the 2026-11-23 migration all end on 2027-03-23). A failed
  SSH call, a timeout, or truncated or non-JSON output is a failed attempt,
  never an empty snapshot. The day stays untaken and the next cycle tries again.
  The error in the store names the failure only. SSH's own message, which
  carries the portal's address, goes to the harvester's local log.
- **Stored:** `storage/YYYY/MM/DD.jsonl` (the snapshot's UTC day; the same
  layout as the weekly build, which already wrote one file per snapshot day).
  There is one line per share: `schema`, `day` (`YYYY-MM-DD`), `week` (the ISO
  week of that day, `2026-W48`), `taken_at`, plus the fields above. A row whose
  `usage_at` is null or more than 24 hours old gets `stale: true`. Files from
  the weekly build have no `day`; their file name gives it.
- **Final use:** a share in one stored snapshot that is missing from the next
  stored snapshot, and is not in the latest one, has left the states that hold
  data (a reset empties a share but keeps it, so it stays). A share that comes
  back was not gone and gets no final row. Each share has one final row, at its
  last departure. That row is its last stored row with `gone_by` (the next
  snapshot's time), so it is up to a day older than the departure. It goes in
  `storage/final.jsonl`. This keeps the use of shares that left, which the
  latest file alone would not show. Snapshots are compared in `taken_at` order,
  daily and weekly files alike.
- **Order:** the snapshot goes into the store in one write, whole or not at all.
  `final.jsonl` and the report are then rewritten from all the snapshots, so
  they are fully derived, and `state/storage-usage.json` comes last. All of it
  goes out in that cycle's one store commit; if the push fails, the next cycle's
  commit carries it. If a snapshot for the day is already in the store (its
  state write was lost), the step does not call the portal again. It rewrites
  `final.jsonl`, the report and the state from the snapshots. A crash between
  writes can therefore neither lose a final row nor repeat one. A day taken with
  a bad snapshot (a bug) is taken again by hand: delete that day's file
  (`storage/YYYY/MM/DD.jsonl`) and `state/storage-usage.json` from the store.
  The step checks the state first, then the store, so both must go. The next
  cycle then takes the day afresh.
- **Which snapshot the weekly report uses:** the report below stays weekly. For
  each ISO week it uses the snapshot with the latest `taken_at` among that
  week's files (Sunday's, once the week is complete; for the current week, the
  latest day so far). Final use (item 3 of the report) uses `final.jsonl`, which
  is built from every daily snapshot.
- **Errors:** as for every step. The error goes into `last_error`, the watchdog
  emails the admins, and the other steps go on.
- **Size and retention:** about 90 rows (the 89 member shares planned in
  STORAGE_REDESIGN_PLAN.md) × 250 bytes × 365 days, about 8 MB a year. It is
  kept with the rest of the store, with no pruning.
- **Test-Instances:** no step (`storage_usage` is not set), because its portal
  is shelved. See "Testing" below.

**What it answers.** The same step rewrites `storage/report.md` from every
snapshot, so the report is fully derived from the `.jsonl` files and the
thresholds can change later with the report regenerated. It uses Python's
standard library only. Rows are counted as follows:

- **Measured:** `used_bytes` and `size_bytes` both known, the size above zero,
  and not stale. Only measured rows enter the median, the percentiles and the
  threshold counts. Stale and unmeasured rows are counted on their own line,
  never as "less than 5 GB". The worker's health pass runs every 60 seconds
  (`HEALTH_EVERY` in the portal's `admin/worker.py`) and measures every mounted
  share, so 24 hours without a measurement means the share was not mounted.
- **New:** a share less than 7 days from its `start` is left out of the "less
  than 5 GB" and "less than 10 GB" counts (a new member has not uploaded yet),
  and counted on its own line.
- **Read-only:** a share in its 15 read-only days is in the weekly figures and
  counted on its own line.

1. **Each week:**
   - the number of shares, and how many are measured, stale, new and read-only;
   - the median and 90th percentile of the GB used;
   - how many shares use more than 80% of their size, and how many 95% or more
     (at the cap: use alone cannot show how much more those members wanted);
   - how many use less than 5 GB, and how many less than 10 GB;
   - the total used and the total size, each against the allocation's 10,000 GB.
     The total size is what counts against the quota.
2. **By age:** each share's use by days since `start`, in 0–30, 31–60, 61–90,
   91–120 and more than 120 days, with the same median, 90th percentile and
   counts for each. Each share is counted once per bin (its latest row in it). A
   share is extended or not by its latest row. One whose latest row has an
   extension (`extensions` > 0) is only in the separate Extended age table, with
   all its rows, including its use before the extension, in the same bins. The
   No-extension age table therefore describes shares never extended so far, and
   a share extended later leaves its early bins. This shows whether use keeps
   growing over a term, or levels off well below 100 GB.
3. **Final use:** the same figures for the final row of every share in
   `final.jsonl`: what a share held when it left the data-holding states. A
   final row is measured if both byte counts are known and the size is above
   zero, whether or not it is stale, because a share that went down first has a
   stale last row. A member whose storage ended and who asks again later keeps
   the same GitHub id, so the first term gets no final row once the new one
   appears. That is accepted for a weekly admin figure.
4. **Counts only.** The report never names anyone, and a group of fewer than 3
   measured shares shows "fewer than 3" (when it has 1 or 2 shares) and no
   figures. The floor applies to the figures. The read-only, new and stale count
   lines are counts only and are always shown, for the same admin-only reason as
   below. Totals across groups can still narrow a hidden group down. That is
   accepted, because the report is only in the admins' private store. The GitHub
   ids are in the data for whoever needs them.

**Privacy.** A GitHub id is a stable pseudonymous key, stored next to each
member's use and term dates. The store (`MorphoCloud/Incidents`) is private.
Only the MorphoCloud admins and the harvester's store token can read it.

**Changes this needs:**

- Portal (morphocloud-data-portal):
  - the `usage` command in `admin/cli.py`;
  - its line in the gate and the sudoers rule;
  - tests (`tests/test_runner_gate.py` already checks that the gate and sudoers
    agree, and a new test checks that the command only reads).
- Harvester (MWF):
  - the step in `harvest-incidents.py`, with tests;
  - `storage_usage: true` on the production source in `incident-sources.yml`.

**Testing.** Unit tests run the step on fixture `usage` documents: the week
rule, validation of bad replies, staleness, final use, the report and its
counts, and a crash between the writes. Test-Instances' portal is shelved, so
the live test is on production, where this is being built. The gate's `usage`
command is run once by hand from runner-1. The harvester is then reinstalled
from main, and nothing is forced: the build lands after Monday 13:00 UTC of
2026-W48, so the first cycle takes that week's real snapshot. The store's new
files are read back. **(2026-11-27)** The daily change is tested the same way:
unit tests for the day rule, a state file from the weekly build, the weekly
report's choice of the latest snapshot in each week, and final rows across mixed
weekly and daily files; live, the first cycle after 13:00 UTC on the day after
the install takes that day's snapshot.

**Out of scope:** a breakdown by folder, which would mean reading members'
files.

**Confirmed by the maintainer:** the snapshot time (Monday 13:00 UTC; daily at
13:00 UTC from 2026-11-27) and the thresholds (80% of the size, 5 GB and 10 GB).
The 95% count was added from the review.

### Admin dashboard (design note, 2026-11-27)

Asked for by the maintainer on 2026-11-27: one page that shows who is using
MorphoCloud now, the failures and problems, and running tallies. It is built on
this harvester, not as a separate pipeline. The harvester collects, stores and
counts. The portal only shows what the harvester hands it.

**Maintainer's decisions (2026-11-27).**

1. **Online** means a signed-in member with a portal tab visible in the last 5
   minutes. **On a desktop** is the subset of the online members who also have
   their instance's desktop open.
   - **Found in review (#477), proposed, awaiting the maintainer:** the portal
     opens the web desktop in a new tab (`/instances/N/web`, `target="_blank"`),
     and TurboVNC is a separate program. A member working on the desktop
     therefore has the portal tab in the background, and a hidden tab does not
     poll. Taken literally, decision 1 would drop them from "online" after 300
     seconds, and so from "on a desktop": the people the count is for would be
     the ones it misses. Proposed: **online = a portal tab visible in the last 5
     minutes OR a desktop open**; **on a desktop = a desktop open**, counted
     from the instance alone. On a desktop stays a subset of online. The page
     splits online into "portal only" and "on a desktop".
2. **Storage** is snapshotted daily (see "Storage usage" above, now "daily
   snapshot, weekly report").
3. The dashboard is a product of the harvester and its store, like
   `storage/report.md`. Nothing is collected only for the page.

**How it fits.**

```
 portal (mc-portal)           member instances (ACTIVE)
  presence table               port 5901 (VNC server)
       │ gate: presence              │ SSH as exouser, metadata uuid check
       ▼                             ▼
 harvester, runner-1, every 5 minutes: activity step ─► activity/YYYY/MM/DD.jsonl
       │                                                  (one line per change)
       │ dashboard step: counts from the store + this cycle
       ▼
 gate: dashboard (stdin JSON) ─► portal table `dashboard` ─► /admin (admins only)
```

The portal gets no store credential. The alternative, a token on the portal to
read `MorphoCloud/Incidents`, would put a credential that reads every incident
on the one VM that faces the internet. The harvester instead sends the portal a
document of counts and GitHub ids through the gate it already calls (`usage`,
`export-events`), with the runner's key.

#### New source 1: presence (the portal)

Portal sign-in sessions are kept in the web process's memory (decision 13 of
INSTANCE_DASHBOARD_PLAN.md). A session lasts days and does not show use, and a
restart forgets them all. So they are not the source.

The member dashboard reloads itself with `GET /` every 10 seconds while a
request is busy and every 60 seconds otherwise, and only while its tab is
visible; a hidden tab does not poll (decision 36 of INSTANCE_DASHBOARD_PLAN.md).
A signed-in `GET /` is therefore the sign of use. On each one the portal writes
the member's `last_seen` into a new table, at most once a minute per member (an
in-memory throttle, so the poll does not add a write every 10 seconds to a
database that has had lock errors). Worst case between two writes for a member
whose tab stays visible: 60 seconds of idle polling plus 60 seconds of throttle,
120 seconds, against the 300-second window (180 seconds to spare, more than one
5-minute cycle's worth of a missed poll):

| Column      | Meaning                            |
| ----------- | ---------------------------------- |
| `github_id` | primary key                        |
| `last_seen` | time of the last signed-in `GET /` |

Admins count like any member when they have the member dashboard open. `/admin`
itself does not write `last_seen`.

A new read-only gate command, `presence`, takes no arguments and prints
`{"schema": 1, "at": <time>, "rows": [{"github_id": …, "last_seen": …}]}` for
the rows seen in the last 24 hours. Same gate line, sudoers rule and
gate-and-sudoers test as `usage`.

#### New source 2: desktops (the instances)

Both ways to the desktop end at the instance's VNC server on port 5901
(exosphere `ansible/roles/guacamole/templates/user-mapping.xml.j2`, connection
`desktop`): the web desktop through `guacd` in its Docker container, which
connects to `172.17.0.1:5901`; and TurboVNC through an SSH tunnel, which
connects from the instance itself. An established TCP connection to port 5901
means a desktop is open.

Each cycle, for each `ACTIVE` server named `instance-N` in the source's project
whose setup is finished:

- **List:** the `server list --long` the `platform` step already reads (it
  returns every server, not only the hosts in `hosts:`). One call serves both. A
  server whose `exoSetup` property does not say it is complete (still in setup,
  or in the setup reboot that #476 waits for) is not checked: it has no desktop
  yet, and checking it would only produce `unknown` lines.
- **Address:** from Neutron (`port list --server`, `floating ip list --port`),
  never Nova's cached `addresses` (decision 29 of INSTANCE_DASHBOARD_PLAN.md).
  Both are already allowed by `openstack-ro`.
- **One SSH call** as `exouser` with the runner's key, the login the portal
  runner already uses on instances. Options: `-T` (no TTY), `BatchMode=yes`,
  `ConnectTimeout=10`, `ForwardAgent=no`, `ForwardX11=no`,
  `ClearAllForwardings=yes`, `LogLevel=ERROR`, and `UserKnownHostsFile` set to
  one file per server id under `~/.config/morphocloud/desktop-known-hosts/` with
  `StrictHostKeyChecking=accept-new`. The server id comes from Nova and is
  checked against
  `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` before it is
  used as a file name; anything else is `unknown`.
- **Host keys:** addresses are recycled between servers. A file per server id
  keeps a recycled address from failing on a new server's key. A changed key for
  the same server id fails (`unknown`, and the step's log names the server). No
  MorphoCloud workflow rebuilds a server in place (MWF `main` has no
  `server rebuild`; a recreate makes a new server with a new id), so a changed
  key for the same id is not expected. If one appears, the admin deletes that
  one file by hand. Files of servers that no longer exist are removed by the
  step.
- **Command:** one fixed string, the same every time, with no value from Nova or
  the store in it, so nothing is quoted per call. The harvester passes it as one
  argument to `ssh`, and the instance's login shell runs it:

  ```
  echo MC-DESK-BEGIN; curl --silent --max-time 5 http://169.254.169.254/openstack/latest/meta_data.json | python3 -c 'import json,sys; print("uuid=" + json.load(sys.stdin)["uuid"])'; echo MC-DESK-SS; ss -Htn state established '( sport = :5901 )'; echo MC-DESK-END
  ```

  A unit test checks the exact argument list.

- **Output is untrusted.** Members have sudo, so they control everything their
  instance prints (login messages, `uuid`, `ss`). The step reads at most 64 KB
  and only the lines between `MC-DESK-BEGIN` and `MC-DESK-END`. It accepts one
  `uuid=` line matching the pattern above and `ss` lines of the form
  `<recv-q> <send-q> <local address>:5901 <peer address>:<port>`, where the
  addresses parse as IPv4 or IPv6. Anything else, a missing marker, or more than
  50 connection lines is `unknown`. Nothing from the output is stored except the
  result and kind.
- **Identity:** the `uuid` must equal the server id, or the instance's result is
  `unknown` and the connection list is not read (class 5 of
  AUTOMATION_AUDIT_2026-11.md). This stops a recycled address from being counted
  for the wrong member. It is not a trust boundary against the member: a member
  can make their own instance report a desktop open or closed. The result is
  used for counts only. Nothing acts on it (no shelve, no email).
- **Result:** `open`, `closed` or `unknown`. `open` carries a kind from the peer
  address: `web` (172.16.0.0/12, where Docker puts its networks), `turbovnc`
  (127.0.0.1 or ::1) or `other`. The address itself is dropped. A failed or
  timed-out SSH call, or a server shelved while it is checked, is `unknown`,
  never `closed`. The `web` range comes from one measurement (below); Docker or
  an exosphere update could move Guacamole's network. A rise in `other` on the
  page is the sign, and the range is measured again after each exosphere update
  of the Guacamole role.
- **Owner:** the GitHub id of the instance's request issue author (Instances
  issue N, read with the harvester's Issues read; cached per issue).
- **Load:** up to 8 instances at once; the whole check stops at 60 seconds, and
  any instance not reached by then is `unknown`. On runner-1 this is one SSH
  login per running instance every 5 minutes (2 running on 2026-11-27; at 30
  running, 30 logins per 5 minutes, at most 8 at a time). The portal's `pickup`
  on the same runner makes a TCP connection to port 22 each minute, not a login,
  and logs in only to read a password. The step's own time goes into
  `harvester.json` like every step's, and the live tests read it.

Each check is one SSH login on the member's instance every 5 minutes. sshd logs
it in the instance's journal, which the member can read (members have sudo).

**Measured before writing (2026-11-27, read-only, from runner-1).** On #356
(amm554, g3.large, ACTIVE): Neutron gave 149.165.150.75; the runner's key logged
in as exouser; metadata `uuid` = server id
`0955d469-2bbc-451b-8c5e-d1033dba7f59`; ports 5901 and 49528 listening; with
nobody on the desktop, 0 established connections on 5901. With the maintainer on
amm554's web desktop: exactly one, `172.17.0.1:5901` ← `172.19.0.2`, the `guacd`
container on Guacamole's compose network (172.19.0.0/16; the instance also has
`docker0` 172.17.0.0/16 and 172.18.0.0/16). The Docker networks differ, so the
`web` rule is the whole 172.16.0.0/12 range, not one subnet. TurboVNC is not
measured yet (test T-A3).

#### The `activity` step

One step, after `platform`, for a source with `activity: true` (production only;
the harvester's default for both `activity` and `dashboard` is off, with a test,
so Test-Instances and any other source are unaffected). It reads presence (gate)
and desktops (above) and keeps the current picture in `state/activity.json`: per
GitHub id, `portal` (`last_seen` within 300 seconds of the gate's `at`), the
desktop result and kind of each of that member's instances, and `online`
(decision 1 as proposed above: `portal` or a desktop `open`).

Like `platform`, it writes a line to `activity/YYYY/MM/DD.jsonl` only when the
picture changes. Each line has `at` plus who changed and how: `portal` on or
off, a desktop `open` (with kind), `closed` or `unknown`. To keep a short outage
from writing pairs of lines, a desktop goes to `unknown` only after two cycles
in a row without a result; `open` and `closed` are written at once. A failed
presence call writes `presence: unknown` once, and its return writes the picture
again. Tallies never count unknown time as nobody online. GitHub ids only; no
login, name, email or address.

If `platform` failed this cycle, there is no server list: the step writes
nothing and keeps the last picture, marked with the time it was taken.

Size: with tens of members, a few hundred change lines a day at about 150 bytes,
under 20 MB a year. Kept with the rest of the store, no pruning.

#### The `dashboard` step

Last in the cycle, for a source with `dashboard: true`. It reads the store and
this cycle's results and builds one JSON document (`schema` 1). It is fully
derived, so it is not committed to the store. A copy is kept on the runner
(`~/.cache/morphocloud/dashboard.json`) for debugging.

| Part           | Contents                                                                                                                                                                                                                                                                                        | From                                                                    |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **Collection** | time of this cycle; each step's last result and `last_error`                                                                                                                                                                                                                                    | `harvester.json`                                                        |
| **Now**        | online ids with `since`; desktop ids with kind and instance; count of `unknown` desktops; member instances by Nova status; listed hosts not `ACTIVE`                                                                                                                                            | `state/activity.json`, `state/platform.json`, this cycle                |
| **Problems**   | for the last 24 hours and 7 days: failed and refused commands by workflow and signature; no-capacity events by cause and flavor; portal failure events; journal `ERR` lines by host; VM state changes; incidents waiting for a verdict (count, and the latest 20 with id and time)              | `outcomes/`, `rollups/`, `incidents/`, `portal/`, journals, `platform/` |
| **Tallies**    | for today (UTC), 7 days, 30 days and since the start date: distinct members online; distinct members on a desktop; hours online and on a desktop (summed from the change lines); instances created, shelved, unshelved and deleted (command outcomes `ok`); failed commands; no-capacity events | `activity/`, `outcomes/`, `incidents/`                                  |
| **Storage**    | from the latest daily snapshot: total used, total size, against the allocation's 10,000 GB; number of shares; median and 90th percentile of GB used; counts above 80% and at 95% or more; shares whose end date is within 14 days; the daily total for the last 30 days                         | `storage/`                                                              |
| **Series**     | per hour for 7 days: members online and on a desktop; per day for 30 days: failed commands, no-capacity events, storage total                                                                                                                                                                   | the same files                                                          |

Hours are summed from the change lines; time marked `unknown` is left out and
shown as a gap. Each closed UTC day's sums are kept on the runner
(`~/.cache/morphocloud/activity-days.json`, rebuilt from the store if lost), so
a cycle reads only today's file, not the whole history.

**When a step failed this cycle.** Every part carries `as_of` (when its data was
read) and `ok`. A part whose source step failed this cycle keeps its last good
figures, with `ok: false` and the older `as_of`; the page shows that part as
"not current since <time>", never as zeros. If no picture was ever taken, the
part is empty with `ok: false`, and the page says "unknown".

**No free text in the document.** It carries numbers, times, GitHub ids,
instance numbers, and names from fixed lists the harvester itself defines:
workflow file names, signature names from `incident-signatures.yml`, host names
from `incident-sources.yml`, Nova statuses, flavor names, desktop kinds, step
names. Error text, issue text and anything a member typed stay in the store.
Incident ids match `^[a-z][a-z0-9]*(-[0-9]+){1,3}$`
(`<repo short>-<run>-<attempt>-<item>`, or `<source>-<row id>` for portal and
runner events); the document carries no URLs.

**What the portal checks.** The gate command `dashboard` reads the document from
stdin (at most 512 KB) and checks it against a fixed schema before it stores
anything: every key known, every value of its type, every name in its part's
pattern (`^[A-Za-z0-9._:/ -]{1,80}$`), every series value a number, every id an
integer, every incident id the pattern above. A document that fails is refused
whole, the stored one stays, and the refusal is the step's `last_error`. It is
stored in a one-row table (`dashboard`: `at`, `body`). A failed send is the
step's `last_error`, which the watchdog emails like any other step error. The
send is every cycle, so the page is at most about 5 minutes behind.

#### The page

`/admin` gains the dashboard above its existing tables (requests waiting,
storage, all requests), which stay as they are. The portal adds login names from
its own tables (`shares`, and the login it saw at sign-in); the store and the
document carry GitHub ids only.

- **Rendering:** every string goes through the template's HTML escaping (Jinja
  autoescape, as on every portal page). The SVG charts are drawn from numbers
  only. Links to the store's incident issues are built by the portal from the
  checked incident id (the quoted search link of "Records" above); the document
  supplies no link.
- **Tiles:** online (portal only / on a desktop); on a desktop (web / TurboVNC);
  member instances running / shelved; failed commands in 24 hours; no-capacity
  events in 24 hours; incidents waiting for a verdict; storage used of 10,000
  GB.
- **Needs attention:** listed hosts not `ACTIVE`, step errors, storage ending
  within 14 days, extension requests waiting (the last one from the portal's own
  table, as today).
- **Charts:** inline SVG drawn by the portal from the series (no external
  scripts): online and on a desktop per hour, 7 days; failures per day, 30 days;
  storage total per day, 30 days.
- **Tables:** online now (login, since, desktop kind, instance); recent problems
  (time, what, link to the store's incident issue); tallies (rows by window).
- **Age:** the page shows the document's time. When it is more than 15 minutes
  old, a line at the top says so, with the time of the harvester's last cycle,
  so a stopped harvester does not look like a quiet system.
- The page reloads every 60 seconds while visible, like the member dashboard.

#### The other actors

| Actor                                  | What it does here                                        | Interleaving and rule                                                                                                                                                                                     |
| -------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Member's browser                       | `GET /` while the tab is visible                         | Tab hidden or closed: off line after 300 s plus up to one cycle.                                                                                                                                          |
| Portal web process                     | writes `last_seen`; serves `/admin`                      | A restart signs everyone out; `last_seen` is in the database and survives, and members who are not back drop off after 300 s.                                                                             |
| Portal runner (`pickup`, runner-1)     | probes port 22 each minute; logs in to read the password | Same key and user; both only read. Its known-hosts file is separate from the step's.                                                                                                                      |
| Nova (shelve, delete, unshelve)        | changes a server during a check                          | SSH fails or the server is gone: `unknown`, never `closed`.                                                                                                                                               |
| Neutron (address moved or recycled)    | the address now belongs to another server                | Metadata `uuid` ≠ server id: `unknown`; its connections are not read.                                                                                                                                     |
| Guacamole (`guacd`) and TurboVNC       | hold the connection to port 5901                         | A desktop left open in a tab is `open` while the connection lasts. Whether the member is at the keyboard is not measured. How long `guacd` keeps the connection after the tab closes is measured in T-A2. |
| The member, on their own instance      | has sudo; controls what the instance prints              | Can make the desktop look open or closed. Counts only; nothing acts on the result. Output parsed strictly (above).                                                                                        |
| Harvester (stopped, or a step failing) | no new document                                          | The page shows the age line; the watchdog emails as today.                                                                                                                                                |

#### Privacy

The store gets GitHub ids, times, and the desktop kind. No login, name, email,
IP address, process list or screen content. The page shows logins to admins
only, from the portal's own tables. The same rules as "Storage usage" above.

#### Changes this needs

- Portal (`morphocloud-data-portal`):
  - `presence` table, the throttled write on a signed-in `GET /`;
  - gate commands `presence` (read-only) and `dashboard` (stdin), their gate
    lines and sudoers rules; tests (the existing gate-and-sudoers test, a test
    that `presence` only reads, validation of `dashboard`);
  - `dashboard` table and the `/admin` section.
- Harvester (MWF):
  - the `activity` and `dashboard` steps in `harvest-incidents.py`, with tests
    (including that both are off unless a source turns them on);
  - `storage_usage` daily (see "Storage usage" above), and the references in the
    code and tests to the section's old title "Storage usage (weekly snapshot)";
  - `activity: true` and `dashboard: true` on the production source in
    `incident-sources.yml`.

#### Testing

Unit tests run the steps on fixtures: presence replies, `ss` output with web,
TurboVNC and no peers, a `uuid` mismatch, SSH failure and timeout (`unknown`),
hostile output (missing markers, extra lines, a bad `uuid`, oversize), the exact
`ssh` argument list, the two-cycle `unknown` rule, change lines and their
tallies, unknown gaps, a failed `platform` or presence step (part marked not
current, never zero), and the document's validation by the portal (unknown keys,
wrong types, a name outside its pattern, a string in a series). Test-Instances'
portal is shelved, so the live tests are on production, as amm554 (the
maintainer types where a command is needed):

| #    | Test                                                                                                                                     | Pass when                                                                                                                                                        |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T-A1 | amm554 opens the portal and keeps the tab visible; then hides it                                                                         | online within one cycle; off line within 300 s plus one cycle                                                                                                    |
| T-A2 | amm554 opens the web desktop of their instance in the new tab the portal opens, so the portal tab is hidden; then closes the desktop tab | `open`, kind `web`, within one cycle; amm554 stays online with the portal tab hidden; `closed` after (the time `guacd` takes to drop the connection is recorded) |
| T-A3 | amm554 connects with TurboVNC (Session Manager), portal tab closed                                                                       | `open`, kind `turbovnc`; amm554 online                                                                                                                           |
| T-A4 | amm554 shelves the instance while the desktop is open                                                                                    | `unknown` (or the server leaves the list), never `closed` while ACTIVE                                                                                           |
| T-A5 | the harvester's cron line is commented out for 20 minutes                                                                                | `/admin` shows the age line                                                                                                                                      |
| T-A6 | the portal's `dashboard` gate command is made to fail once                                                                               | step `last_error`; watchdog email; the next cycle recovers                                                                                                       |

The identity mismatch (a recycled address) is tested with fixtures only.

#### Parameters

| Parameter                  | Value                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Online window              | 300 seconds                                                                                                         |
| `last_seen` write throttle | 60 seconds per member                                                                                               |
| Desktop check              | 8 at once, 60 seconds for the whole check                                                                           |
| Age line on the page       | document older than 15 minutes                                                                                      |
| Storage snapshot           | daily, after 13:00 UTC                                                                                              |
| Tallies' start date        | 2026-11-27 (the first request since the migration by a member other than the test accounts: Instances #357, 19:28Z) |
| Desktop `unknown`          | after 2 cycles in a row without a result                                                                            |
| Desktop output             | at most 64 KB, at most 50 connection lines                                                                          |

#### Out of scope

What members do on their desktop or in Slicer; whether someone at an open
desktop is at the keyboard; page views other than the member dashboard; course
repositories (their own allocations and runners); Test-Instances.

### Reconcile with production after the migration (design note, 2026-11-24)

The harvester was designed before the 2026-11-23 migration. This note lists
where it no longer matches production and the one change that fixes the whole
set. Every item was checked in the code (MWF `main` a7e5fb0, portal `main`
28bd83c), on the hosts (read-only) and in the stores on 2026-11-24/25.

**What was found.**

| #   | Gap                                                                                                                                                                                                                                                                                            | Evidence                                                                                                                               |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | The production portal VM `mc-portal` is not in `hosts:`. Its journal is not read.                                                                                                                                                                                                              | `incident-sources.yml` `hosts:`; `openstack server list` (BIO180006): `mc-portal` ACTIVE, 10.0.16.108; no `mcjournal` account on it    |
| 2   | The Test side is still configured: the `MorphoCloud/Test-Instances` source (three hosts) and the Test crontab target, which runs a harvester into `Incidents-sandbox`. All three Test VMs are shelved. The sandbox store's watchdog emails every 6 hours that `mc-data-portal` cannot be read. | `incident-sources.yml`; `check-workflow-clock.py` `TARGETS`; BIO240357: MC-Test-runner, mc-data-portal, mc-join-test SHELVED_OFFLOADED |
| 3   | 16 workflow names in `workflows:` exist in neither MWF nor Instances (the two retired by #459, old Instances-only files, Test-only files). They are still needed to classify old runs; only some say so.                                                                                       | GitHub contents API, Instances `.github/workflows` (41 files)                                                                          |
| 4   | The portal's 1-minute job (`pickup`) shelves an unreachable instance with `gh workflow run` and the dispatch token, and writes no `DISPATCH` line. Its runs would be counted as `admin` (Phase 2's exit says none may be). None has happened since the migration.                              | portal `runner/mc_share_runner.py` `dispatch_shelve`                                                                                   |
| 5   | The monthly bake emails through `admin-alert.yml` and logs its `DISPATCH` line to `~/bake/cron.log`, which the harvester does not read. Its 2026-11-24 23:51 email became incident `instances-45513468666` with `actor_kind: admin`. The bake's log itself is not in the store.                | `bake-monthly.py` `email()`; store record                                                                                              |
| 6   | The control runner's (runner-2) Actions `_diag` error lines are never read: `control_host` is null, and its code path logs in as `exouser`, which runner-1 cannot do (publickey refused). `mcjournal` on runner-2 cannot enter `/home/exouser` (mode 750).                                     | `incident-sources.yml`; `harvest-incidents.py` `remote_diag`; read-only checks on both runners                                         |
| 7   | `mc-k8s-portal` (BIO180006) is a server VM not listed and not excluded.                                                                                                                                                                                                                        | `openstack server list`                                                                                                                |

Checked and in order (no change): the workflow kinds; commands posted by the
portal (the member's own login, so `actor_kind` is right, and the
`request-source:portal` label is a marker); `portal_only` refusals; the
storage-usage snapshot (2026-W48, 44 shares); `expire-shares` failures reach the
store; runner-1's harvester files match `main` byte for byte; the join app's
GitHub sign-in adds no log file.

**Decided by the maintainer (2026-11-24).**

- Test-Instances is sunset for now and will serve another purpose later. Every
  change here is for production, built from `main`. The Test crontab target is
  removed too.
- A successful bake stays an incident: its email asks the maintainer to promote
  the images and gives the exact commands. Only its attribution changes, to
  automation.
- `mc-k8s-portal` is out of scope.
- Runner-2's `_diag` error lines are read through the read-only `mcjournal`
  account.
- The portal events (evidence G, `portal_events` and `export-events`) are their
  own design and PR, right after this one. Only the one-line `pickup` `DISPATCH`
  log is done here.

**The change.**

MWF (one PR):

- `incident-sources.yml`:
  - a host `mc-portal` (`nova: mc-portal`, `ssh: mcjournal@10.0.16.108`) with
    units `mc-data-portal.service`, `mc-data-worker.service`,
    `mc-data-transfer.service` (new since Test: the MorphoSource cart),
    `copyparty.service`, `caddy.service`, `mnt-shares-*.mount`, `cron.service`.
    No `files:`: the portal logs only to the journal;
  - `diag: true` on the `morphocloud-instances-runner-2` host; `control_host` is
    removed;
  - a comment under `hosts:` that `mc-k8s-portal` is left out on purpose: out of
    scope (maintainer, 2026-11-24). The file's own rule is that leaving a server
    out is an exclusion and must say why;
  - the `MorphoCloud/Test-Instances` source is removed;
  - `runner_logs` gains `{ name: bake, path: ~/bake/cron.log }`. That ships the
    bake's log and lets rule 1 find its `DISPATCH` line, so its email is
    `automation`;
  - every workflow name that no longer exists says "retired <date>; kept for the
    runs already recorded".
- `scripts/mc-journal-read.py`: a third request form, `diag`. It reads the
  per-file offsets as JSON on standard input and answers in the format
  `remote_diag` already parses (`==FILE <name> <size>`, then the matching
  lines). It reads only the directory named on the `diag-dir` line of the
  root-owned allowlist, only files changed in the last 48 hours, and only lines
  matching the `diag_match` pattern, which the reader holds itself. Nothing else
  becomes readable.
- `scripts/harvest-incidents.py`: `remote_diag` sends the `diag` request to the
  host's `mcjournal` account instead of a shell script as `exouser`; offsets are
  pruned to files seen in the last 48 hours, so the request stays small
  (runner-2 keeps 27,000 `Worker_*.log` files, 5.7 GB).
- `scripts/install-journal-reader.sh`: `--diag <dir>` writes the `diag-dir` line
  and gives `mcjournal` traverse-only access to the home directory
  (`setfacl -m u:mcjournal:x /home/exouser`). `_diag` and its files are already
  world-readable (775 and 644).
- `scripts/check-workflow-clock.py`: the Test target is removed;
  `scripts/test-instances-runner.crontab` is deleted;
  `runner-cron-dispatcher.md` and the comment in
  `restore-instances-dispatcher.sh` are updated. The rendered production crontab
  must come out byte-identical (checked in the PR).
- Tests: the Test-source fixtures move to production names; new tests for the
  `diag` form (allowlist only, the 48-hour window, the match), the bake
  `DISPATCH` join, and an `mc-portal` journal line.

Portal (one small PR, `morphocloud-data-portal`): `dispatch_shelve` logs
`DISPATCH control-instance-from-workflow.yml repo=<repo> issue=<n> by=pickup result=<ok|FAILED …>`
in the uniform format, with a test. `pickup.log` is already shipped, so rule 1
finds it.

**Actor map: who else touches each resource.**

| Resource                                                                                                | Who writes or acts on it                                                                                    | Who reads it                                   | What this change does to it                                                                                                              |
| ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| runner-1 harvester files (`~/harvest-incidents.py`, the two `.yml`, `redact.sed`, `mc-journal-read.py`) | `install-harvester.sh` (maintainer), `restore-instances-dispatcher.sh`                                      | the harvester, every 5 minutes, under its lock | replaced from `main` by `install-harvester.sh` (files only)                                                                              |
| runner-1 crontab                                                                                        | `restore-instances-dispatcher.sh` (managed block); `install-runner.sh` (the pickup line, outside the block) | cron                                           | **no change**                                                                                                                            |
| `~/bake/cron.log`                                                                                       | the bake (`0 8 * * 2`, `0 9 * * 1`)                                                                         | the maintainer; from now on the harvester      | read only, from its last offset                                                                                                          |
| `~/mc-data/pickup.log`, `~/mc-data/mc_share_runner.py`                                                  | pickup every minute; `install-runner.sh` or a copy of the file                                              | the harvester (already)                        | the script gains the `DISPATCH` line; copied after the portal PR merges, crontab untouched                                               |
| `mc-portal` VM                                                                                          | portal deploy (`provision.sh`); `mcrunner` (pickup and usage through the gate); members through the web app | —                                              | new locked `mcjournal` account with the runner's key, forced to the reader; read-only                                                    |
| runner-2                                                                                                | Actions runner service (writes `_diag`); `mcjournal` (journal reads, already)                               | the harvester, through `mcjournal`             | the reader is replaced; one ACL entry (`x` on `/home/exouser`)                                                                           |
| store `MorphoCloud/Incidents`                                                                           | the harvester only (decision 18)                                                                            | its watchdog; the maintainer                   | new: `mc-portal` journal, runner-2 `diag.log`, `bake.log`; `hosts` gains `mc-portal`                                                     |
| production watchdog                                                                                     | GitHub schedule                                                                                             | `state/harvester.json`                         | no change; it emails if `mc-portal` stays unreadable for 2 hours, so the reader is installed before the config                           |
| store `MorphoCloud/Incidents-sandbox`                                                                   | the Test harvester (stopped: MC-Test-runner shelved)                                                        | its watchdog (emails every 6 hours)            | its watchdog is disabled; the store is kept as it is                                                                                     |
| MC-Test-runner (shelved)                                                                                | nobody while shelved                                                                                        | —                                              | **not touched.** Its old crontab still holds the Test harvester and dispatcher lines; whoever unshelves it must clear that crontab first |

**Rollout, in order.** Each step on production needs the maintainer's OK.

1. Both PRs merged after the Claude review. Merging changes nothing on a host:
   runner-1 keeps running its installed copy until step 4.
2. `install-journal-reader.sh` on `mc-portal` (as `ubuntu`, through runner-1 as
   jump host), with no `files`. Its last check logs in from runner-1 as
   `exouser` with `StrictHostKeyChecking=accept-new`, which records
   `10.0.16.108`'s host key in the `known_hosts` the harvester uses (it runs as
   `exouser` with `BatchMode=yes`). Steps 2 and 3 come before step 4, so the new
   config never meets a host it cannot read.
3. `install-journal-reader.sh --diag /home/exouser/actions-runner/_diag` on
   runner-2.
4. `install-harvester.sh exouser@149.165.152.104` (files and tokens only; no
   crontab change).
5. Only after the portal PR has merged: copy the portal's
   `runner/mc_share_runner.py` to runner-1's `~/mc-data/` after a diff against
   the copy there (it matches portal `main` before that PR).
6. `gh workflow disable watchdog.yml -R MorphoCloud/Incidents-sandbox`. The
   emails about `mc-data-portal` stop here.
7. Check: the next cycle's `state/harvester.json` shows `mc-portal` and runner-2
   `ok` and `last_error` null; `runner/<day>/mc-portal/journal.log` and
   `runner/<day>/morphocloud-instances-runner-2/diag.log` appear; the harvest
   log shows no error.

**Testing.** Test is sunset, so there is no Test run. Unit tests cover each
change. Steps 2, 3 and 7 are the live checks, each run once by hand on the real
host. The bake attribution is checked at the next bake email (first Tuesday of
December, or the Monday overdue check), by reading its record.

**Cost.** No new crontab line, no new token. `mc-portal`'s journal on Test was
about 40 KB a day; runner-2's `_diag` errors are expected to be similar to
runner-1's.

**As built.**

- The harvester keeps runner-2's `_diag` offsets in its runner-side state, each
  with the time the reader last named the file, and forgets one after 3 days.
  (The build said 30 days and about 250 KB, from a wrong estimate of runner-2's
  logs. Measured at rollout, 2026-11-25: about 920 Worker logs a day, so 30 days
  meant a 1.3 MB request every cycle, near the reader's 2 MB limit; with the
  reader's 2-day window, 3 days keep about 200 KB.) A live file is named at
  every call, so it is never forgotten. It sends that limit as `forget_before`.
  A file the reader finds without an offset starts at its end when its name
  (`Worker_YYYYMMDD-HHMMSS-utc.log`) dates it before that limit, so a forgotten
  file's lines never ship twice; a newer file, or one without a date in its
  name, is read from its start (a new file).
- The reader caps its output at `CAP` from the first file on. When the cap or
  its time budget is reached, it names every remaining file the harvester knows
  with the harvester's own offset, so those offsets and their "last seen" stay
  current and the rest is read next cycle. A single error line longer than the
  cap is cut to it, and a run of 64 MB without a newline is skipped, so no file
  can stop the reader.
- A first sight starts each file after its last whole line, never in the middle
  of a line.
- Known limit: a `_diag` file rewritten to a size larger than its old offset is
  not seen as rewritten. The runner only appends to these files.
- A `_diag` read that fails is logged every cycle. After 2 hours it is named in
  `last_error`, so the store's watchdog emails about it. A host whose journal
  answers is not marked "unreachable" for this.
- The reader opens `_diag` files with `O_NOFOLLOW` and reads only regular files,
  so a link placed in `_diag` cannot expose another file to `mcjournal`.
- The rendered production crontab is byte-identical before and after (checked
  with `check-workflow-clock.py render`).
- `install-journal-reader.sh --diag` stops with a clear message when `setfacl`
  (package `acl`) is missing. Runner-2 has it.
- Everything new reaches the store through the harvester's redactor
  (`redact.sed`): `diag.log` through `ship_capped`, `bake.log` through the
  runner log step. The portal's `DISPATCH` line carries only the workflow,
  repository, issue and a one-line reason, never the token.
- `bake.log` is read from its start the first time (it began on 2026-11-24 and
  is a few KB). The incident of the bake's install-test email,
  `instances-45513468666`, stays `admin`: `--rederive` does not recompute
  `actor_kind`, and one record is not worth a rewrite.
- Left alone on purpose: `Test-Instances` in `noxfile.py` (a vendorize target
  for its later use), `morphocloud-dispatch.sh`, `police-floating-ips.py` and
  the composite actions (repository-name checks that do nothing while Test has
  no runs). The `Incidents-sandbox` store and its data are kept; its watchdog
  stays disabled until Test's new purpose is designed.

### Volume

Measured on Instances over 2026-10-09 to 11-07: about 1,100 runs a day, about
300 command runs and about 125 incidents a month (45 failed commands, 79 failed
sweeps and other runs, 62 of them `update-request-status-label`).

| Production, per month                                             | Size       |
| ----------------------------------------------------------------- | ---------- |
| outcome lines (~400) and rollups                                  | ~0.4 MB    |
| incidents (~125 at ~70 KB with the job log)                       | ~9 MB      |
| setup records for every create (~40 at ~150 KB)                   | ~6 MB      |
| job logs of the other command runs (~275 at ~32 KB) **(amended)** | ~9 MB      |
| portal events and every runner log line **(amended)**             | ~3 MB      |
| **total**                                                         | **~27 MB** |

About 330 MB a year, under GitHub's 1 GB guidance for about three years, after
which the archive rule in "Retention, size, cost" applies. The 90-day backfill
adds about 55 MB once (incident and command-run logs). `check-added-large-files`
at 2 MB in the store's pre-commit.

## Signatures

`incident-signatures.yml` lives in MorphoCloudWorkflow and is vendorized. It is
a controlled vocabulary: the collector assigns from it, the agent may propose
additions in its decision record, and only the maintainer adds them. Seeded from
what the prototype already distinguishes:

| Family     | Signatures                                                                                                                                                                                            | Assigned by                                   |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `create`   | `no_capacity`, `host_failure`, `no_address`, `api_error`, `timeout`                                                                                                                                   | notify-admins class + failure_mode            |
| `unshelve` | `no_capacity` (dec. 30), `host_failure`, `no_address`, `address_not_attached`, `wrong_end_state`, `timeout`                                                                                           | notify-admins class + Nova failed step        |
| `shelve`   | `wrong_end_state`, `timeout`, `api_error`                                                                                                                                                             | notify-admins class                           |
| `setup`    | `error:<task>`, `stalled:<task>`, `timeout:<task>`, `ceiling`, `ssh_auth` (dec. 33; task from C5)                                                                                                     | resume-setup / follow-through                 |
| `followup` | `retries_spent`, `server_error` (dec. 28)                                                                                                                                                             | follow-through                                |
| `drift`    | `active_without_address`, `repair_failed` (dec. 34), `stuck_shelved`, `error_state`, `address_on_shelved`                                                                                             | reconcile                                     |
| `network`  | `unreachable_shelved`, `could_not_shelve` (dec. 23), `non_routable_address`, `address_drought`, `address_drought_over` (dec. 24), `wrong_server_at_address` (**amended:** `fip.py verify` exit 4, B4) | admin-alert subject; B4                       |
| `capacity` | `project_cap` (**amended:** `error:system-capacity`, B14), `quota` (**amended:** a limit at its ceiling in F6's `limits show`)                                                                        | create-instance label; F6                     |
| `email`    | `credentials_failed`, `admin_send_failed`                                                                                                                                                             | notify-admins / admin-email-failed            |
| `github`   | `api_5xx` (the top failed-run cause, #317), `command_dropped`                                                                                                                                         | error line of the failed step; report-dropped |
| `share`    | `expire_failed`, `mount_failed`                                                                                                                                                                       | expire-shares; setup                          |
| `member`   | `help_request`                                                                                                                                                                                        | on-admin-mention                              |
| `monitor`  | `dormant_workflow`                                                                                                                                                                                    | detect-dormant-workflows (digest only)        |
| —          | `unclassified`                                                                                                                                                                                        | fallback                                      |

`github.api_5xx` is classified from the failed step's error line, never by
grepping the whole log: every log contains github.com URLs. A growing
`unclassified` share is itself a finding.

## The triage agent

`triage.yml` in the store runs on a push that adds an `incident.json`, on the
`incidents-agent` runner, one job per episode (concurrency group
`triage-<episode>`, so two incidents of one episode never race).

**1 · Group (shell).** Assign the episode (decision 12), and record a repeat
(same instance and signature within 24 h).

**2 · Case file (shell).** Using a read-only token, assemble into the workspace:
the incident directory; the issue's timeline since the request (comments,
labels, reactions); the last 30 days of outcomes and decisions for the instance;
the episode; the latest runner report for the instance where the portal exists;
the signature list; the playbook catalog with each playbook's current level; the
signature's history from the store: its base rate, its last 20 incidents with
the maintainer's verdict and what was done, and whether it is novel (decision
16). The token is not passed to the next step.

**3 · Decide (agent).** `anthropics/claude-code-action` with tools limited to
reading the workspace and writing `decisions/<id>.json`:

```json
{
  "id": "…",
  "episode_id": "…",
  "verdict": "act | page | wait | digest | none",
  "playbook": "rerun-github-5xx",
  "params": { "run_id": 12345678901 },
  "wait_for": { "rule": "follow-through", "until": "2026-11-08T15:00:00Z" },
  "signature": "unshelve.host_failure",
  "proposed_signature": null,
  "confidence": "high | medium | low",
  "user_waiting": true,
  "summary": "two sentences for the maintainer",
  "evidence": ["incidents/…/diagnostics.md#L12"],
  "similar": ["<incident ids with verdicts>"]
}
```

- **act**: a playbook in the catalog fits and its preconditions hold.
- **page**: the maintainer is needed now. This is always the verdict when a
  member is waiting and no playbook or owner rule covers the case, when a server
  is in ERROR, when the case touches a delete or a share, and when the agent's
  confidence is low.
- **wait**: an existing rule owns the case (`owner_rule`); the deadline is that
  rule's own (two follow-through attempts, the half-hour reconcile repair). The
  stale check turns an expired wait into a page.
- **digest**: no member is waiting and nothing needs doing now.
- **none**: already resolved (e.g. the member's own retry succeeded).

**4 · Validate (shell).** Schema-check the record. A malformed or missing
decision becomes `page`. Then apply the rules that are not left to the model:

- a novel signature (decision 17) is `page`, and the agent's own verdict is kept
  as a shadow record;

- a repeat (same instance and signature within 24 h) is at least `page`, unless
  a playbook at level act covers it and has not already run on this instance
  today;
- `member.help_request` is always `page` until its playbook level says
  otherwise;
- `act` on a playbook below its required level becomes a proposal (confirm) or a
  shadow record.

Commit. When the verdict is act and the mode allows it, dispatch
`incident-act.yml` on the source repository with the decision id.

**5 · Notify.** A page is one email to the maintainer's address with the
summary, the proposed action if any, the incident link, and an issue in the
store (`page:<id>`) for the verdict. Pages are capped at one per episode.
Everything else waits for the digest.

**Clocked jobs (runner crontab):**

- `digest.yml`, twice a day: one email listing digest and act items since the
  last digest. Each item carries a "should have paged" link (decision 6).
- `stale-check.yml`, every 10 minutes: a `wait` past its deadline, or a `digest`
  whose instance is still unhealthy in the latest outcome or runner report after
  12 h, is promoted to `page`.
- Collection completeness needs no separate audit: the harvester lists every
  run, so its own lag is the measure ("When collection fails"). A heartbeat
  email would not do: its silence would look like health.

## Playbooks and the executor

`incident-playbooks.yml` (MorphoCloudWorkflow, vendorized) holds one entry per
action:

```yaml
- name: rerun-github-5xx
  level: shadow # shadow | confirm | act — changed only by the maintainer
  matches: [github.api_5xx]
  preconditions:
    - failed step's error line is a GitHub 5xx
    - no newer command on the issue since the failed one
    - instance state unchanged since the failure (re-read)
    - run_attempt < 3
  does: gh run rerun --failed <run_id>
  member_note: null
  post_check: rerun concluded success within 30 min
  budget: { per_issue_per_day: 2, per_hour: 10 }
```

Initial candidates, each something the maintainer does by hand today and each
mapped onto an existing workflow:

| Playbook              | Covers                                                                                          | Dispatches                                                       |
| --------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `rerun-github-5xx`    | a command or sweep that failed only on a GitHub 5xx (#317's auto-rerun)                         | `gh run rerun --failed`                                          |
| `resend-access-email` | `email.credentials_failed` once mail works                                                      | `control-instance-from-workflow` `email`                         |
| `finish-after-settle` | an abandoned command follow-through does not cover (its two attempts spent, server now settled) | `control-instance-from-workflow`, reason `agent:<id>`            |
| `resume-setup`        | `setup.stalled` whose console later shows `complete`                                            | `resume-setup.yml`                                               |
| `draft-js2-ticket`    | a host or Neutron failure that repeats across instances                                         | none: a drafted ACCESS ticket with request ids, sent in the page |

`incident-act.yml` (self-hosted control runner, per run) does the following:

1. Reads `MORPHOCLOUD_AGENT_MODE`; `off` exits.
2. Fetches the decision from the store, finds the playbook, and checks that its
   level allows acting.
3. Re-checks every precondition against live state (OpenStack, labels, the
   issue's comments).
4. Checks the budget. Every action counts against it, so an excess stops the
   playbook and pages.
5. Dispatches with reason `agent:<decision id>`, so `control-instance` re-checks
   the reason under the per-issue lock (audit class 2).
6. Posts the playbook's templated note on the member's issue (decision 35).
7. Appends the result to the decision record.

On the next tick, the stale check runs the post-check. A failure demotes the
playbook to `confirm` and pages.

**Levels.**

- **shadow**: the decision is recorded; nothing happens beyond today's emails.
- **confirm**: the page carries the proposed action. The maintainer comments
  `/approve` on the `page:<id>` issue and a store workflow dispatches the
  executor. Every approve or reject is a verdict.
- **act**: the executor runs. The action appears in the digest, and any one can
  be reverted by setting the mode to `off`.

**Promotion** (maintainer's decision, on the numbers the analysis reports):

- shadow → confirm once every signature the playbook matches is not novel, and
  after 20 shadow decisions on the playbook with the maintainer agreeing on 19
  or more, and no disagreement where the action would have harmed a member.
  Replayed baseline cases do not count toward the 20; only live ones do;
- confirm → act after 20 approved runs whose post-checks all passed, then a week
  report-only on production (decision 35).

**Demotion** is automatic to `confirm` on any maintainer override of an act
decision, any failed post-check, or a budget excess.

## The maintainer's verdict

The agent is only as good as the record of what the right call was. Three
low-effort inputs feed it:

- **Page issues.** Every page opens `page:<id>` in the store. Closing it with a
  label records what should have happened, in the same words the baseline uses,
  so the agent's calls and the baseline compare one to one: `needed-me`,
  `could-have-waited`, `nothing-to-do`, `test-induced`, or
  `should-have-acted:<playbook>`. An optional `/did <text>` comment records a
  manual action that is not a playbook yet.
- **"Should have paged" links** in the digest. Each one opens a prefilled issue
  `missed:<id>`; opening it is the verdict. A missed page is the one error that
  must stay at zero.
- **Implicit labels.** Outcomes with `actor_kind: admin` on the same issue
  within 24 h of an incident record what was done by hand, with no form.

`verdicts/<id>.json` is written by the harvester from these, when it sees the
issue closed. During the baseline (Phase 2) there is no agent and no page: every
admin email the system already sends opens an `incident:<id>` issue in the
store, and the email links to it. Closing it with one label (`needed-me`,
`could-have-waited`, `nothing-to-do`, or `test-induced` for a deliberate test
the test-account mark did not catch, plus `/did <text>` when something was done
by hand) is the whole cost to the maintainer. `test-induced` incidents, and
every run marked `actor_kind: test`, stay in the store but are left out of the
base rates and of what the agent is graded on. The implicit labels fill in what
was done. This is the record the agent is graded against before it is trusted
with anything.

## The analysis agent

`analyze.yml`, weekly from the runner clock, on `incidents-agent`, with DuckDB
views over incidents, outcomes, episodes, decisions and verdicts. Its first run
is at the end of the baseline, over the whole of it. Until then the weekly
numbers come from plain SQL (`scripts/incident-rates.sql`) in the digest, with
no model involved. The prompt asks for these, in order:

1. **Base rates** (pure SQL; the agent reports and does not estimate): failure
   rate per command, signature, flavor, hour, workflow version and dispatch
   reason; queue wait; share of incidents closed with no maintainer action.
2. **Changes**: signatures whose rate moved against the prior four weeks, with
   the first `workflow_version` they appear on (a regression), or the Jetstream2
   status for the window (a cloud-side change).
3. **Themes**: for each cluster worth attention, a hypothesis citing the
   incidents that support and contradict it, kept in `reports/themes/<slug>.md`
   so a theme's history is one file.
4. **Rule candidates**: where a theme points at our code, an issue on
   MorphoCloudWorkflow written as a decision proposal. It states the pattern and
   evidence, the deterministic rule or new playbook, the actors it interacts
   with (from the audit's actor map), and the test that would replay it. Issues
   only, never pull requests (design first, one PR per class). Where a theme
   points at Jetstream2, a drafted ACCESS ticket.
5. **Manual actions that repeat**: a `/did` or admin-dispatched action seen 3 or
   more times on one signature becomes a playbook candidate. This is the path by
   which autonomy grows: the maintainer's repeated hand action becomes a
   proposed playbook, which is approved, then shadowed, then acts.
6. **Agent performance**: per playbook and per signature, agreement with
   verdicts, missed pages, post-check pass rate, and the promotion and demotion
   recommendations.
7. **Vocabulary**: `proposed_signature`s seen 3 or more times, as a proposal.

Output: `reports/<ISO week>.md` and one summary email.

## Use case recorded 2026-11-09: a platform-wide outage notice

Asked for by the maintainer after the Jetstream2 storage failure of 2026-11-09
(official notice: "Jetstream2 is experiencing storage failures and is currently
unavailable"). That day the users were told by hand, two hours in, from the
maintainer's laptop (the portal and the join app were themselves down). The
agent should do this, under these rules:

- **Detect** a platform-wide outage, Jetstream2 or GitHub, not a MorphoCloud
  bug. Never from one signal. The 2026-11-09 pattern: every self-hosted runner
  offline to GitHub at once (production runners and the Test runner, three VMs);
  the dispatcher stopped creating runs; the store heartbeat went stale; SSH to
  several floating IPs timed out while GitHub itself answered; Nova later showed
  every running VM `PAUSED` (control plane back, VMs not). A GitHub outage
  inverts the picture: runners online, githubstatus.com reports an incident, our
  API calls fail. A single dead runner, a dead harvester with runners online, or
  a stale store with runs still completing is **our** problem and must never
  produce a users' notice.
- **Corroborate** with the outside feeds the harvester already reads (ACCESS
  current outages, githubstatus.com) and the Jetstream2 status page, and say in
  the record which signals agreed. The ACCESS feed had nothing posted an hour
  into the 2026-11-09 outage, so its silence is not evidence of health.
- **Wait** one to two hours of sustained signals before any email: brief blips
  recover on their own and a notice for them costs trust.
- **Act**: email every verified user (the join app's recipient rule, from the
  intake backup snapshot on GitHub when the portal is down; batches of eight
  through Dynadot) with the platform's own words, what is unreachable, that no
  action is needed and commands should not be re-sent, and a follow-up when
  service returns. Then write the incident like any other, with the signals, the
  timeline and the notice as evidence.
- **Baseline first**, like every other playbook: record-only until the
  maintainer has seen it fire correctly on real outages, then the email.
  Unpausing or restarting VMs after such an event is a separate decision and
  stays with the maintainer.

## The agent among the other actors

The agent's actions go through the same dispatch and lock as every other
automated actor (AUTOMATION_AUDIT_2026-11.md):

| Other actor                        | Rule                                                                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| member commands                    | Precondition "no newer command since the failure"; the executor's dispatch is re-checked under the lock and yields to a member. |
| follow-through (dec. 28)           | While `followup:*` is on the issue the verdict is `wait`; the agent acts only after follow-through has spent its attempts.      |
| reconcile repair (dec. 34)         | While a repair is pending the verdict is `wait`.                                                                                |
| auto-shelve / portal (dec. 23, 26) | No playbook shelves; an unreachable instance is theirs.                                                                         |
| floating-IP policing (dec. 24)     | No playbook touches addresses directly; an unshelve no-op path takes from the pool like any other start.                        |
| End of Life / expire-shares        | No playbook on an expired or closing request.                                                                                   |
| maintenance switch (dec. 31)       | The executor treats `MORPHOCLOUD_MAINTENANCE` like the sweeps do (keeps working), except that member notes are suppressed.      |

Before any playbook goes beyond shadow, its interleavings with each of these are
tested on Test as amm554, from a test plan the maintainer approves first.

## Redaction and privacy

Applied twice with the same rules: by `record-facts` inside the job, to every
value and file **before** the artifact is uploaded, and by the harvester to
everything it writes (facts again, job logs, issue comments, portal rows). Two
reasons it runs in the job:

- a run artifact can be downloaded by anyone with read access to the source
  repository, members included;
- GitHub's masking covers the rendered log, not artifacts.

The artifact is then encrypted to the harvester's public key and kept 7 days, so
a rule that misses something is still unreadable to members. The store watchdog
reports a stopped harvester long before the 7 days run out. The rules are in
`.github/actions/record-facts/redact.sed`, vendorized with the action; the
harvester reads the same file. They are versioned and stamped in the record:

- GitHub tokens (`ghp_`, `ghs_`, `github_pat_`), private-key blocks, the SMTP
  password, OpenStack application-credential secrets, `OS_PASSWORD`;
- the instance passphrase (`exoPw`) and VNC password lines;
- **(amended)** the CephFS share access key (`access_key` in
  `mc_share_runner.py`'s output). It is masked in the job today and travels over
  SSH stdin, so it never reaches the console or the artifact; the rule is belt
  and braces;
- email addresses, replaced by an HMAC with a secret key (an Actions secret for
  `record-facts`, the same key on the harvester host), so one member correlates
  across incidents but the address cannot be recovered by hashing guesses.

**Every protection in `record-facts` fails closed:**

- If the HMAC secret is missing (not set in a repository, or not passed into a
  reusable workflow), email addresses are **dropped** (`[email]`), never written
  in clear. The facts record `email_hash: unavailable`.
- If the encryption certificate is missing, **nothing is uploaded**: the run is
  recorded as `facts: missing`, with a `::warning`.

The redaction test covers both: a fixture run with no key and no certificate
must produce no address and no artifact.

There is no rule for values a job registered with `::add-mask::`: nothing
outside the job can learn them. Job logs the harvester downloads are already
masked by GitHub. The facts carry only what the rules above remove.

From inside an instance, only system setup logs are read (C1–C4), and only
during create and resume-setup, before the member has ever logged in. The
consoles in D1 and D2 are the serial console, not the member's files. Nothing is
read from a lived-in instance's disk. The portal's `connections` table
(passphrases) is never read. A CI test runs the rules against a fixture with one
of each and asserts nothing survives. GitHub logins are kept: they are already
public on the issues.

## Retention, size, cost

- **Git**: everything, about 27 MB a month on production (see "Volume"). Kept
  indefinitely; if the store ever nears 1 GB, logs older than two years move to
  a separate archive repository.
- **Actions minutes** (GitHub-hosted jobs in private repositories bill at about
  $0.006 a minute, each job rounded up to a minute; self-hosted runners and the
  runner crontab are free):

  | Item                                                                                                                                                   | Billed minutes / month |
  | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------- |
  | harvester, backfill (runner crontab)                                                                                                                   | 0                      |
  | facts in self-hosted jobs (command workers, sweeps, setup)                                                                                             | 0                      |
  | store watchdog, every 6 hours                                                                                                                          | ≤120                   |
  | weekly numbers email (`Incidents`)                                                                                                                     | ~5–10                  |
  | facts upload in the GitHub-hosted jobs that send admin email (`on-admin-mention`, `report-dropped-command`, `detect-dormant-workflows`, `admin-alert`) | ~20                    |
  | **steady state**                                                                                                                                       | **~150 (~$1)**         |
  | Phase 1 testing on Test-Instances, once                                                                                                                | ~150 (~$1)             |

  Two rules keep it there:
  1. **No facts upload from the GitHub-hosted authorize jobs.** They run on
     every command (~300 a month), and an upload that pushes a job past a minute
     boundary bills a whole extra minute. Their refusal reasons are read from
     the job log by the harvester instead, from the one `REFUSED reason=` line
     each refusal prints (B8).
  2. **Verdicts are recorded by the harvester when it sees a verdict issue
     closed, not by a workflow in the store.**

- **Model usage**: `CLAUDE_CODE_OAUTH_TOKEN` (subscription). Triage is capped at
  one run per episode and at most 40 a day; above that, triage pauses and one
  page says so. Analysis runs weekly.

## Phases

Each phase is one feature: design approved, built whole, one review, tested on
Test as amm554 from an approved test plan, then production.

| Phase | Scope                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Exit                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | **Data collection, one feature, three PRs on one seam (amended 2026-11-08).** Design note first (the evidence inventory plus what each capture adds inside the per-issue lock: the pre-shelve capture D1/D2, the SSH reads C3–C4 after `verify`), approved by the maintainer. The PRs follow the production two-step, so each stands alone and the harvester meets production's run volume and second runner weeks before the facts exist (Test reproduces neither). **PR A (MorphoCloudWorkflow): the harvester side.** The store repos; `harvest-incidents.py` with its PAT, its OpenStack credential or wrapper (decision 18), the `limits show` snapshot, the full job log of every command run (A3) and every runner log line (F); the uniform `DISPATCH` line at every runner-side `gh workflow run` call site (`morphocloud-dispatch.sh` with `repo=` and the failure text, `police-floating-ips.py` notes and alerts, `check-dispatch-tokens.sh`; the portal runner's `pickup` is in PR C); `check-dispatch-tokens.sh` gains the harvester PAT; `TARGETS` gains the store field; the restore script drops stray harvester lines and installs the keys; the rebuild inventory. Ships to production by hand before the merge. **PR B (MorphoCloudWorkflow): the job side.** `record-facts` (redaction and encryption in the job) and every facts site (B, C, D: B13's three-valued status output, B14, the `fip.py` result fact, the `MC_EGRESS_PROBE` console line, `failed_extensions` among them), the facts upload as the last step of every self-hosted job, `run-name`, the version file, signatures, the incident link in the body of every admin email (a one-line edit at each sender the B10 check finds), and the three CI checks of decision 20. Moving the admin-email senders onto `notify-admins` is **not** part of this phase. Lands with the 2026-11-23 merge. **PR C (portal): `portal_events`, `export-events`, the `pickup` `DISPATCH` line.** Lands with the migration. Each PR: one review, built and tested whole on Test-Instances as amm554, writing to `Incidents-sandbox`, from a test plan approved first. **Then production, in two steps** (see "Production only, and before the merge"). **Before the merge**, with the maintainer's go-ahead for that production change: the harvester is installed by hand on the acquire runner, records A, E, F and H, and runs the 90-day backfill of Instances; the store watchdog and the `check-dispatch-tokens.sh` entry go live. **At the 2026-11-23 merge**: the facts (B, C, D), the keys, the rendered harvester line replacing the hand-added one, and with the migration the portal (G). | Every provoked failure (impossible flavor, setup forced to fail, setup stalled, unreachable instance, GitHub error by a bad token) and a normal create, unshelve and shelve, checked line by line against the inventory. Every run in the Actions UI is in the store as a row or a count, including a long-queued run, a rerun of a run more than 48 hours old, and a rerun's facts (attempt 2's artifact uploaded beside attempt 1's). A downloaded facts artifact is unreadable without the key, and its decrypted content passes the redaction test. With the HMAC secret or the certificate removed, no address and no artifact leave the job. The rendered production crontab carries the harvester lines and the hand-added ones are gone; the restore script installs the keys and the PAT from the `~/.ssh` backups, and with a key missing it still restores the clock, warns, and the watchdog reports the harvester stopped. Stopping the harvester's cron line makes the watchdog email within its next deliveries. A policing member note, a policing alert, a `pickup` auto-shelve and a crontab sweep are each recorded as `automation`, and a dispatch by hand from the CLI as `admin`. Every admin email carries a working incident link, quoted, and a multi-item email finds all its items. Every `portal_events` row since the cursor is in the store, including a help request and a delivered alert, which the first draft's tables would have lost. Each of the three CI checks fails on a fixture that lacks its step or line and passes on the tree. Every self-hosted workflow, `resume-setup` and `migrate-volume` included, leaves a facts artifact, and a `/create` on a workshop issue (dispatched by `create-workshop` from `ubuntu-latest`) is joined to its parent run. A failed create's facts carry the image id, the server id and the `limits show` snapshot. Time to 👍/👎 and lock time for each command type, measured with and without collection on Test: no measurable change in time to 👍/👎 on success paths, extra lock time within the table in "What it costs member operations". A gap found in testing is fixed as a class, before merge. |
| 2     | **Baseline: recording only, no agent.** Every admin email opens an `incident:<id>` issue for the verdict; implicit labels; weekly base rates by plain SQL in one email. Signatures refined by hand as `unclassified` cases are labeled.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 8 weeks of full recording counted from the 2026-11-23 merge (no earlier than 2027-01-18) **and** at least 100 incidents with a verdict other than `test-induced` (decision 16); every harvester stop longer than two hours followed by a watchdog email at its next delivery (within about 6 hours); no watchdog email on a quiet night with a healthy harvester; no crontab-, policing- or `pickup`-dispatched run counted as `admin`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 3     | Analysis agent, first run over the whole baseline: base rates, themes, rule candidates as issues. It reads the history; it decides no case. Weekly afterwards.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | A report the maintainer has reviewed; the list of signatures that are still novel.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| 4     | **Backtest:** the triage agent replayed over the baseline, each case file rebuilt as of the incident's time, graded against the verdicts. Nothing is sent.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Zero missed pages on non-novel signatures; page precision ≥ 80%; every disagreement read by the maintainer and either the prompt or the verdict fixed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 5     | Triage agent live in shadow: decisions recorded, today's emails unchanged; novel cases always paged (decision 17).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 3 weeks: zero missed pages; page precision ≥ 80%; `unclassified` < 10%.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 6     | Email switch: the maintainer's address gets pages and the digest; `notify-admins`' direct emails go to an alerts mailbox as the audit trail.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 2 weeks, zero missed pages.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 7     | Audit classes 2 and 3 fixed (prerequisite); `incident-act.yml`; first playbook `rerun-github-5xx` through shadow → confirm → act.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Promotion criteria met; a week report-only on production.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 8     | Further playbooks, one at a time, each from an approved analysis proposal on a non-novel signature.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Per playbook.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 9     | Courses (`MC-*`), once they have a clock.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | One course cycle.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |

## How we know it works

- **Missed pages** (a verdict `missed:` or `should-have-acted` on a digest
  item): must be zero. One resets the playbook's or the signature's counter.
- **Backtest agreement** (Phase 4): the agent's replayed calls against the
  baseline verdicts, per signature. It is the first number, and it is known
  before anything goes live.
- **Novel share**: incidents on novel signatures, per week. It should fall as
  the history grows; a jump is a new failure type and worth a look on its own.
- **Page precision**: pages the maintainer marked `needed-me`.
- **Hands-off share**: incidents resolved with no maintainer action, per week.
- **Time to resolution** per signature, before and after a rule or playbook
  lands.
- **Maintainer load**: pages per week.
- **Themes closed**: rule candidates built, and their signature's rate after.

## Parameters to confirm

- Store name `MorphoCloud/Incidents` (production) and
  `MorphoCloud/Incidents-sandbox` (Phase 1 testing only).
- Harvester credential. **Decided 2026-11-08** (delegated to the implementer): a
  fine-grained PAT, one year, watched by `check-dispatch-tokens.sh` like the
  dispatcher's. The app's private key would put a credential that can act on
  every repository onto the runner host.
- Whether Jetstream2 allows an application credential restricted to read calls
  (decision 18); if not, the wrapper and the recorded risk.
- Backfill sample. **Decided 2026-11-08** (delegated): about 40 incidents from
  the top classes get `needs-verdict`, so those signatures clear the novelty
  threshold on day one.
- Standing verdicts. **Decided 2026-11-08** (delegated): yes, one per signature,
  starting with the ~60 status-label GitHub errors a month, so only exceptions
  need a label.
- The agent runner: a separate user on the Test runner host, or a small
  dedicated VM.
- Episode threshold (3 instances / 30 min); repeat window 24 h; stale window 12
  h; triage cap 40 a day.
- Digest at 08:00 and 17:00 Pacific.
- Baseline size: 8 weeks and 100 verdicts (decision 16). Novelty: fewer than 10
  verdicts or under 90% agreement among them (decision 17).
- Promotion thresholds (20 decisions / 19 agreeing; 20 clean approved runs).
- Whether `member.help_request` pages immediately (recommended) or goes to the
  digest outside working hours.

## Out of scope

- The agent writing code or opening pull requests. Rule candidates are issues,
  designed and reviewed like any decision.
- Any action on shares, data, approval, expiry, or delete, and any free text to
  members.
- A dashboard. DuckDB and the weekly report cover operators; the portal is for
  members.
- Reading the mailbox. Every email the system sends already has a GitHub-side
  event, and the store is fed from those.
