# Incident Store and Operations Agent — Design & Plan

**Goal.** An autonomous operations agent for MorphoCloud that, for every
failure, decides correctly whether to fix it on its own or contact the
maintainer, and that finds the recurring themes across failures and proposes the
rule that would end each one. This plan builds the record that agent learns
from, the agent itself, and the gate that lets it act.

**Status (2026-11-08):** proposed, nothing approved, nothing built. Built on
`share-prototype` (c9711a2), which reaches production with the 2026-11-23
migration. Supersedes the `main`-based draft on `ccr-028ab0f6-ximhmd`.

**Under review now: data collection only.** That is decisions 1–6, 18 and 19,
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
   logs included (gzipped): about 190 MB a year (see "Volume"), so no object
   store is needed. DuckDB over a checkout is the query engine.

2. **One writer, pulling.** Jobs never write to the store. They write their
   facts into a run artifact. **One harvester**, on production's acquire runner,
   reads every completed Instances run through the Actions API, along with the
   runner host's logs and, after the migration, the portal's events. It is the
   store's only writer, so nothing races. Nothing is added to the per-issue
   lock, and no workflow holds a store credential. Every run is recorded even
   when its job died before its last step. Each admin-email sender records the
   subject and body in its facts where it already sends (B10). No sender is
   moved. Consolidating the senders onto `notify-admins` changes production
   alerting and is a separate PR, outside this plan.

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
   Each entry is captured, deliberately excluded with a reason, or marked as a
   blind spot. A new workflow, log file or table is added to it in the same PR.
   When a postmortem finds it needed something that was not captured, the
   inventory gets the entry first.

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

## Architecture

```
 Instances (jobs)                             production acquire runner
 ┌─────────────────────────────────┐         ┌──────────────────────────────────┐
 │ facts.sh: redact, then encrypt  │         │ crontab (decision 21)            │
 │   → mc-facts-<job>-<attempt>    │         │  harvest-incidents.py */5, -w */1│
 │ (no store credential, no push)  │◀── API ─│   1 runs + jobs + facts + logs   │
 └─────────────────────────────────┘  read   │   2 runner logs (dispatch, fip,  │
                                             │     pickup, runner _diag)        │
 data portal VM                              │   3 portal events (SSH gate)     │
 ┌─────────────────────────────────┐         │   4 late OpenStack snapshot      │
 │ export-events --since (gate)    │◀── SSH ─│   5 classify, redact, write      │
 │ reachability_events (new)       │         └───────────────┬──────────────────┘
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
`timeout-minutes: 1`. `report-command-outcome` does it in the 6 command
workflows that call it (control-instance, create-instance,
create-course-instance, delete-instance-and-volume, extend-session, send-email)
and in `report-dropped-command`, **after** it has set the 👍/👎. One added final
step does it in the sweeps and alerts. Every `run-name:` gains the issue number:
`#${{ github.event.issue.number || inputs.issue_number }} ${{ github.workflow }}`.

### Where it runs

| Where                    | What runs there                                                                                                                                                               | Machine                                                                                                                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| inside the workflow jobs | the facts (B), the setup log (C), the pre-shelve capture (D): local file writes and one artifact upload                                                                       | the runner each job already uses: the self-hosted control runner for instance work, `ubuntu-latest` for the small jobs (authorize, `admin-alert`, `report-dropped-command`). Nothing new is scheduled. |
| **the harvester**        | everything else: reads the Actions API, classifies, redacts, takes the late snapshot, reads the runner logs, pulls the portal events, reads outside context, writes the store | **production's acquire runner**, where the dispatcher lives, from its crontab                                                                                                                          |
| the portal VM            | only the read-only gate command `export-events`, and the new `reachability_events` table written as each state report is stored                                               | the portal VM, reached over the SSH gate the runner's `pickup` already uses                                                                                                                            |

The runner host and not the portal VM because:

- the runner host already has what the harvester needs: OpenStack access for its
  allocation (not read-only; see decision 18), the logs it reads (F), the
  crontab clock (decision 21) and the SSH path to the portal;
- the portal VM is member-facing and by design holds no GitHub credential
  (decision 20), so a write credential and a batch job there would widen what
  the web app exposes;
- production has a runner now and no portal VM until the migration.

