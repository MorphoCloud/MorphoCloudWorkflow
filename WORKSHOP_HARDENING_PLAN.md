# Workshop Hardening — Implementation Plan

Hardening the workshop `/create` path so instances don't shelve mid-workshop,
can't be created too early, are gated against real JS2 capacity, and auto-fill
to the requested count as capacity frees up. All times communicated to
organizers in **their own local timezone** — never UTC.

## Problems being fixed

1. **Lifecycle anchored to `/create` time**, so a workshop created the evening
   before shelves mid-session (timeout = `DURATION×24` from boot; e.g. a 2-day
   workshop created Sun 3 PM shelves Tue 3 PM, before it ends).
2. **Silent failures** — `/create` validation errors (`Invalid duration`, etc.)
   exit before the comment step, so the organizer sees nothing.
3. **No capacity awareness** — `/create` will happily start 30 instances when
   only 10 GPU slots are free, blocking itself and other users (~10
   min/instance, serial).
4. **No create-timing guidance/guardrail** — nothing stops an organizer creating
   days early (wasting credits) or tells them when they _may_ start.

## Locked decisions

| Decision              | Value                                                                                                                            |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Start field           | Structured **date+time (their TZ)** — text `input` `YYYY-MM-DD HH:MM` + `dropdown` timezone (no native calendar in GitHub forms) |
| Timezone dropdown     | Curated **Americas + Europe** IANA zones (+ UTC)                                                                                 |
| Earliest `/create`    | **`start − 12h`** — earlier is rejected with a clear message                                                                     |
| Latest `/create`      | **None** — if they're late, that's on them                                                                                       |
| Lifecycle anchor      | **Workshop window** (shared `shelve_at`/`delete_at`), not per-instance create time                                               |
| Capacity shortfall    | **Partial-create** as many as are free + report; do **not** block                                                                |
| Backfill              | **Auto, default-on**, until target met / parent closed / **workshop end**                                                        |
| All user-facing times | **Organizer's local TZ**, named (e.g. "Sun Jun 7, 8:00 PM EDT"); UTC internal only                                               |

## Parameters to confirm (recommended defaults)

- **Workshop end definition:** `end = start + DURATION_days × 24h` (simple,
  safe; slight tail-idle). _Alternative:_ add an explicit end-time field, or
  assume an end-of-day hour, to trim the tail. **Rec: start + duration×24h.**
- **Grace:** `shelve_at = end + 1h`; `delete_at = end + 24h` (retention for data
  retrieval).
- **Grafana unreachable:** fail-open — skip the gate, attempt the shortfall, let
  backfill catch stragglers.
- **Backfill cadence / per-run cap:** every **15 min**; create at most
  **available** (and respect `MORPHOCLOUD_MAX_TOTAL_INSTANCES`) per run.

## Experiment results & validated backfill design (2026-06-02)

Validated live against the scarce **r3.xl** flavor on BIO180006 (prototype loop
on the runner; the maintainer freed slots on demand):

- **Capture works via periodic attempt-create.** A loop that simply attempts
  `openstack server create` every ~30–40 s and **holds** what succeeds captured
  both freed slots, held them simultaneously, and the 3rd attempt correctly
  failed ("No valid host") at the real ceiling of **2**. **OpenStack is the
  arbiter** — no Grafana gating.
- **Holding (accumulate) is essential.** An earlier delete-on-capture prototype
  was wrong — it just re-grabbed the same free slot and never tested the
  ceiling. The backfill must **hold** captures and accumulate toward target.
