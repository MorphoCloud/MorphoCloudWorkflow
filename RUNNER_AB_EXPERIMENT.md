# A/B Experiment — Runner Split (2-runner lanes) vs Monolithic main (1 runner)

Status: **contract draft for review.**

## Why

The goal of this experiment is to evaluate the experience of a normal
MorphoCloud instance user trying to do his things while a set of instances are
being provisioned.

We expect that under a 10-instance workshop `/create`, the **split + 2-runner**
architecture keeps interactive commands (`/shelve`, `/unshelve`, `/email`)
responsive and provisions instances no slower, vs. the **monolithic + 1-runner**
baseline where commands queue behind the create storm.

However this experiment will show us real-time gains. This will determine
whether it is worthwhile to transition to this more complex workflow.
Secondarily we want to exercise the new workflow extensively to uncover hidden
race conditions or bug or conflicts we may not have considered, or overlooked in
the code.

## How

- **A — split:** `Instances/main` = the runner-split code (two-job split + lane
  routing). **Both runners** online: runner-1 = `acquire`, runner-2 = `control`.
- **B — baseline:** `Instances/main` reverted to the **monolithic** code
  (vendorize MorphoCloudWorkflow `main` → `Instances/main`). **runner-2 parked**
  (service stopped) → single general-purpose runner. (main is not meant to use
  two runners.)

## Hard rules (non-negotiable)

1. **All _driving_ is from the GitHub issue page only** — issue comments,
   exactly as a human clicking through the issues page. **No driving from
   OpenStack or the runners**: no create/shelve/unshelve/delete via CLI, no
   `--ref` dispatch, no `workflow_dispatch`, no runner SSH intervention.
   **Measurement may cross-correlate read-only sources** — OpenStack logs /
   `console log show`, workflow run + job timestamps — to corroborate the
   issue-page timings. Observation is allowed; driving is not.
2. Commands are issued **serially, gated on completion**: post a command, wait
   until its result comment appears on the issue, _then_ post the next. (So a
   `/email` is never posted before its prerequisite `/unshelve` shows
   completed.)
3. The command **sequence is fixed and identical** in all 6 runs. Only the
   architecture (A vs B) varies.

## Fixtures

### Workshop (the load)

- One workshop request per run: **10 × g3.large**, started inside the 12h
  window.
- Driven by hand: open request → `/approve` → **one** `/create`. `/create`
  spawns the 10 sub-issues; I never `/create` a sub-issue.

### Command platform (3 real, dedicated **individual** instances)

- **P1, P2, P3** — provisioned once, _before_ the experiment, as
  `request-type:instance` issues (individual, so the workshop backfill never
  touches them). Reused, unchanged identity, across all 6 runs.
- **Start state, reset before every run** (via `/shelve`÷`/unshelve` comments —
  not measured): **P1 = ACTIVE, P2 = ACTIVE, P3 = SHELVED.**

## Deterministic command sequence (cyclic; issued serially, each after the prior completes)

**Looped continuously** during the whole provisioning window (see Per-run
procedure). The sequence is **cyclic** — it ends in its start state
(`P1 ACTIVE, P2 ACTIVE, P3 SHELVED`), so it repeats with no reset between loops.

| #   | Command        | State after                  |
| --- | -------------- | ---------------------------- |
| 1   | `/email` P1    | P1-A P2-A P3-S               |
| 2   | `/shelve` P1   | P1-S P2-A P3-S               |
| 3   | `/unshelve` P3 | P1-S P2-A P3-A               |
| 4   | `/email` P3    | P1-S P2-A P3-A               |
| 5   | `/shelve` P2   | P1-S P2-S P3-A               |
| 6   | `/unshelve` P1 | P1-A P2-S P3-A               |
| 7   | `/email` P1    | P1-A P2-S P3-A               |
| 8   | `/shelve` P3   | P1-A P2-S P3-S               |
| 9   | `/unshelve` P2 | P1-A P2-A P3-S               |
| 10  | `/email` P2    | **P1-A P2-A P3-S = start ✓** |

Every instance gets shelve + unshelve + email each cycle. Each command is posted
the instant the previous one's result comment appears (rule #2).

**Why loop** (from review): a single pass would finish within roughly the first
⅓ of the workshop-creation window, leaving the rest of the provisioning time
with no data. Looping fills the entire window with measurements under sustained
load.

## Metrics (all from issue-page comment timestamps)