New on the runner host: two crontab lines, the script, a fine-grained token and
two keys (see "The harvester"). Production runs two runners. The production
harvester reads the second runner's `_diag` logs (F5) over SSH, and installs
nothing there.

### Production only, and before the merge

Production runs `main` plus hotfixes until the 2026-11-23 merge. `record-facts`,
the crontab renderer (`check-workflow-clock.py`) and `admin-alert.yml` exist
only on `share-prototype`. So collection on production comes in two steps:

| Production                      | Recorded                                                                                            | How the harvester is installed                                                                                                                                                                                                                                                                                                                                                                                                                     | Alerts                                                                                                                                                              |
| ------------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **before the merge**            | what needs no workflow change: A (runs, jobs, logs), E, F (runner logs), H, and the 90-day backfill | **by hand**: the script copied to the acquire runner and its two crontab lines added outside the dispatcher's managed block, the way the Test runner's portal `pickup` line is kept. `restore-instances-dispatcher.sh` is never run from `share-prototype` against production before the merge: it would render production's crontab from the prototype's workflows and schedule workflows `main` does not have (follow-through, expire-shares, …) | the store watchdog (see "When collection fails"); token expiry through `check-dispatch-tokens.sh`, which already alerts through Analytics' `token-expiry-alert.yml` |
| **from the merge (2026-11-23)** | everything: the facts (B, C, D) and, with the migration, the portal (G)                             | the harvester lines move into the rendered crontab (`RUNNER_SCRIPTS`) and the hand-added lines are removed in the same step                                                                                                                                                                                                                                                                                                                        | the same                                                                                                                                                            |

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
2. **What can be read later is read by the harvester, not the job.** On
   successes, capacity at the time comes from Jetstream2's Grafana vacancy
   history; the labels at the time come from the issue's timeline events.
   Jetstream2 outages come from the ACCESS outages feed. GitHub incidents come
   from githubstatus.com's incident history. On failures, `control-instance`
   keeps the capacity read it already does.
3. **Failure captures (C3, C4) come after the member's failure comment,** under
   one 30-second cap, so a failure is reported as fast as today.
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

Built from the code on `share-prototype` and the portal's `share-prototype`
(every `openstack` read, every SSH read, every log file a crontab writes, every
portal table), not from memory. **Lost** says when the evidence is gone if it is
not taken. ✔ = captured by this plan, ✘ = deliberately not, with the reason.

**A. The GitHub run** (harvester)

| #   | Evidence                                                                                                    | Lost                       | When                                                             |
| --- | ----------------------------------------------------------------------------------------------------------- | -------------------------- | ---------------------------------------------------------------- |
| A1  | run envelope: workflow, event, actor, triggering actor, attempt, created/started/updated, conclusion, issue | never (until runs deleted) | ✔ every run (no-op sweep ticks and skipped runs as daily counts) |
| A2  | jobs: runner name, start/end, every step's name and conclusion, the first failed step                       | never                      | ✔ every command run; sweeps when they failed or acted            |
| A3  | full job log, redacted, gzipped (~32 KB), and the failed step's last 200 lines                              | 90 days                    | ✔ every incident; ✔ every create (setup timing baseline)         |
| A4  | the facts artifact                                                                                          | 7 days (encrypted)         | ✔ every run that has one                                         |

**B. What the job knows** (facts; lost when the job ends)