- **Grafana** (`js2_availability`) is **start-only / admin-facing** ("~N free
  right now"), never a gate.

**Backfill plan (Phase 4) — re-provision missing sub-issue instances:**

- `workshop-backfill.yml`: `schedule` (~ every 10 min) + `workflow_dispatch`,
  `runs-on: self-hosted` (needs OpenStack to detect live instances).
- For each open `request-type:workshop` + `request:approved` parent, not
  `workflow:creating-instances`, with `now < workshop-end`:
  - `live` = count of sub-issues whose OpenStack instance
    (`<prefix>_instance-<sub#>`) is `ACTIVE`; `shortfall = target − live`.
  - For sub-issues lacking a live instance (up to shortfall, respecting
    `MORPHOCLOUD_MAX_TOTAL_INSTANCES`), **re-dispatch
    `create-instance-from-workflow.yml`** for that sub-issue (the attempt-create
    — succeeds only when a slot is free).
  - Comment on the parent only when `live` changes ("backfilled → Y/N"). Stops:
    target met / parent closed-or-unapproved / `now ≥ workshop-end`.
- **To build together + verify (needs the r3.xl scenario):**
  1. Store a `workshop-end:<epoch>` label at validation (watcher needs it;
     start + duration are available there).
  2. Confirm **`create-instance` is safe to re-dispatch** for a sub-issue whose
     prior attempt failed (it has `check-instance-exists` — verify it cleanly
     retries, no double-provision / partial-state error).
  3. Live-instance detection: OpenStack `server show` (authoritative) vs the
     sub-issue `status:*` label (cheaper).
- **Partial-create at `/create` (Phase 3)** is then minimal — create-workshop
  already creates N sub-issues + dispatches provisioning; capacity-short ones
  just fail and the backfill tops them off. Add messaging ("X of N up; topping
  off the rest as capacity frees").

> **Status 2026-06-02:** Phase 1 (timing core) + the split date/time fields are
> live on Instances. **Phase 2 (lifecycle window-anchoring)** implemented in
> source (commit `643368a`, NOT yet vendorized). Phase 3/4 per the design above
> — to implement + test together.

## Component changes (all in `MorphoCloudWorkflow`, vendorized to Instances/Test-Instances/MC-\*)

### 1. Issue template — `.github/ISSUE_TEMPLATE/03-workshop-request.yml`

Replace the free-text **"Workshop Date(s)"** with:

- `input` **"Workshop start date"** (`YYYY-MM-DD`) + `input` **"Workshop start
  time"** (`HH:MM`, 24-hour) — separate fields — required.
- `dropdown` **"Your timezone"** — curated Americas + Europe IANA zones + UTC —
  required.

Keep `Duration (days)`, `Number of Instances`, flavor, description.

### 2. Validation — `.github/workflows/on-instance-request-opened.yml` (workshop branch, lines ~149+)

Add a step (gated `request-type:workshop`) that:

- Parses start date + TZ → UTC epoch via `zoneinfo`.
- If unparsable / in the past → comment the exact expected format, add
  `needs-fix` label, leave un-approvable.
- On success → store canonical **`start:<utc-epoch>`** and
  **`workshop-target:<N>`** labels (machine-readable; avoids re-parsing the body
  later).

### 3. `/approve` — `.github/workflows/approve-workshop.yml`

On approve, read `start:<epoch>`, compute `start − 12h`, post in the organizer's
local TZ:

> ✅ Approved. You may run `/create` no earlier than **Sun Jun 7, 8:00 PM EDT**.

### 4. `/create` — `.github/workflows/create-workshop.yml`

- **12h gate (first step):** if `now < start − 12h` → reject in local time
  (_"Too early — earliest is …"_), exit. No latest gate.
- **Fix silent failures:** all validation errors post a comment back to the
  issue.
- **Incremental & idempotent:** `existing = parent.sub_issues_summary.total`;
  `shortfall = target − existing`; if `≤0` → "already N/N", stop.
- **Capacity-aware partial create:** `available = check-capacity(flavor)`;
  `make = min(shortfall, available, MAX_TOTAL headroom)`; create `make`; report
  _"created X; now Y/N; Z short — backfill will continue."_
- **Window-anchored lifecycle (per sub-issue):**
  - `shelve_at = end + 1h`, `delete_at = end + 24h` (shared, from the workshop
    window).
  - `timeout_hrs = ceil((shelve_at − instance_boot)/3600)` → `timeout:<n>hrs`.
  - `expiration_days = (delete_at − sub_issue_created_at)/86400` (decimal; the
    deleter supports `bc`) → `expiration:<d>d`.
  - ⇒ initial **and** backfilled instances converge on the same wall-clock end.
- **Lock:** hold `workflow:creating-instances` on the parent during the run
  (already used); backfill skips locked workshops.

### 5. Availability source — `morphocloud-intake`

- New `GET /availability` (gated by `X-Api-Key`, like `/lookup`) →
  `{ "g3.large": 10, ... }` from `js2_availability.get_availability()` (reuses
  existing Grafana logic — single source of truth).

### 6. New action — `.github/actions/check-capacity`

- Inputs: `flavor`, `url`, `api_key`. Calls `/availability`; outputs
  `available`.
- **Fail-open:** unreachable → output `unknown`, caller proceeds optimistically.

### 7. New scheduled workflow — `.github/workflows/workshop-backfill.yml`

- Cron `*/15 * * * *` + `workflow_dispatch`.
- For each open issue with `request-type:workshop` + `request:approved`, **not**
  `workflow:creating-instances`, `sub_issues.total < workshop-target`, and
  `now < end`:
  - `make = min(shortfall, available, MAX_TOTAL headroom)`; if `>0`, create them
    (shared logic, §8).
  - Comment **only on change** ("backfilled 2 → 30/30 ✅ complete").
- Stops automatically: target met / parent closed-or-unapproved / past workshop
  end.

### 8. Refactor — reusable "create N workshop instances"

Extract the sub-issue-creation + window-anchored-lifecycle +
provisioning-dispatch loop from `create-workshop.yml` into a reusable
workflow/action called by **both** `create-workshop` (manual) and
`workshop-backfill` (auto). Single implementation, no drift.

### 9. Repo vars / helper

- New repo var `MORPHOCLOUD_AVAILABILITY_URL`; reuse
  `MORPHOCLOUD_LOOKUP_API_KEY`.
- Small `zoneinfo` helper for local↔UTC conversion and local-time formatting;
  dropdown values must be valid IANA names.

## Phased rollout (each testable on Test-Instances first)

1. **Timing core:** template field + validation + `start:`/`workshop-target:`
   labels + `/approve` display + 12h gate.
2. **Lifecycle anchoring** to the workshop window + fix silent failures.
   _(Verify with the compressed shelve/delete harness.)_
3. **Capacity:** `/availability` endpoint + `check-capacity` action +
   partial-create.
4. **Backfill:** refactor to shared creator (§8) + `workshop-backfill.yml`
   watcher.

## Notes

- Vendorize via `nox` only; validate on `Test-Instances` (BIO240357) before
  `Instances`.
- The same `check-capacity` action can later gate individual/course `/create` (1
  instance each).