- **M1 — per-workshop-instance creation time**, for each of the 10:
  `(that sub-issue's final "all ✅" progress comment) − (parent /create comment)`.
  Report the 10 values + min / median / mean / max.
- **M2 — per-command latency under load**:
  `(its result comment) − (its command comment)`, for every command issued.
  Because the sequence loops, each command type recurs many times during the
  window — report the **per-command-type latency distribution** (count, mean,
  median, max) for the run.
- **M3 — idle baseline**: the latency of one command pass run _after_ the
  workshop is fully up (nothing provisioning). The headline result is the
  **contention penalty = (M2 under-load latency) − (M3 baseline)** per command
  type, A vs B.
- **Throughput**: number of full command cycles completed during the
  provisioning window (more cycles in the same window = more responsive).
- **Start delay (cleanest contention signal)**: per command,
  `(job in_progress) − (command comment)`, from the run's queued→in-progress
  timestamps (read-only cross-correlation, allowed). The work itself is
  ~constant across architectures, so the queue wait is the purest A-vs-B signal
  — lean on `/shelve` and `/unshelve`; treat `/email` as noisier (external
  lookup + SMTP).
- **Capacity-failure tagging**: any instance that fails `/create` with
  `No valid host` (JS2 inventory, not architecture) is tagged and **excluded**
  from the M1 comparison; note if availability actually constrained a run.

## Per-run procedure

1. Reset platform to start state (P1/P2 ACTIVE, P3 SHELVED). _(setup, not
   measured)_
2. Open a fresh workshop request (10 × g3.large) → `/approve`. _(setup)_
3. **T0:** post `/create` on the workshop. Immediately begin **looping** the
   cyclic command sequence (serial, completion-gated) on P1/P2/P3, and keep
   looping until **all 10 workshop instances are fully ready** (final ✅ +
   emailed).
4. **Baseline:** with nothing provisioning, run one more command pass → M3.
5. Collect M1 (10 instances), M2 (all under-load commands), M3 (baseline pass).
6. Teardown gate: `/delete_all` the 10 sub-issues, then **wait until fully torn
   down** — all 10 servers + volumes deleted and their floating IPs back in the
   pool — _before_ the next run's `/create`. Guarantees every run starts from
   the same zero state. _(not measured)_

## Run order — counterbalanced (A B A B A B)

3 runs per condition, but **alternated**, not A A A then B B B. Over a ~12 h
window JS2 load and time-of-day drift; alternating spreads both conditions
across the same time bands so drift isn't charged to the architecture. Cost: 5
condition switches (~5 min each).

Each switch flips `Instances/main` and the runner count:

- **→ A (split):** vendorize MorphoCloudWorkflow **runner-split** →
  `Instances/main`; push. Ensure **both** runners online (runner-1 = acquire,
  runner-2 = control).
- **→ B (monolithic):** vendorize MorphoCloudWorkflow **main** →
  `Instances/main`; push. **Park runner-2** (stop its service); confirm only
  runner-1 online.

Pre-flight before each run: confirm the right `Instances/main` is live, the
right runner(s) online, and the prior run fully torn down (teardown gate above).
(Restore afterward only if you ask: re-vendorize the split, unpark runner-2.)

## Execution

- **Run 1 (A) is supervised end-to-end** before the loop goes autonomous. It is
  also the first **at-scale validation of the dpkg-lock fix** (live but never
  run under a real 10-instance storm). If run 1 is clean (10/10 ready, no
  apt-lock errors) and the measurement pipeline checks out, the remaining runs
  proceed autonomously.
- **Results are written incrementally** — each run's M1 / M2 / M3 / start-delay
  / throughput is appended to a results file the moment that run finishes, so a
  late failure never loses earlier runs.

## Tabulation (final output)

For A and B side by side:

- M1: mean / median / max instance-creation time (across 10 × 3 runs) + per-run
  spread.
- **Contention penalty (M2 − M3)** per command type, averaged across the 3 runs
  — the headline comparison.
- M2 raw under-load latency and throughput (cycles/run) per condition.
- Any command that ran degraded/slow or errored under contention (goal #2:
  surfacing hidden races/bugs).

## Scale / cost

6 runs × 10 g3.large = 60 builds. At ~10min/instance create, each experiment is
going to run 1.5-2h x 6 for total of ~10–12 h total, plus per-run
reset/teardown. No g3.large ceiling concern (per allocation owner).