| #   | Evidence                                                                                                                                                                                                                                                                                                                                                                                       | When                                          |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| B1  | `command` as typed, `issue`, `instance`, `flavor`, `dispatch_reason` (`member`, `timeout`, `unreachable`, `retry:<cmd>`, `repair:no-address`), `workflow_version`                                                                                                                                                                                                                              | ✔ every command                               |
| B2  | Nova `status` and `task_state` before and after; `failure_mode`; the failure class `notify-admins` uses; the failed Nova step (`schedule_instances` / `compute_unshelve_instance`)                                                                                                                                                                                                             | ✔ every command                               |
| B3  | **`hostId`**, Nova's per-project hash of the compute host. Today read only inside the failure branch of `create-instance` and `control-instance` and thrown away. With it on successes too, failure rate per host becomes a query (the sick-host pattern behind the g3 unshelve failures) Nova hashes the host together with the project, so ids compare only within the production allocation | ✔ every create and unshelve, success included |
| B4  | address outcome: taken, attached, released, shelved again (decision 25)                                                                                                                                                                                                                                                                                                                        | ✔ every create and unshelve                   |
| B5  | stage timestamps: requested, BUILD done, ACTIVE, address bound, SSH up, setup complete, share mounted, reboot verified, email sent                                                                                                                                                                                                                                                             | ✔ every create, unshelve, resume              |
| B6  | capacity for the flavor at the time: on failures, the `morphocloud.org/api/availability` read `control-instance` already does; on successes, the harvester reads Jetstream2's Grafana vacancy history afterwards (no call in the job)                                                                                                                                                          | ✔ every create and unshelve                   |
| B7  | labels on the issue at the time (`followup:*`, `followup-count:*`, `needs-admin`, `invariant-violation`, `admin-email-failed`, `status:*`): reconstructed by the harvester from the issue's timeline events (no call in the job)                                                                                                                                                               | ✔ every command                               |
| B8  | refusal reason (not approved, does not exist, in flight, maintenance, unauthorized), written before the deliberate `exit 1`                                                                                                                                                                                                                                                                    | ✔ every refusal                               |
| B9  | `collect-instance-diagnostics`: Nova fault and recent actions with each step's result and host                                                                                                                                                                                                                                                                                                 | ✔ when the workflow runs it                   |
| B10 | the admin email: subject, class, body, recorded by `record-facts` at each existing sender: `notify-admins`, `admin-alert.yml`, and the direct senders in `follow-through`, `reconcile-instances`, `delete-instance-and-volume`, `expire-shares`, `guard-issue-close`, `detect-dormant-workflows`, `on-admin-mention`                                                                           | ✔ every admin email                           |
| B11 | sweeps: what each tick did (`acted=[{item, issue, action, reason}]`); reconcile's violations and the dormant detector's findings, one item each; follow-through's attempts                                                                                                                                                                                                                     | ✔ every tick that acted or failed             |
| B12 | runner host at job time: free disk, load, uptime (`df`, `/proc/loadavg`)                                                                                                                                                                                                                                                                                                                       | ✔ every command on a self-hosted runner       |

**C. The instance's setup log** (lost when the server is shelved, deleted or
recreated, so it is taken during the run, never later)

| #   | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                         | When                                              |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| C1  | **the full console log** (cloud-init and ansible output) that `setup-instance` already reads every poll as `$log_output` and today discards after extracting one line. Nova returns only the tail, so each poll's read is merged into a growing copy. Each merge records whether it found an overlap with the copy; where it did not, a `[gap]` marker goes into the copy and `console_complete: false` into the facts, so a gap never looks like a complete log | ✔ every create and resume-setup, success included |
| C2  | the console at follow-through's hand-over (resume, error, or the stall/ceiling shelve of decision 33): follow-through reads it every tick and discards it today                                                                                                                                                                                                                                                                                                  | ✔ every hand-over                                 |
| C3  | `/var/log/cloud-init-output.log` and `/var/log/cloud-init.log` from the instance, over the SSH session `setup-instance` already has                                                                                                                                                                                                                                                                                                                              | ✔ setup failure after SSH is up                   |
| C4  | `journalctl -b` for the cloud-init units, last 2,000 lines                                                                                                                                                                                                                                                                                                                                                                                                       | ✔ same                                            |
| C5  | derived: status, reason, last ansible task, failed item (computed today), time to first console line, time to `complete`, tasks seen → signature `setup.error:<task>`, `setup.stalled:<task>`, `setup.timeout:<task>`                                                                                                                                                                                                                                            | ✔ every create                                    |

**D. An unreachable or address-less instance** (lost at the shelve that fixes
it)

