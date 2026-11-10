# Fast failure when Jetstream2 has no host (design, 2026-11-10)

Status: design for review. Prototype (share-prototype, Test-Instances) only;
production keeps running as it is until the migration (Nov 23–29) and gets this
with the migration vendorize.

## The rule

**Nova's answer to the request is the only judge of whether an instance can be
had.** The vacancy numbers Jetstream2 publishes are not, in either direction:

- Test-Instances #539, 2026-11-10 17:24Z: the Grafana panel showed 2 free g3.xl;
  Nova answered "No valid host was found" in 3 s.
- Instances #257, 2026-11-10 19:32Z: the panel showed 0 g3.large at every minute
  from 19:00 to 19:53 (1-minute resolution, 5-minute refresh); Nova placed the
  g3.large in 21 s. At 19:53 ten more g3.large requests from Exosphere all got
  "No valid host was found" within seconds.

So nothing here gates an attempt on vacancy. Failing fast means: reach Nova
sooner, notice its answer as soon as it is given, and tell the member before
anything else.

## Where the time goes today

Exosphere calls Nova directly and shows its answer, so a request with no host
fails in seconds. Ours does the same Nova call inside a workflow.

**#539, create, no host (Test-Instances run 42864454776), measured from the
log:**

| Z        | step                                                                                   | elapsed |
| -------- | -------------------------------------------------------------------------------------- | ------: |
| 17:23:24 | member comments `/create`                                                              |     0 s |
| 17:23:45 | GitHub starts the run, `authorize` job (GitHub-hosted), hand-off to the runner         |    21 s |
| 17:23:49 | job setup, checkout, token, quota and JS2 checks                                       |    25 s |
| 17:24:05 | share check, instance-exists check, labels, spare address, **three progress comments** |    41 s |
| 17:24:10 | **Nova: create → ERROR "No valid host was found"**                                     |    46 s |
| 17:24:16 | capacity check (1.5 s), **diagnostics (4.3 s)**                                        |    52 s |
| 17:24:17 | **the member sees "No instance of type g3.xl is available"**                           |    53 s |

Nova's part: about 5 s of 53.

**#538, unshelve, no host (Test-Instances run 42853462672):**

| Z        | step                                                                                                                                                                                                     | elapsed |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------: |
| 16:20:02 | member comments `/unshelve`                                                                                                                                                                              |     0 s |
| 16:20:13 | `authorize` done                                                                                                                                                                                         |    11 s |
| 16:20:56 | **the `control` job waits 43 s for the runner**: the runner clock's 16:20 tick started four sweeps on the one Test runner                                                                                |    54 s |
| 16:21:22 | "Take over any pending follow-up" (5 s), then six pre-checks, five of them a separate `openstack server show` (about 1.5 s each, mostly client start-up), one an `event list`; then the unshelve request |    80 s |
| 16:21:28 | "Record activity on the data share" (6.8 s) **before polling starts**                                                                                                                                    |    86 s |
| 16:22:13 | **poll notices the failure**: Nova's events are read only every 30 counted seconds (each loop is about 6.5 s real), although `schedule_instances` failed within a second or two of the request           |   131 s |
| 16:22:21 | diagnostics (7.6 s)                                                                                                                                                                                      |   139 s |
| 16:22:23 | **the member sees the no-capacity comment**                                                                                                                                                              |   141 s |

Nova's part: about 2 s of 141.

**Across the stores (every member `/create` and `/unshelve` recorded, Sep–Nov),
the wait between `authorize` finishing and the work job starting:**

| store      | command     | outcome  |   n | wait p50 | wait p90 |
| ---------- | ----------- | -------- | --: | -------: | -------: |
| Test       | `/create`   | capacity |  20 |      2 s |    877 s |
| Test       | `/unshelve` | capacity |  22 |     81 s |  1,924 s |
| Test       | `/unshelve` | success  |  69 |      2 s |    103 s |
| production | `/create`   | capacity |   6 |     95 s |    117 s |
| production | `/unshelve` | success  | 223 |      2 s |      2 s |

The wait is longest exactly when capacity is short: failing creates queue behind
each other in the acquire lane (Instances, 2026-11-02 22:36: five g3.xl
`/create`s in a row, each waiting for the one before to fail). A faster failure
also shortens that queue. (Production's unshelve capacity p90 of 7,481 s is the
Nov 9 runner outage, not this.)

## Changes

Each keeps every message, error line and comment the member and the incident
store see today; only the order and the waiting change.

### A. Create: call Nova right after the checks that can refuse

Order in `.github/actions/create-instance/action.yml`:

1. Unchanged, before Nova: the owner's data share, instance already exists,
   `Extract labels` (the flavor), and **`fip.py ensure-spare`**. The spare
   address check stays before Nova on purpose (AUTOMATION_AUDIT_2026-11, class
   1): a dry pool must fail before a server is built. In volume mode the volume
   check and create also stay before Nova (the volume count quota must fail
   before a server is built); share mode has no volume.
2. Moved after the Nova call:
   `Add request:approved label and remove stale labels`, and the progress
   comment. The progress comment is created once, right after
   `openstack server create` returns, with Create IP and Create Volume already
   ✅ and Create Instance ⏳, instead of being created and edited three times
   before Nova is asked. The later edits stay as they are.

