# A/B Experiment Results

Platform: P1=#40, P2=#41, P3=#42 (m3.xl). Cyclic 10-command sequence looped
under load.

## Run 1 (A / split) — SHAKEDOWN (2026-06-03, T0=04:20Z)

- **dpkg fix validated at scale:** 10/10 g3.large provisioned, **0 lock-frontend
  errors** (was ~1/3 failures before fix).
- **A provisioning:** 10 × g3.large fully ready in ~27–30 min (acquire→runner-1,
  setup→runner-2).
- **M3 idle baseline (no load):** /email ~20s (20,22,21,19) · /shelve ~56s
  (61,54,54) · /unshelve ~90s (104,85,82).
- **M2 under load (n=1, directional):** /email = **309s** → penalty ≈ **289s**
  vs baseline.
- **Process:** found+fixed a driver bug (`gh --jq` doesn't accept `--arg`);
  detector now pipes to standalone `jq --arg`. Storm finished before clean
  under-load capture, so A under-load here is n=1.
- **Note:** commands are `runs-on: self-hosted` (not lane-pinned), so under load
  they wait for whichever runner frees first.

## Longevity mechanism (confirmed)

`/opt/instance-config-support/dist/check-instance-shelve.sh` (no args) resets
the ~4h shelve tracker; works headless via runner→instance SSH. Unshelve also
reboots → fresh tracker. Top up idle ACTIVE platform instances every ~3h.

## Run A (clean, split/2-runner) — #54, T0=05:16Z

Provisioning: 9/10 (one failed, see below). 4 command cycles (40 samples) over
48 min. | cmd | n | mean | max | baseline(idle) | |---|---|---|---|---| | /email
| 16 | 25s | 84s | ~20s | | /shelve | 12 | 87s | 377s | ~56s | | /unshelve| 12 |
100s| 185s | ~90s | Pattern: all spikes in cycle 1 (peak storm ~05:17–36):
/shelve 377s, /unshelve 185s/143s, /shelve 116s, /email 84s. Cycles 2–4 (storm
drained) ≈ baseline. **A penalty is real but TRANSIENT** (short storm).

## Run B (clean, monolithic / single runner) — #65, T0=06:15Z

runner-2 parked (offline); all work on runner-1. 7 commands over ~1h48m.
Provisioning: **0/10 ready** (gridlocked). | seq | cmd | latency | |---|---|---|
| 1 | /email | **1133s (19m)** | | 2 | /shelve | **1855s (31m)** | | 3 |
/unshelve| **TIMEOUT >33m** | | 4 | /email | 55s | | 5 | /shelve | **639s
(11m)** | | 6 | /unshelve| 159s | | 7 | /email | **316s (5m)** | Single runner
thrashes between the 10 full-creates and interleaved commands → commands 19–31m+
early, provisioning makes **zero** progress in ~2h.

## A vs B — VERDICT

| metric                   | A (split, 2 runners)                  | B (monolithic, 1 runner)       |
| ------------------------ | ------------------------------------- | ------------------------------ |
| /email under load        | peak 84s (mostly ~20s)                | up to **1133s (19m)**          |
| /shelve under load       | peak 377s (mostly ~53s)               | up to **1855s (31m)**          |
| /unshelve under load     | peak 185s (mostly ~80s)               | **TIMEOUT >33m**               |
| provisioning 10×g3.large | **~27 min, 9/10**                     | **0/10 in ~2h (gridlock)**     |
| idle baseline (both)     | email 20s / shelve 56s / unshelve 90s | (same — no contention at idle) |

**Conclusion:** the split decisively wins on BOTH command responsiveness (~5–30×
faster under load) AND provisioning throughput under concurrent command load (B
gridlocks). Worst-case (continuous command loop); a sporadic real user fares
better, but even B's _first_ command during the storm took 19 min.

## B-config idle baseline (runner-2 parked) — measured 14:46Z

/email: 29,27,26,27 -> 27.3 ± 1.3 (n=4) /shelve: 58,65,63 -> 62.0 ± 3.6 (n=3)
/unshelve: 90,87,92 -> 89.7 ± 2.5 (n=3) (A-config idle baseline was: /email
20.5±1.3, /shelve 56.3±4.0, /unshelve 90.3±11.9 — baselines ~match, confirming
idle latency is ~config-independent.)

## SYMMETRIC TABLE (mean ± SD (n), seconds)

| command   | A baseline    | A under load | B baseline   | B under load             |
| --------- | ------------- | ------------ | ------------ | ------------------------ |
| /email    | 20.5±1.3 (4)  | 36±32 (4)    | 27.3±1.3 (4) | 501±562 (3)              |
| /shelve   | 56.3±4.0 (3)  | 182±172 (3)  | 62.0±3.6 (3) | 1247±860 (2)             |
| /unshelve | 90.3±11.9 (3) | 137±51 (3)   | 89.7±2.5 (3) | 159 (1)+1 censored >1980 |

Penalty (load - own baseline): A: email +16, shelve +126, unshelve +47. B: email
+474, shelve +1185, unshelve +69(unreliable, n=1+censored).

## CORRECTION (job-level build vs queue) — supersedes earlier build figures

Earlier I wrote "B build 53.9 min" and "B 0/10 provisioned". BOTH WRONG:

- B provisioned 10/10 (all control jobs success).
- 19.1/53.9 min were TOTAL = queue + build, mislabeled as build. Job-level
  decomposition (create-instance-from-workflow run/job timestamps): | metric | A
  (split, 2 runners) | B (monolithic, 1 runner) | |---|---|---| | BUILD (job
  execution) | 782 ± 162 s (13.0 min), n=9 | 556 ± 43 s (9.3 min), n=10 | |
  QUEUE (dispatch -> runner pickup) | 366 ± 275 s (6.1 min) | 2675 ± 1782 s
  (44.6 min) | | TOTAL (queue+build) | ~19.1 min | ~53.9 min | Builds are within
  the 20-min setup cap (A max 15.8 min, B max 10.4 min). The architecture
  difference is QUEUE, not build: B's single runner serializes 10 builds
  (instance #10 waits ~89 min); A's two runners keep queue ~6 min. A's "build"
  spans acquire->setup across both runners (incl control-lane wait), so it reads
  slightly higher than B's single-job build.