| #   | Evidence                                                                                                                                                                                                                                                                                          | When                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | console log, `port show` for the server's port (status, fixed IP), the floating IP's status, `server event list`, the last SSH error text. Today auto-shelve (decisions 23 and 26) reads the port and the address, fails SSH, and shelves: the network-orphan evidence is destroyed by the repair | ✔ in `control-instance` when `dispatch_reason` is `unreachable`, before it shelves; in `automatic-instance-shelving` before it dispatches |
| D2  | the same for an unshelve or create that ends ACTIVE without an address or unreachable (decision 25's shelve-again path, inside `control-instance`) and before decision 34's repair (inside `reconcile-instances`, before it dispatches)                                                           | ✔ before the shelve-again or the repair dispatch                                                                                          |

**E. Outside context at the moment** (read afterwards by the harvester; these
sources keep their history)

| #   | Evidence                                                                                                                                                                                                                                            | When                                                    |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| E1  | Jetstream2 availability for the flavor                                                                                                                                                                                                              | ✔ (B6)                                                  |
| E2  | Jetstream2 outages: the ACCESS outages feed `check-js2-status` reads, including its past outages, read by the harvester. How far back the past listing reaches is to be confirmed; outside its range the record says `unknown`, never `operational` | ✔ every incident on an OpenStack step                   |
| E3  | GitHub incidents from githubstatus.com, read by the harvester; they tell a GitHub-wide outage from our bug. The public feed lists only recent incidents, so backfilled incidents outside its range get `unknown`                                    | ✔ every incident whose error line is a GitHub API error |
| E4  | Jetstream2 Grafana vacancy history                                                                                                                                                                                                                  | ✔ the source of B6 on successes                         |

**F. The runner host** (the harvester runs there and reads these in place, from
its last offset)

| #   | Evidence                                                                                                                                         | When                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| F1  | `~/morphocloud-dispatch.log`: **a crontab dispatch that failed never becomes a run**, so this is the only record that a sweep did not fire       | ✔ every failed dispatch; daily count |
| F2  | `~/fip-police.log`: mints, releases (with issue), droughts, non-routable addresses                                                               | ✔ every action                       |
| F3  | the portal runner's `pickup.log` (Test now, production after the migration): uncaught SSH timeouts, lost claims, failed restores (audit class 8) | ✔ every error line                   |
| F4  | `~/token-expiry.log`                                                                                                                             | ✔ every warning                      |
| F5  | the Actions runner service's `_diag` logs (runner crashes, lost connections), on runner-1 in place and on production's second runner over SSH    | ✔ error lines                        |
| F6  | the late OpenStack snapshot of an incident's instance (`server show`, `event list`, port, floating IP), stamped with its age                     | ✔ every incident                     |

**G. The portal: what the member experienced** (new read-only gate command
`export-events --since <cursor>` in the portal repo, pulled over the same SSH
gate the runner already uses; Test now, production after the 2026-11-23
migration)

| #   | Evidence                                                                                                                                                                                  | When                                                |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| G1  | `instance_commands`: every button press, posted and finished, ok or failed. The member-side denominator, including presses that never produced a run                                      | ✔ every row                                         |
| G2  | `instance_requests`: create requests, including ones stuck in `posting` or `failed`                                                                                                       | ✔ every row                                         |
| G3  | `failures`: member-visible failures the web app recorded                                                                                                                                  | ✔ every row                                         |
| G4  | `auto_shelves`: each unreachable episode (since, requested, dispatched, error)                                                                                                            | ✔ every row                                         |
| G5  | **reachability history, kept nowhere today**: `instance_states` holds only the latest snapshot. New table `reachability_events` (issue, from, to, flavor, hostId), one row per transition | ✔ every transition                                  |
| G6  | `alerts`, `alert_outbox`, `alert_digest`: what the portal told the admins                                                                                                                 | ✔ every row                                         |
| G7  | `chatbot_escalations`: a member asking for help. **Deleted after 7 days** (CHATBOT_PLAN decision 9), so harvested within that window                                                      | ✔ every row, body redacted                          |
| G8  | `audit`: resets, keeps, recreates                                                                                                                                                         | ✔ every row                                         |
| G9  | `connections`                                                                                                                                                                             | ✘ holds the passphrase and the web link; never read |

**H. The issue, afterwards** (harvester, for 24 h after an incident)

| #   | Evidence                                                                                                                                    | When             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| H1  | the timeline: comments (text, redacted), labels added and removed, reactions. A member's "still can't connect" is evidence                  | ✔ every incident |
| H2  | what happened next: the member's next command and its result, any admin-dispatched run (the maintainer's hand action), any automatic action | ✔ every incident |

**Blind spots, stated:**

- A command GitHub never queued (an outage). The portal's `instance_commands`
  (G1) now records the press, so it shows on production after the migration.
- An instance that is reachable and set up but broken for the member (desktop,
  Slicer, the share). It shows only as a help request (G7, `on-admin-mention`).