Saves about 11 s in share mode (#539: Nova asked at about 30 s instead of 41 s).

### B. Unshelve: read the server once before the request, and start polling at once

In `.github/actions/control-instance/action.yml`:

1. The pre-checks that each run `openstack server show` (instance exists,
   already in the requested state, corrupt shelved state, has a floating IP)
   read one `server show` taken after "Wait for in-flight server action to
   settle", saved to a file in `$RUNNER_TEMP`. That step keeps reading Nova
   itself, because it waits for the state to change. Each check keeps its own
   step, its own comment and its own failure; only the read is shared.
2. "Record activity on the data share" moves after the poll: it does not change
   what the poll sees, and today it delays detection by about 7 s.

Saves about 12 s (#538).

### C. Unshelve: look for Nova's answer on every poll while the server is idle

Today the poll reads Nova's action events only when `total_wait_time % 30 == 0`
(about every 40 s of real time). Change: read them on every poll iteration while
`task_state` is null and the iteration is not the first, exactly as the
30-second check does now. The guards that #127 and Decision 30 rely on stay: an
Error found while `task_state` is non-null is never this attempt's, the errors
that existed before this run (`BASELINE_ERROR_REQS`) are ignored, and
`schedule_instances` Error is no host while a `compute_*` Error is a host
failure.

Detection goes from about 45 s to 5–10 s after the request. Cost: one more
`openstack server event list` per 5-second iteration, only while the server is
idle, at most one per iteration of the existing 300-second poll budget.

### D. Tell the member before collecting diagnostics

On the no-host paths (create's capacity ERROR, unshelve's `no_capacity`), post
the member's failure comment first, then collect the diagnostics, then (create)
delete the server. The diagnostics still run before the delete, so Nova's record
is still captured; they are still posted as their own collapsed comment. The
unshelve host-failure comment keeps the diagnostics inside it, so that path
keeps its order.

Saves 4–8 s.

**Expected result:** #539 would answer the member in about 38 s instead of 53 (A
−11 s, D −4 s); #538 in about 85 s instead of 141 (B −12 s, C −37 s, D −8 s),
and in about 45 s when the runner is free (the 43 s wait is the open decision
below).

## Not changed, and why

- **No vacancy check before the attempt.** See the rule: the numbers are wrong
  in both directions, and the attempt costs Nova about 3 s.
- **The diagnostics are not skipped or cut down.** They are the evidence the
  incident store needs to tell scarcity from a failing host (the hosts to report
  to Jetstream2), and nothing else records it once the server is gone.
- **The `authorize` job stays a separate GitHub-hosted job** (about 20 s of
  start-up and hand-off): it is the security boundary before the self-hosted
  runner.

## Open decision for the maintainer: which runner a member's unshelve waits for

`create-instance.yml` runs on `MORPHOCLOUD_ACQUIRE_RUNNER` and the sweeps on
`MORPHOCLOUD_CONTROL_RUNNER`, but `control-instance.yml` (unshelve, shelve,
delete) runs on any `self-hosted` runner. On Test there is one runner, so a
member's unshelve waits behind the runner clock's sweeps (#538: 43 s). On
production the p50 wait is 2 s, but an unshelve can still land behind sweeps.
Option: run `control-instance.yml` on the acquire runner when the command is
`/unshelve` (it competes for the same Jetstream2 capacity as `/create`). This is
not in the first implementation; it is listed with its data for a decision.

## Who else touches the same resources (and what this changes for them)

| actor                                                 | shared resource                        | effect of A–D                                                                                                                          |
| ----------------------------------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Nova scheduler (every Jetstream2 project)             | hosts                                  | none: the same requests, sooner                                                                                                        |
| Other takers of floating IPs (fip-police, other runs) | the address pool                       | none: `ensure-spare` stays before Nova; the attach after ACTIVE keeps its compare-and-swap                                             |
| The per-issue concurrency group                       | one command per issue at a time        | runs end sooner, so a queued command starts sooner                                                                                     |
| The acquire lane (creates, workshop backfill)         | the runner                             | a failing create holds it for less time                                                                                                |
| `update-request-status-label`, `reconcile-instances`  | read the server and the issue's labels | the server exists about 11 s earlier in the run; it is deleted after the diagnostics as today; the label step still runs on every path |
| The incident store harvester                          | the run's log                          | none: the same error lines and the same diagnostics block (its tests pin them)                                                         |
| Jetstream2's API                                      | request rate                           | B removes about four `server show` calls per unshelve; C adds up to one `event list` per idle 5-second poll                            |

## Test plan (Test-Instances, as amm554; uses real resources, presented for approval)

| #   | case                                    | how                                                                                              | pass                                                                                                                                                                           |
| --- | --------------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| T1  | `/create` with no host                  | a flavor Nova has no room for at the time (as on 2026-11-10: fill the remaining slots, then ask) | member's no-capacity comment ≤ 40 s after `/create` when the runner is free; diagnostics comment follows; server deleted; store: `capacity.cause = no_host`, `evidence = nova` |
| T2  | `/create` that succeeds                 | a flavor with room                                                                               | same progress table, address, setup and email as today                                                                                                                         |
| T3  | `/unshelve` with no host                | as T1, on a shelved instance                                                                     | failure noticed ≤ 15 s after the unshelve request; comment; instance still SHELVED_OFFLOADED; store: `no_host`, `nova`                                                         |
| T4  | `/unshelve` that succeeds               | a flavor with room                                                                               | unchanged outcome and timing at least as fast                                                                                                                                  |
| T5  | the pre-checks still refuse             | `/unshelve` on an ACTIVE instance; `/unshelve` on a nonexistent one                              | the same refusal comments as today                                                                                                                                             |
| T6  | unshelve host failure, dry address pool | cannot be forced safely                                                                          | unchanged code paths; covered by review and the existing fixtures                                                                                                              |

## Build order

One PR on share-prototype with A–D, then the test plan above, then the migration
vendorize. Production does not change before the migration.
