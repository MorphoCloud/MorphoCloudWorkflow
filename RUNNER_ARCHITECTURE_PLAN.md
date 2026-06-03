# Runner Architecture & Concurrency — Design Note

Status: **IMPLEMENTED & VALIDATED (2026-06-03), merged to `main`.** The
two-runner split (`acquire` / `control` lanes) is live and was A/B-tested
against the monolithic single-runner — interactive commands stay far more
responsive under a workshop create-storm (full results in
`RUNNER_AB_RESULTS.md`). A **workshop-build trickle** was added on top: a soft
de-prioritization of workshop creates vs. individual-user commands, via
`MORPHOCLOUD_WORKSHOP_BUILD_BATCH` (default 3). Sections 1–10 are the original
analysis/exploration; §9 (reconciler) was explicitly _not_ built.

> **Known limitation (soft, not hard).** Precedence comes from lane separation +
> the trickle, but GitHub self-hosted scheduling isn't strict FIFO, and
> individual `/create` is itself a long acquire+setup job that holds the acquire
> lane while it runs. A _hard_ guarantee would need a dedicated command lane + a
> floating-IP mutex — not built (the trickle is the low-risk path that preserves
> FIP safety).

---

## Decision (agreed 2026-06-02)

Built **now**, during the pre-launch offline window, because once MorphoCloud is
advertised it is effectively **frozen for ~2 years** (solo volunteer maintainer,
no further downtime windows). So the rule is: **do the architectural work that
needs the window now; defer only what can be added live later; don't build
what's too heavy to maintain alone.** All work happens on a **feature branch —
`main` is never touched until validated** (see "Implementation contract"), so
the current working system is always the fallback.

### What we are building (needs the window)