- Course repositories (`MC-*`): no harvester until they have a runner clock.
- Runs older than 90 days: envelope only, no log. Backfilled runs never have
  facts (B, C, D).
- A rerun of a run created more than 48 hours earlier that queues and finishes
  between two one-minute watch passes (see "The harvester", step 1).

### The harvester

`scripts/harvest-incidents.py`, on production's acquire runner, where the
dispatcher lives. It has two crontab lines: the full cycle every 5 minutes and a
light `--watch` pass every minute.

- **Before the merge** they are added by hand (see "Production only, and before
  the merge").
- **From the merge** they are runner-side scripts in
  `scripts/check-workflow-clock.py`'s `RUNNER_SCRIPTS`, next to the floating-IP
  policing and not in the `.extra` file, templated per target.

The Test runner gets the same lines writing to `Incidents-sandbox`, but only
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
   miss runs that queued or ran long, and every rerun of an older run. Three
   mechanisms cover it:
   - **New runs:** every cycle lists the runs created since the last cycle, with
     a 15-minute overlap, in narrow windows (the API returns at most 1,000 runs
     per filtered query, and Instances produces about 1,100 a day).
   - **Lookback window:** once an hour, the runs created in the last 48 hours
     are re-listed and each run's `status`, `run_attempt` and `updated_at`
     compared with what is recorded. Hourly rather than every cycle: with the
     watch list and the watch pass in place, it only catches very fast reruns of
     recent runs, and every 5 minutes it would pull about 2,200 run records each
     time.
   - **Watch list:** every run seen queued or in progress is kept and checked
     every cycle until it completes, whatever its age.
   - **Watch pass:** every minute, `--watch` lists the runs that are `queued` or
     `in_progress` with no creation filter (two calls), so a rerun of a run of
     any age joins the watch list. A new `run_attempt` on a recorded run is a
     new outcome line, and a new incident when it fails.

   The rerun playbook planned for the agent records each run it reruns. The
   residual blind spot is stated under "Evidence inventory".

2. **Jobs and facts.** For each command, alert, failed or acting run: fetch the
   jobs and the `mc-facts-*` artifacts.
3. **Classify** (deterministic): `result` is `skipped`, `dropped`, `cancelled`,
   `refused` (facts, or a failed step on the refusal list), `failure`,
   `success-with-incident` (an admin email or a violation in the facts) or
   `success`. The signature comes from the facts first, else from the failed
   step's error line against `incident-signatures.yml` (a GitHub 5xx only on the
   error line, never by grepping the whole log), else `unclassified`.
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
5. **Runner logs** (F1–F5) and **portal events** (G1–G8), each from its own
   cursor.
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
portal/YYYY/MM/DD/<table>.jsonl    G1–G8
runner/YYYY/MM/DD/<host>.jsonl     F1–F5
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
    "actor_kind": "member | admin | automation",
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
  cycle, lag, last error. The **store watchdog**, `watchdog.yml` in
  `MorphoCloud/Incidents`, checks it and the newest outcome line, and emails the
  admins once per episode when either is more than two hours old or the last
  cycle ended in an error.
  - It runs on GitHub, not on the runner, so it still fires when the runner host
    is down.
  - It runs on `on: schedule`, hourly, with `workflow_dispatch`. This is a
    deliberate exception to decision 21: it must not depend on the runner, and
    GitHub's 3–5 deliveries a day are enough for a check measured in hours
    against a 7-day retention.
  - It is about 150 billed minutes a month (a private repository on
    `ubuntu-latest`).
  - The mail credentials are the store's own secrets.
  - The only silent case left is GitHub's scheduler and the runner failing
    together.
- **The portal export fails** (from the migration): its cursor stays put. The
  tables keep their rows, except `chatbot_escalations` (7 days), so the watchdog
  also checks the newest portal record, at one day.

### Volume

Measured on Instances over 2026-10-09 to 11-07: about 1,100 runs a day, about
300 command runs and about 125 incidents a month (45 failed commands, 79 failed
sweeps and other runs, 62 of them `update-request-status-label`).

| Production, per month                           | Size       |
| ----------------------------------------------- | ---------- |
| outcome lines (~400) and rollups                | ~0.4 MB    |
| incidents (~125 at ~70 KB with the job log)     | ~9 MB      |
| setup records for every create (~40 at ~150 KB) | ~6 MB      |
| portal and runner events                        | ~1 MB      |
| **total**                                       | **~16 MB** |

About 190 MB a year, under GitHub's 1 GB guidance for years. The 90-day backfill
adds about 27 MB once. `check-added-large-files` at 2 MB in the store's
pre-commit.

## Signatures

`incident-signatures.yml` lives in MorphoCloudWorkflow and is vendorized. It is
a controlled vocabulary: the collector assigns from it, the agent may propose
additions in its decision record, and only the maintainer adds them. Seeded from
what the prototype already distinguishes:

| Family     | Signatures                                                                                                                       | Assigned by                                   |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `create`   | `no_capacity`, `host_failure`, `no_address`, `api_error`, `timeout`                                                              | notify-admins class + failure_mode            |
| `unshelve` | `no_capacity` (dec. 30), `host_failure`, `no_address`, `address_not_attached`, `wrong_end_state`, `timeout`                      | notify-admins class + Nova failed step        |
| `shelve`   | `wrong_end_state`, `timeout`, `api_error`                                                                                        | notify-admins class                           |
| `setup`    | `error:<task>`, `stalled:<task>`, `timeout:<task>`, `ceiling`, `ssh_auth` (dec. 33; task from C5)                                | resume-setup / follow-through                 |
| `followup` | `retries_spent`, `server_error` (dec. 28)                                                                                        | follow-through                                |
| `drift`    | `active_without_address`, `repair_failed` (dec. 34), `stuck_shelved`, `error_state`, `address_on_shelved`                        | reconcile                                     |
| `network`  | `unreachable_shelved`, `could_not_shelve` (dec. 23), `non_routable_address`, `address_drought`, `address_drought_over` (dec. 24) | admin-alert subject                           |
| `email`    | `credentials_failed`, `admin_send_failed`                                                                                        | notify-admins / admin-email-failed            |
| `github`   | `api_5xx` (the top failed-run cause, #317), `command_dropped`                                                                    | error line of the failed step; report-dropped |
| `share`    | `expire_failed`, `mount_failed`                                                                                                  | expire-shares; setup                          |
| `member`   | `help_request`                                                                                                                   | on-admin-mention                              |
| `monitor`  | `dormant_workflow`                                                                                                               | detect-dormant-workflows (digest only)        |
| —          | `unclassified`                                                                                                                   | fallback                                      |

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

`verdicts/<id>.json` is written by a store workflow from these. During the
baseline (Phase 2) there is no agent and no page: every admin email the system
already sends opens an `incident:<id>` issue in the store, and the email links
to it. Closing it with one label (`needed-me`, `could-have-waited`,
`nothing-to-do`, or `test-induced` for a deliberate test the test-account mark
did not catch, plus `/did <text>` when something was done by hand) is the whole
cost to the maintainer. `test-induced` incidents, and every run marked
`actor_kind: test`, stay in the store but are left out of the base rates and of
what the agent is graded on. The implicit labels fill in what was done. This is
the record the agent is graded against before it is trusted with anything.

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

- **Git**: everything, about 16 MB a month on production (see "Volume"). Kept
  indefinitely; if the store ever nears 1 GB, logs older than two years move to
  a separate archive repository.
- **Actions minutes**: none. The harvester is a crontab script, agents run on
  self-hosted runners, and the facts are written inside jobs that already run.
- **Model usage**: `CLAUDE_CODE_OAUTH_TOKEN` (subscription). Triage is capped at
  one run per episode and at most 40 a day; above that, triage pauses and one
  page says so. Analysis runs weekly.

## Phases

Each phase is one feature: design approved, built whole, one review, tested on
Test as amm554 from an approved test plan, then production.

| Phase | Scope                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Exit                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | **Data collection, one feature.** Design note first (the evidence inventory plus what each capture adds inside the per-issue lock: the pre-shelve capture D1/D2, the SSH reads C3–C4), approved by the maintainer. Then built whole: store repos, harvester with its PAT, its OpenStack credential or wrapper (decision 18) and its decryption and HMAC keys, `record-facts` (redaction and encryption in the job) and every facts site (A–H), signatures, the version file, and the portal's `export-events` and `reachability_events`. Moving the admin-email senders onto `notify-admins` is **not** part of this phase. Adding the incident link to the body of every admin email (B10's nine senders: `notify-admins`, `admin-alert.yml`, `follow-through`, `reconcile-instances`, `delete-instance-and-volume`, `expire-shares`, `guard-issue-close`, `detect-dormant-workflows`, `on-admin-mention`) **is**: a one-line body edit at each, where it already sends. One review; one PR in MorphoCloudWorkflow and one in the portal repository. Built and tested whole on Test-Instances as amm554, writing to `Incidents-sandbox`, from a test plan approved first. **Then production, in two steps** (see "Production only, and before the merge"). **Before the merge**, with the maintainer's go-ahead for that production change: the harvester is installed by hand on the acquire runner, records A, E, F and H, and runs the 90-day backfill of Instances; the store watchdog and the `check-dispatch-tokens.sh` entry go live. **At the 2026-11-23 merge**: the facts (B, C, D), the keys, the rendered harvester lines replacing the hand-added ones, and with the migration the portal (G). | Every provoked failure (impossible flavor, setup forced to fail, setup stalled, unreachable instance, GitHub error by a bad token) and a normal create, unshelve and shelve, checked line by line against the inventory. Every run in the Actions UI is in the store as a row or a count, including a long-queued run, a rerun of a run more than 48 hours old, and a rerun's facts (attempt 2's artifact uploaded beside attempt 1's). A downloaded facts artifact is unreadable without the key, and its decrypted content passes the redaction test. With the HMAC secret or the certificate removed, no address and no artifact leave the job. The rendered production crontab carries the harvester lines and the hand-added ones are gone; the restore script installs the keys and the PAT from the `~/.ssh` backups, and with a key missing it still restores the clock, warns, and the watchdog reports the harvester stopped. Stopping the harvester's cron line makes the watchdog email within its next deliveries. Every admin email carries a working incident link, quoted, and a multi-item email finds all its items. Every portal row since the cursor is in the store. Time to 👍/👎 and lock time for each command type, measured with and without collection on Test: no measurable change in time to 👍/👎 on success paths, extra lock time within the table in "What it costs member operations". A gap found in testing is fixed as a class, before merge. |
| 2     | **Baseline: recording only, no agent.** Every admin email opens an `incident:<id>` issue for the verdict; implicit labels; weekly base rates by plain SQL in one email. Signatures refined by hand as `unclassified` cases are labeled.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 8 weeks of full recording counted from the 2026-11-23 merge (no earlier than 2027-01-18) **and** at least 100 incidents with a verdict other than `test-induced` (decision 16); harvester lag never over an hour unannounced.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 3     | Analysis agent, first run over the whole baseline: base rates, themes, rule candidates as issues. It reads the history; it decides no case. Weekly afterwards.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | A report the maintainer has reviewed; the list of signatures that are still novel.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 4     | **Backtest:** the triage agent replayed over the baseline, each case file rebuilt as of the incident's time, graded against the verdicts. Nothing is sent.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Zero missed pages on non-novel signatures; page precision ≥ 80%; every disagreement read by the maintainer and either the prompt or the verdict fixed.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 5     | Triage agent live in shadow: decisions recorded, today's emails unchanged; novel cases always paged (decision 17).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 3 weeks: zero missed pages; page precision ≥ 80%; `unclassified` < 10%.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 6     | Email switch: the maintainer's address gets pages and the digest; `notify-admins`' direct emails go to an alerts mailbox as the audit trail.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | 2 weeks, zero missed pages.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 7     | Audit classes 2 and 3 fixed (prerequisite); `incident-act.yml`; first playbook `rerun-github-5xx` through shadow → confirm → act.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Promotion criteria met; a week report-only on production.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| 8     | Further playbooks, one at a time, each from an approved analysis proposal on a non-novel signature.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Per playbook.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 9     | Courses (`MC-*`), once they have a clock.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | One course cycle.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

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
- Harvester credential: a PAT whose expiry the harvester watches itself, or the
  app's private key on the runners.
- Whether Jetstream2 allows an application credential restricted to read calls
  (decision 18); if not, the wrapper and the recorded risk.
- Whether a sample of the 90-day backfill gets `needs-verdict` (about 40 from
  the top classes puts them past the novelty threshold on day one).
- A standing verdict per signature for clearly harmless, high-volume classes
  (the ~60 status-label GitHub errors a month), so only exceptions need a label.
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