1. **Control runner** — a second self-hosted runner (label `control`) for the
   safe lane. _Why:_ the single runner is starved by long create jobs, so
   shelving/status crons fire hours late (issue #1). Separate lanes fix everyday
   reliability.
2. **Move lifecycle crons → control runner** (shelving, status, uptime,
   backfill). _Why:_ they are read-only / resource-freeing (safe alongside a
   create) and must never starve behind a create storm.
3. **Two-job split of the create path** — Job A (acquire runner: FIP
   allocation + `server-create`, serial) → Job B (control runner: FIP-associate,
   cloud-init, mount, email). Same workflow, `needs:`; the only baton is the
   **issue number** (instance name is `instance-<issue>`, state lives in
   OpenStack). _Why:_ the one change that needs the window (it refactors the
   proven create path). It frees the scarce acquire runner and — key for a
   2-year freeze — turns throughput into a **runner-count knob**: scaling up
   later = _add control runners_ (live, no code, no downtime), never a rewrite.
4. **Setup watchdog** — periodic re-dispatch of Job B for any instance stuck
   "built but setup-incomplete." _Why:_ non-expert organizers can't notice/fix a
   half-provisioned VM; this self-heals and closes the crash-orphan gap.
   Per-instance (not target-based), so it also covers course instances.
5. **`email-sent` marker** — _Why:_ so the watchdog's retries never double-email
   credentials (the one non-idempotent step).
6. **Phase 0:** cron `cancel-in-progress: true → false`. _Why:_ a starved
   lifecycle cycle should queue, not be silently dropped.
7. **Soak-test** on Test-Instances / via `workflow_dispatch --ref <branch>`.
   _Why:_ validate on the branch before main; the split's mechanics test fine on
   plentiful g3.large (capacity-independent).

### Deferrable (additive, no window needed — safe to do live, even post-freeze)

- **More control runners**, and **automating** their per-workshop
  provision/teardown (the temp-pool idea). _Why deferrable:_ adding a runner is
  pure infra — no code, no downtime, no risk to the create path. Add runners by
  hand for the first workshops; automate later if/when usage warrants.

### Explicitly NOT building

- **The reconciler / desired-state state machine (§9).** _Why:_ right for
  continuous, team-maintained load; wrong for a solo volunteer freezing for ~2
  years — a polling state machine is the worst thing to debug alone in 18
  months. The two-job split + add-runners covers the need with far less to
  maintain.

### Applies to courses too

All three create paths — individual (`create-instance.yml`), workshop
(`create-instance-from-workflow.yml`), course (`create-course-instance.yml`) —
share the `create-instance` composite action, so the **two-job split, watchdog,
and `email-sent` marker land on courses automatically via vendorize.** Course
specifics:

- Make the job `runs-on` labels **configurable per repo** (a repo variable,
  defaulting to `acquire`/`control`) so a course repo with only one runner falls
  back gracefully to single-runner behavior — no course is broken by the split.
- The watchdog is **per-instance**, so it covers course instances unchanged. The
  workshop **backfill** (top-off toward a `workshop-target`) stays workshop-only
  — courses have no target.
- Courses stay on GitHub `on: schedule` (not the Instances VM-cron, by your
  rule), so the course watchdog runs on the slower GitHub cadence — acceptable,
  as course instances trickle in over days rather than a synchronized burst.

### Implementation contract

- All work on a **feature branch** in MorphoCloudWorkflow; vendorize to a
  **branch** in the target repos via `nox -s vendorize -- … --branch` (which
  opens a PR).
- \*\*MOrphoCloudWorkflow `main` is never to be 1touched without explicit
  permission of the human (double check)

---

## 1. Problem

All OpenStack work runs on a **single self-hosted runner** (BIO180006,
`149.165.152.104`), which executes one job at a time. During a workshop
`/create` storm this saturates: long create jobs monopolize the runner and the
short lifecycle/cron jobs (shelving, status, uptime, backfill) starve.

### Evidence — workshop #9 (2026-06-02)

Six `g3.xl` instances requested at 16:37. Backfill progress comments:

```
16:50  2/6      16:56  3/6      17:17  4/6      17:31  5/6   (6th never came up — capacity)
```

~54 min to reach 5/6, with a 21-min gap to 4/6 — not a clean cadence. The run
log shows why:

| Run                                         | Wall time   | Result           |
| ------------------------------------------- | ----------- | ---------------- |
| Create Instance from Workflow (16:56→17:30) | **~34 min** | success          |
| Create Instance from Workflow (16:56→17:32) | **~36 min** | failure          |
| Workshop Backfill (17:00→17:17)             | **~17 min** | success (queued) |

And the lifecycle crons were **cancelled** because they never got the runner:

```
17:00 Automatic Instance Shelving  → cancelled 17:05
17:05 Automatic Instance Shelving  → cancelled 17:10
17:00 Collect Instance Uptime      → cancelled 17:15
17:00 Update Request Status Label  → cancelled 17:05
```

This is the same mechanism behind the earlier **"shelved ~3h late"** (issue #1):
when the runner is jammed by creates, shelving cycles are starved and then
dropped. So this is not just slow provisioning — it is a **lifecycle
reliability** problem during any busy period.

## 2. Root causes

1. **A create holds the runner for its entire lifecycle, not "a few minutes."**
   The job blocks on the post-ACTIVE readiness wait (cloud-init/SSH; ceiling
   `max_wait_time=1200` = 20 min at `create-instance/action.yml:509`), plus the
   BUILD→ACTIVE wait (`:280`, now 300s), plus volume mount / SSH / email. One
   create can occupy the runner ~25 min.
2. **One runner serializes everything** — long creates and sub-minute lifecycle
   jobs share the same single lane.
3. **The single runner is the _only_ concurrency control we have.** The command
   workflows (`create-instance`, `control-instance`) have **no `concurrency:`
   group** today. The cron workflows do, keyed by `workflow + OS_CLOUD`, and
   shelving/status/uptime use `cancel-in-progress: true` — which is exactly why
   starved cycles are dropped rather than delayed.

## 3. Why the naïve "two runners: `/create` vs everything else" is unsafe

The reason the system assumes serial execution is the **floating-IP pool +
compute-quota** hazard. `/create` is not the only op that hits it:

- **create** retrieves/creates a FIP from the shared pool and associates it
  (`create-instance/action.yml:119` "Retrieve or create floating IP" → `:424`
  associate), and consumes compute quota.
- **unshelve** calls the **same `create-ip` action**
  (`control-instance/action.yml:299-305`) when the resumed instance has no FIP,
  and re-consumes compute quota.
- **shelve / delete** only _free_ resources (offload compute,
  release/disassociate FIPs). Status / uptime are read-only.

So two concurrent FIP-allocating ops can race on the pool (TOCTOU: both claim an
"unused" FIP) and on the quota cap. **create and unshelve are the same hazard
class.** Putting `unshelve` on the "everything else" runner while `create` runs
on its own runner reintroduces the exact race the serial model prevents.

### JS2 FIP policy (clarified) — implementation is compliant

JS2 caps the FIPs the **project** may hold (~90). The requirement is that a FIP
must not stay **stuck on a shelved instance** — on shelve it has to return to
the **project's** pool of unattached FIPs. It does **not** have to be deleted
back to JS2's global pool; staying allocated-but-unattached to the project is
fine. Verified the implementation meets this:

- Shelve **disassociates**: `openstack server remove floating ip`
  (`control-instance/action.yml:568`) → the FIP becomes unattached (Port==null),
  back in the project pool. ✓ Not stuck on the shelved instance.
- Unshelve draws from that pool via `create-ip` — reusing an unattached FIP
  (preferring the old address) or creating a new one within the ~90 cap
  (`create-ip/action.yml:27-56`). ✓

So **no compliance issue.** But this is still the **same hazard class as
create**: both ops _select-or-create from the shared project FIP pool_ and both
consume compute quota + count against the ~90-FIP cap. Two concurrent
`create-ip` calls can pick the **same** unattached FIP (Port==null TOCTOU) or
both push past the cap/quota → **create+unshelve must serialize.**
(Shelve/delete only _release_ into the pool, so they stay safe to parallelize
with an acquire.)

### The correct dividing line is **hazard class, not command duration**

| Lane                              | Ops                                                                                                | Why                                                                              |
| --------------------------------- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **Acquire** (must serialize)      | `create`, `unshelve`                                                                               | allocate FIP from shared pool + consume quota (both call `create-ip`)            |
| **Control** (safe to parallelize) | `shelve`, `delete`, volume-delete, status, uptime, backfill/reconciler _detection_, command intake | read-only or resource-_freeing_; only one side ever _allocates_, and it's serial |

A `delete` (releases a FIP) running alongside a `create` (allocates one) is fine
— releasing returns to the pool; only allocation is racy, and allocation stays
serial on the acquire lane.

## 4. The duration objection — and why #2 dissolves it

**Your point is valid:** `/unshelve` and `/shelve` take minutes; you don't want
a quick `/unshelve` stuck behind a 20-minute `/create`. That latency cost is the
_only_ real argument for separating unshelve from create — and it disappears if
the create stops holding the lane for 20 minutes.

That is exactly what **#2 (decouple the runner-hold)** does: fire the OpenStack
mutation and return the runner in ~1 minute; move the readiness wait + finalize
to a short reconciler. With #2 the acquire lane is never busy more than ~1 min,
so serializing `unshelve` behind `create` costs ~1 min, not 20 — **safe _and_
fast.** So #2 and the hazard-class split are synergistic, not alternatives:

- **Two runners without #2:** lifecycle stops starving, but a 30-instance
  workshop still crawls serially on the acquire runner, and an `/unshelve` can
  still wait behind a long create.
- **#2 with the split:** creates fire-and-return; OpenStack builds them in
  parallel _off-runner_; the reconciler polls on the control lane; the acquire
  lane has high throughput and short waits.

## 5. Proposed architecture

1. **#2 fire-and-poll.** `create`/`unshelve` issue the OpenStack call and exit.
   Readiness (BUILD→ACTIVE, cloud-init/SSH), FIP-association verification,
   volume mount, and the credentials email move to a **reconciler**.
2. **Reconciler (single, on the control lane)** drives actual→desired state in
   short ticks; it is the _only_ mutator for transitions and **absorbs the
   backfill** (create/unshelve/top-off become one loop). Repeated `/unshelve`
   becomes a no-op by construction (it just re-asserts desired state).
3. **Lanes via runner labels:** `control` runner(s) for short/safe jobs; one
   `acquire` runner for create+unshelve.
4. **Logical mutex independent of runners:** a shared
   `concurrency: { group: acquire-lane, cancel-in-progress: false }` on the
   create and unshelve workflows serializes acquires regardless of runner count
   — it survives a mislabeled runner and is the real lock. (Use `false` = queue;
   never cancel a half-finished acquire.)
5. **Idempotent, state-aware ops:** every op reads OpenStack state first and
   no-ops if already there or mid-transition (never re-fire `unshelve` while
   `UNSHELVING`/`BUILD`).

### Concurrency matrix (target)

|              | create      | unshelve    | shelve | delete | status/uptime |
| ------------ | ----------- | ----------- | ------ | ------ | ------------- |
| **create**   | ✗ serialize | ✗ serialize | ✓      | ✓      | ✓             |
| **unshelve** | ✗ serialize | ✗ serialize | ✓      | ✓      | ✓             |
| **shelve**   | ✓           | ✓           | ✓\*    | ✓      | ✓             |
| **delete**   | ✓           | ✓           | ✓      | ✓\*    | ✓             |

\* same-op concurrency handled by that workflow's own per-instance group.

## 6. Phased migration (each phase is independently shippable)

- **Phase 0 — cheap, immediate (no new runner):**
  - Flip `automatic-instance-shelving` (and status/uptime) cron
    `cancel-in-progress: true → false` so a starved cycle **queues instead of
    being dropped**. This alone likely fixes "shelved late."
  - Add the `acquire-lane` concurrency group to create+unshelve (harmless on one
    runner; prepares for the split).
- **Phase 1 — add the control runner:** move lifecycle crons + intake +
  shelve/delete to a `control` runner; keep create+unshelve on the original (now
  `acquire`) runner. Fixes lifecycle starvation. Safe — control jobs are
  read/free.
- **Phase 2 — #2 decoupling:** split the readiness wait into the reconciler;
  create/unshelve become fire-and-return. Throughput fix; shrinks acquire-lane
  latency to ~1 min.
- **Phase 3 — optional, later:** true parallel acquires. Requires making
  `create-ip` (pool selection) + quota allocation atomic/concurrency-safe; then
  relax `acquire-lane` to a per-instance group and add acquire runners.

## 7. Risks & open questions

1. **create-ip race — RESOLVED.** create and unshelve both select-or-create from
   the shared project FIP pool via `create-ip` (and share the ~90-FIP cap +
   compute quota), so concurrent execution can race on pool selection →
   **serialize create+unshelve.** FIP handling is policy-compliant (see §3:
   shelve disassociates the FIP back to the project pool, not stuck on the
   shelved instance), so there's no separate fix needed.
2. **Reconciler is a real state machine** (BUILD/ACTIVE/ERROR/SHELVED/
   SHELVED_OFFLOADED/UNSHELVING) — needs careful design + tests.
3. **Email/credential timing** moves from end-of-create-job to reconciler
   finalize step.
4. **Second runner provisioning** on JS2 (how it's stood up, labels, the token).
5. **Backfill duplicate-dispatch** edge already noted — folds into the
   reconciler naturally.

## 8. Decisions needed before building

- Stop at **Phase 0 + 1** (split + queue-don't-cancel), or commit to **Phase 2**
  (reconciler)?
- One control runner, or a small pool?
- Is `/unshelve` latency-behind-create acceptable in Phase 1 (pre-#2), or is
  that the trigger to do Phase 2?
- Validate the create-vs-unshelve FIP race first (experiment), or design as if
  it's real (serialize) and revisit in Phase 3?

## 9. Reconciler design (the decoupled, error-safe model)

### 9.1 State lives on the issue (phase labels)

Today an instance's progress lives _inside_ the running job (implicit,
edge-triggered). Decoupled, it must be **externalized** so any tick can resume.
Each sub-issue carries one `phase:*` label plus a few markers:

- `phase:creating` — server-create issued, awaiting ACTIVE
- `phase:active` — Nova ACTIVE, FIP not yet associated
- `phase:setup` — FIP associated; cloud-init / mount / SSH / email underway
- `phase:ready` — fully provisioned (terminal success)
- `phase:failed:<reason>` — terminal failure
  (`capacity|build-timeout|fip|mount|ssh|email`)
- markers: `fip-associated`, `email-sent` (idempotency guards), and a
  `phase-since:<epoch>` clock for deadlines (or read OpenStack/issue timestamps)
- desired-state (for lifecycle): `desired:running` / `desired:shelved`

These get added to `labels.yml`.

### 9.2 The split

- **Acquire job** (acquire runner, `acquire-lane` group): preamble + `create-ip`
  (FIP allocation — the racy part that must serialize) + create volume +
  `openstack server create`, set `phase:creating`, **exit**. Holds the runner
  ~1–3 min instead of ~13.
- **Reconciler** (control runner, singleton cron, ~every 2–3 min + dispatch):
  the level-triggered **owner**. Each tick it lists every sub-issue with a
  non-terminal `phase:*` (and every approved workshop sub-issue below target),
  reads actual OpenStack state, and advances one step. It is the **single
  mutator** of post-create transitions and **subsumes the backfill** (dispatches
  acquire jobs for missing sub-issues up to `workshop-target`, respecting
  `MAX_TOTAL`).

### 9.3 State machine (one tick, per instance)

| Phase                   | Check                                        | Action                                                                                                            | On failure                                           |
| ----------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| (missing, below target) | —                                            | dispatch acquire job → `phase:creating`                                                                           | —                                                    |
| `creating`              | OpenStack status                             | ACTIVE → `active`; ERROR(no host) → capacity cleanup, back to missing; BUILD past deadline → delete orphan, retry | —                                                    |
| `active`                | FIP associated?                              | associate FIP (idempotent; tolerate JS2 500) → `setup`                                                            | retry; deadline → `failed:fip`                       |
| `setup`                 | cloud-init/SSH ready? mounted? `email-sent`? | when ready+mounted → send email iff `!email-sent` → set marker → `ready`                                          | per-step retry; deadline → `failed:<step>` + comment |
| `ready` / `failed:*`    | terminal                                     | nothing                                                                                                           | —                                                    |

Every transition is **idempotent** — re-running a tick that already associated
the FIP / mounted / emailed is a no-op (guarded by markers + state checks). A
crashed or skipped tick loses nothing; the next tick resumes.

### 9.4 Error handling & safeguards (who catches the tail)

- **The reconciler owns it, not a runner/job.** Level-triggered re-sweeps mean
  no instance is unowned — including ones orphaned by a crashed acquire job
  (they sit at `creating`/`active` and get picked up). This is **stricter** than
  the sync model, where a job dying post-ACTIVE orphans silently (the bug we
  hit).
- **Per-phase retry budget + deadline** → `failed:<reason>` + the same ❌ issue
  comment as today, then stop (no infinite loop).
- **Email exactly once** via the `email-sent` marker — the one non-idempotent
  action, explicitly guarded.
- **Failure policy:** mount/ssh fail → `failed:setup` + comment; email fail →
  comment with manual recovery, **leave the VM up** (it's functional).
- **Desired-state wins:** a `/shelve` or `/delete` mid-setup just flips
  `desired:*`; the reconciler reconciles to the latest (single actor, no fight).

### 9.5 Reused vs new

- **Reused (moved, not rewritten):** FIP-associate (incl. JS2-500 retry),
  cloud-init/SSH/mount checks, the `workshop-send-email` action,
  capacity-ERROR + orphan cleanup — all exist today in
  `create-instance`/`control-instance`; re-hosted in the reconciler and made
  re-runnable.
- **New:** phase-state model + labels, the sweep/state-machine driver, per-phase
  deadlines, the `email-sent` marker, folding in the backfill target logic.

## 10. Effort & build plan

**Doing the reconciler right is a project, not a session.**

| Piece                                                                                                      | Scope                                                | Risk                                    |
| ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- | --------------------------------------- |
| Phase-state labels + markers + helpers                                                                     | small                                                | low                                     |
| Refactor `create-instance` → acquire-only (extract the post-server-create tail)                            | medium surgery on a ~900-line, battle-tested action  | **high** (tested hot path)              |
| Reconciler workflow + state-machine action (sweep, transitions, idempotency)                               | large (~300–500 lines + helpers)                     | medium                                  |
| Re-host FIP-associate / mount / SSH / email as re-runnable steps                                           | medium                                               | medium                                  |
| Per-phase deadlines + retry budgets + failure comments                                                     | medium                                               | medium                                  |
| `email-sent` marker                                                                                        | small                                                | **high if wrong** (double / never send) |
| Fold in backfill target logic                                                                              | small–medium                                         | low                                     |
| Live testing w/ failure injection (mount fail, email fail, ACTIVE-timeout, capacity ERROR, crash mid-tick) | **large — gated by slow live g3.xl capacity cycles** | the real cost                           |

**Estimate: several focused build-days + ~1–2 weeks of live hardening**, testing
dominating (throttled by capacity/iteration speed, as this week showed).
Riskiest single item: refactoring the proven `create-instance` action. Most
correctness-sensitive: email-once.

**Recommendation — phase it, don't big-bang:**

1. **Phase 0** (≈done): cron `cancel-in-progress:false`; concurrency groups
   (shipped for create-from-workflow + backfill).
2. **Phase 1:** control runner — lifecycle crons stop starving. Biggest
   reliability win for least work; no reconciler.
3. **Phase 2a — two-job handoff** (~1–2 days, low risk): split `create-instance`
   at server-create into Job A (acquire) + Job B (setup on control runner).
   Reuses every tested step; Job B keeps today's `failure()`/comment error
   handling; setups parallelize. ~All the throughput win **without** the state
   machine — but a crashed Job B still orphans (same gap as today).
4. **Phase 2b — reconciler** (the project above): build when you want the
   crash-orphan watchdog + a single desired-state owner; it then subsumes
   backfill and Job B.

**Bottom line:** the reconciler is **not a "right now" change** — it's the
multi-week end-state. The **two-job handoff** captures most of the value at a
fraction of the cost/risk and is the sane next step after the control runner.
