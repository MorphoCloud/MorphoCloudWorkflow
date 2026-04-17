# Workflow Split Plan: Course vs Individual/Workshop

## Motivation

The current `MorphoCloudWorkflow` repo serves two distinct instance management
paths that have meaningfully different lifecycles:

- **Individual/Workshop**: user-driven approval, expiration labels, `/renew`
  command, email lookup via Apps Script, workshop sub-issues.
- **Course**: team-membership gating, no renewal, FERPA-safe email delivery,
  instructor access command, enrollment via `students.txt`.

These paths currently share files with internal `if:` branching on issue-type
labels (e.g. `validate-command.yml`, `on-request-opened.yml`). This creates two
risks:

1. A change targeting one path can silently affect the other.
2. Course repos (`MC-*`) vendorized from this workflow repo receive irrelevant
   individual/workshop files (and vice versa for `MorphoCloudInstances`).

The fix is to split branching files into per-type variants and use two `nox`
vendorize sessions to deliver the correct subset to each repo type.

---

## Step 1 — Split `validate-command.yml` into three files

**Current state:** one file with three parallel validation blocks (individual,
workshop, course) and shared failure-comment logic.

**Action:** Delete `validate-command.yml` and create:

### `validate-command-instance.yml`

- Trigger: `issue_comment: created`
- Job `if:` guard: `contains(labels, 'request-type:instance')`
- Supported commands: `/create`, `/shelve`, `/unshelve`, `/email`, `/renew`,
  `/delete_instance`, `/delete_volume`, `/delete_all`
- Failure comment appends `issue-commands.md`

### `validate-command-course.yml`

- Trigger: `issue_comment: created`
- Job `if:` guard: `contains(labels, 'request-type:course-instance')`
- Supported commands: `/create`, `/shelve`, `/unshelve`, `/email`,
  `/delete_instance`, `/delete_volume`, `/delete_all`
- Failure comment appends `course-issue-commands.md`

### `validate-command-workshop.yml`

- Trigger: `issue_comment: created`
- Job `if:` guard: `contains(labels, 'request-type:workshop')`
- Supported commands: `/create`, `/approve`, `/unapprove`
- Failure comment appends `workshop-issue-commands.md`

---

## Step 2 — Split `on-request-opened.yml` into two files

**Current state:** 460-line file with 13 jobs interleaved across all three issue
types, including cross-type quota logic (course count + individual count
contributing to a global cap).

**Action:** Delete `on-request-opened.yml` and create:

### `on-instance-request-opened.yml`

Jobs (all guarded by `request-type:instance` or `request-type:workshop`):

- `check-instance-team-membership`
- `check-instance-quota`
- `call-validate-request`
- `check-workshop-organizer-membership`
- `call-validate-request-workshop`
- `call-request-notify-admin-workshop`
- `call-request-labeler`
- `call-request-initial-comments`
- `rename-instance-issue`

### `on-course-request-opened.yml`

Jobs (all guarded by `request-type:course-instance`):

- `check-course-team-membership`
- `check-course-instance-quota`
- `call-validate-request-course`
- `call-request-initial-comments-course`
- `rename-course-issue`

**Note on quota logic:** The current `on-request-opened.yml` checks a _global_
cap (course + individual open issues combined). After the split, this check must
be preserved in both files:

- `on-instance-request-opened.yml`: when checking individual quota, also count
  open course issues for the same user.
- `on-course-request-opened.yml`: when checking course quota, also count open
  individual issues for the same user. The cross-type counting logic is already
  present in the current code; it just needs to be preserved correctly in each
  split file.

---

## Step 3 — Fix latent bug in `send-renewal-email.yml`

**Current state:** The workflow branches on `is_workshop` (true/false) but has
no awareness of course issues. If called for a course issue it silently falls
through to the individual path, tries to look up a student email via Apps Script
(which will fail — students never registered individually), and sends a renewal
email that course instances cannot act on.

**Action:** At the top of the `send-renewal-email` job, add an explicit guard
step:

```yaml
- name: Skip if course instance
  shell: bash
  run: |
    IS_COURSE=$(gh issue view "$ISSUE_NUMBER" --json labels \
      --jq '[.labels[].name] | index("request-type:course-instance") != null')
    if [[ "$IS_COURSE" == "true" ]]; then
      echo "Course instances are not renewable. Skipping."
      exit 0
    fi
  env:
    ISSUE_NUMBER: ${{ inputs.issue_number }}
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    GH_REPO: ${{ github.repository }}
```

This is a targeted safety fix independent of the broader split.

---

## Step 4 — Update issue templates (bidirectional)

**Current state:** All three issue templates live in `.github/ISSUE_TEMPLATE/`
and are copied wholesale by both vendorize sessions.

### Templates kept per repo type

| Template file                        | `MorphoCloudInstances` (individual/workshop)     | `MC-*` (course)                                  |
| ------------------------------------ | ------------------------------------------------ | ------------------------------------------------ |
| `01-individual-instance-request.yml` | ✅ keep                                          | ❌ exclude                                       |
| `02-course-instance-request.yml`     | ❌ exclude                                       | ✅ keep                                          |
| `03-workshop-request.yml`            | ✅ keep                                          | ❌ exclude                                       |
| `config.yml`                         | ✅ keep (patched: `blank_issues_enabled: false`) | ✅ keep (patched: `blank_issues_enabled: false`) |

**Result:** Users opening an issue in `MorphoCloudInstances` see only Individual
and Workshop templates. Users opening an issue in an `MC-*` repo see only the
Course Instance Request template.

---

## Step 5 — Add `vendorize-course` nox session; update `vendorize`

### New `vendorize-course` session (for `MC-*` repos)

Includes:

- `.github/` (with exclusions below)
- `.pre-commit-config.yaml`
- `course-issue-commands.md`
- `cloud-config`
- `scripts/list-instance-credentials.sh`

Excludes from `.github/`:

- `dependabot.yml`
- `ISSUE_TEMPLATE/01-individual-instance-request.yml`
- `ISSUE_TEMPLATE/03-workshop-request.yml`
- `workflows/on-instance-request-opened.yml`
- `workflows/validate-command-instance.yml`
- `workflows/validate-command-workshop.yml`
- `workflows/create-instance.yml`
- `workflows/create-workshop.yml`
- `workflows/approve-workshop.yml`
- `workflows/update-workshop.yml`
- `workflows/test-workshop-deletion.yml`
- `workflows/request-notify-admin.yml`
- `workflows/request-notify-admin-workshop.yml`
- `workflows/send-email.yml`
- `workflows/update-renew-label.yml`
- `workflows/send-renewal-email.yml`
- `workflows/validate-request.yml`
- `workflows/request-labeler.yml`

Also excludes from root:

- `issue-commands.md`
- `workshop-issue-commands.md`

### Updated `vendorize` session (for `MorphoCloudInstances`/Test)

Add to existing excludes:

- `ISSUE_TEMPLATE/02-course-instance-request.yml`
- `workflows/on-course-request-opened.yml`
- `workflows/validate-command-course.yml`
- `workflows/validate-request-course.yml`
- `workflows/create-course-instance.yml`
- `workflows/instructor-access.yml`
- `workflows/enroll-students.yml`

Add to existing includes:

- `course-issue-commands.md` (needed because `MorphoCloudInstances` handles
  course issues opened there — e.g. admin-created issues — and the initial
  comments workflow references it)

---

## Step 6 — Run pre-commit validation

For every new or modified file:

```bash
cd ~/Desktop/Projects/MorphoCloudWorkflow
pre-commit run --files \
  .github/workflows/validate-command-instance.yml \
  .github/workflows/validate-command-course.yml \
  .github/workflows/validate-command-workshop.yml \
  .github/workflows/on-instance-request-opened.yml \
  .github/workflows/on-course-request-opened.yml \
  .github/workflows/send-renewal-email.yml \
  noxfile.py
```

`actionlint` (already in `.pre-commit-config.yaml` as of April 2026) will
validate all workflow YAML beyond what `check-yaml` and `check-github-actions`
catch.

---

## Step 7 — Vendorize to MorphoCloudInstancesTest

```bash
cd ~/Desktop/Projects/MorphoCloudWorkflow
pipx run nox -s vendorize -- ~/Desktop/Projects/MorphoCloudInstancesTest/ --commit
```

Verify:

- `MorphoCloudInstancesTest/.github/workflows/` has no course-only files
- `MorphoCloudInstancesTest/.github/ISSUE_TEMPLATE/` has no
  `02-course-instance-request.yml`
- All three `validate-command-*.yml` variants are present (individual, workshop,
  course) since `MorphoCloudInstances` handles all three paths

---

## Implementation status (April 2026)

Steps 1–7 are **complete** on branch `implement-workflow-split`.

| Step                                                             | Status                             |
| ---------------------------------------------------------------- | ---------------------------------- |
| 1. Split `validate-command.yml` → 3 type-specific files          | ✅ Done                            |
| 2. Split `on-request-opened.yml` → 2 files                       | ✅ Done                            |
| 3. Course guard in `send-renewal-email.yml`                      | ✅ Done                            |
| 4. Bidirectional issue template exclusions                       | ✅ Done                            |
| 5. `vendorize-course` nox session + updated `vendorize` excludes | ✅ Done                            |
| 6. Pre-commit / actionlint validation                            | ✅ Done                            |
| 7. Vendorize to test repos                                       | ✅ Done (CourseTemplate + MC-U-AL) |

### Additional changes made during testing (beyond original plan)

- Added `COURSE_INSTRUCTOR_GITHUB` to allowlists in `control-instance.yml`,
  `create-course-instance.yml`, `send-email.yml`, `delete-volume.yml`,
  `delete-instance-and-volume.yml` so course instructors can run commands on
  student issues.
- Added email masking (`::add-mask::`) and removed raw API response logging in
  `lookup-email/action.yml` to prevent student emails from appearing in action
  logs.
- Added URL-to-ID extraction regex in `lookup-email/action.yml` so
  `MORPHOCLOUD_STUDENT_ROSTER_SHEET_ID` accepts either a full Google Sheets URL
  or a bare sheet ID.
- Removed `01-individual-instance-request.yml` and `03-workshop-request.yml`
  from `MorphoCloudCourseTemplate`.
- Removed "Course Repository Configuration" section from
  `course-issue-commands.md`.
- `_patch_files_course()` strips `--key-name` from `create-instance/action.yml`
  for course repos (no admin keypairs in allocations).

---

## Remaining problem: shared-file coupling

The original split addressed the most obvious branching files
(`validate-command.yml`, `on-request-opened.yml`), but **16 shared workflows and
16 shared actions still go to both repo types**. This means:

1. A change to `send-email.yml` targeting the course path can silently break the
   individual path (or vice versa) — the exact risk the split was supposed to
   eliminate.
2. Nine files are vendorized to repos where they are never called (dead weight
   that obscures what's actually active).

### Approaches considered and rejected

- **Allow-list instead of deny-list in noxfile**: Flips the failure mode
  (forgotten file is excluded vs. included) but doesn't fix the real problem — a
  solo maintainer still has to remember to update a list.
- **Naming convention enforcement (course-\*, instance-\*, workshop-\*) with
  noxfile glob patterns**: Removes per-file bookkeeping but relies on the
  convention being followed. An agent or contributor who creates a new file
  without the prefix silently breaks vendorization. No enforcement mechanism
  short of a curated list — same problem as option A.
- **Noxfile validation that rejects unclassified files**: Errors at vendorize
  time for files not matching a pattern or not in a shared whitelist. Still
  requires maintaining the whitelist. Shifts the failure from silent to loud but
  doesn't prevent it.
- **Splitting shared workflows into type-specific copies within the same repo**
  (e.g. `control-instance.yml` → `control-course-instance.yml` +
  `control-instance-instance.yml`): Still requires filtering during
  vendorization — the curated exclude list grows instead of shrinking. Doesn't
  solve the fundamental problem of maintaining lists.

All of these approaches shift bookkeeping from one form to another. The root
cause remains: **a single repo serving two instance types requires filtering at
vendorize time, and any filtering mechanism requires a maintained list.**

---

## Proposed next phase: hierarchical repo split

### Architecture

Eliminate vendorize filtering entirely by splitting into three repos with a
hierarchical vendorize chain:

```
MorphoCloudSharedWorkflow          (generic OpenStack actions + workflows)
  ├── vendorize → MorphoCloudInstanceWorkflow   (+ individual/workshop-specific files)
  └── vendorize → MorphoCloudCourseWorkflow     (+ course-specific files)

MorphoCloudInstanceWorkflow        → vendorize → MorphoCloudInstances / MorphoCloudInstancesTest
MorphoCloudCourseWorkflow          → vendorize → MorphoCloudCourseTemplate → (fork to MC-* repos)
```

Each vendorize step is a **wholesale copy** — no exclude lists, no include
lists, no filtering. Every file in the source repo belongs in the target.

### Key properties

- **Course-specific changes** (most common): commit in
  `MorphoCloudCourseWorkflow` → vendorize to `MorphoCloudCourseTemplate`. One
  commit, one vendorize. No other repo touched.
- **Instance/workshop-specific changes**: commit in
  `MorphoCloudInstanceWorkflow` → vendorize to `MorphoCloudInstances`. One
  commit, one vendorize.
- **Shared OpenStack changes** (rare — infrastructure is stable): commit in
  `MorphoCloudSharedWorkflow` → vendorize to both type-specific workflow repos →
  vendorize each to production targets. One commit, four vendorize runs.
- **New file creation is inherently safe**: a file created in
  `MorphoCloudCourseWorkflow` can never appear in `MorphoCloudInstances` because
  they are separate repos. No naming convention, glob pattern, or curated list
  required.

### Vendorize scripts

Each type-specific workflow repo includes a vendorize nox session that
**automatically pulls from the shared repo first**, then copies everything to
the target. This means the maintainer never needs to manually run the shared
vendorize step — it's built into the type-specific vendorize command:

```bash
# In MorphoCloudCourseWorkflow:
cd ~/Desktop/Projects/MorphoCloudCourseWorkflow
pipx run nox -s vendorize -- ~/Desktop/Projects/MorphoCloudCourseTemplate/ --commit

# This internally:
# 1. Vendorizes MorphoCloudSharedWorkflow → MorphoCloudCourseWorkflow (updates shared files)
# 2. Copies everything in MorphoCloudCourseWorkflow → MorphoCloudCourseTemplate
```

Same pattern for `MorphoCloudInstanceWorkflow`.

### Production targets

- **`MorphoCloudCourseTemplate`** is the primary vendorize target for course
  workflows. New course repos (`MC-*`) are created by `mc-course-intake.gs` as
  forks/copies of this template — vendorization is not part of the GitHub
  Actions pipeline.
- Direct vendorize to a live `MC-*` repo is only done for immediate hotfixes or
  real-time testing — not the normal flow.
- **`MorphoCloudInstances`** (and `MorphoCloudInstancesTest`) is the primary
  vendorize target for individual/workshop workflows.

### Files that need to be split (type branching)

Five files currently contain type-specific branching logic (checking labels or
dispatching to different actions based on instance type). In the hierarchical
model, these cannot live in the shared repo. Each gets split into type-specific
variants that live in the appropriate type-specific workflow repo:

| Current file (shared)             | Branching logic                                                                                                     | Course variant (→ CourseWorkflow) | Instance variant (→ InstanceWorkflow) |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------- | --------------------------------- | ------------------------------------- |
| `send-email.yml`                  | 3-way dispatch: calls `send-email/`, `workshop-send-email/`, or `course-send-email/` based on label                 | Course-only email dispatch        | Individual + workshop email dispatch  |
| `automatic-instance-deleting.yml` | Workshop: skips renewal email, closes sub-issues, cascades to parent issue                                          | Simple expiration (no renewal)    | Full renewal + workshop cascade       |
| `validate-request.yml`            | Different success comments for individual vs workshop                                                               | Course-specific validation        | Individual + workshop validation      |
| `control-instance/` action        | On unshelve: sends email via `send-email/` (individual) or `course-send-email/` (course) based on `roster_sheet_id` | Always uses `course-send-email/`  | Always uses `send-email/`             |
| `lookup-email/` action            | Branches on `roster_sheet_id`: calls `?action=lookup_course_roster` (course) vs `?action=lookup` (individual)       | Always uses roster lookup         | Always uses standard lookup           |

### Files that reference type but don't branch

These files reference type-specific variables (e.g. `COURSE_INSTRUCTOR_GITHUB`
in allowlists) but don't change behavior. They can stay in the shared repo —
empty variables resolve to empty strings harmlessly:

- `control-instance.yml` — `COURSE_INSTRUCTOR_GITHUB` in allowlist
- `control-instance-from-workflow.yml` — accepts `roster_sheet_id` input, passes
  through
- `delete-volume.yml` — `COURSE_INSTRUCTOR_GITHUB` in allowlist
- `delete-instance-and-volume.yml` — `COURSE_INSTRUCTOR_GITHUB` in allowlist
- `labels.yml` — creates labels for all types unconditionally
- `runner-setup-helper.yml` — derives course name from repo name

### Exosphere and cloud-config

The `bump-exosphere` nox session and `cloud-config` live in the shared repo.
Exosphere bumps propagate automatically through the vendorize chain — the
type-specific repos' vendorize scripts pull from shared first.

### Downsides and mitigations

| Downside                                             | Mitigation                                                                                                                                                 |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Shared OpenStack bug must be fixed in SharedWorkflow | Discipline: shared changes go upstream. The vendorize script pulls shared first, so the fix propagates automatically on the next type-specific vendorize.  |
| Direct edits to shared files in type-specific repos  | Next shared vendorize overwrites local changes — forces the fix upstream. This is intentional: it prevents drift.                                          |
| Three repos instead of one                           | Day-to-day work only touches one repo (the type-specific one). The shared repo is touched only for OpenStack infrastructure changes, which are infrequent. |
| Exosphere bumps require vendorize propagation        | Built into the vendorize script — one command in each type-specific repo handles it.                                                                       |

---

## Full audit: file classification

### Workflows (35 files)

#### Course-only (5) → MorphoCloudCourseWorkflow

| File                           | Key evidence                                             |
| ------------------------------ | -------------------------------------------------------- |
| `validate-command-course.yml`  | Label guard: `request-type:course-instance`              |
| `validate-request-course.yml`  | Called only by `on-course-request-opened`                |
| `on-course-request-opened.yml` | Label guard: `request-type:course-instance`              |
| `create-course-instance.yml`   | Label guard: `request-type:course-instance`              |
| `enroll-students.yml`          | Refs `COURSE_TEAM_SLUG`; trigger: push to `students.txt` |

#### Instance/Workshop-only (14) → MorphoCloudInstanceWorkflow

| File                                | Key evidence                            |
| ----------------------------------- | --------------------------------------- |
| `validate-command-instance.yml`     | Label guard: `request-type:instance`    |
| `validate-command-workshop.yml`     | Label guard: `request-type:workshop`    |
| `on-instance-request-opened.yml`    | Guards on instance + workshop labels    |
| `create-instance.yml`               | Label guard: `request-type:instance`    |
| `create-instance-from-workflow.yml` | Called only by `create-workshop`        |
| `create-workshop.yml`               | Label guard: `request-type:workshop`    |
| `approve-workshop.yml`              | Label guard: `request-type:workshop`    |
| `update-workshop.yml`               | Workshop completion scheduling          |
| `test-workshop-deletion.yml`        | Test workflow for workshops             |
| `request-notify-admin.yml`          | Individual instance admin email         |
| `request-notify-admin-workshop.yml` | Workshop admin email                    |
| `update-renew-label.yml`            | `/renew` — course instances can't renew |
| `send-renewal-email.yml`            | Explicit course-skip guard              |
| `request-labeler.yml`               | Called only by instance/workshop flows  |

#### Truly generic (8) → MorphoCloudSharedWorkflow

| File                              | Key evidence                                            |
| --------------------------------- | ------------------------------------------------------- |
| `automatic-instance-shelving.yml` | Schedule — scans all OpenStack instances; no type logic |
| `automatic-volume-deleting.yml`   | Schedule — scans all volumes; no type logic             |
| `collect-instance-uptime.yml`     | Schedule — all instances; no type logic                 |
| `update-request-status-label.yml` | Schedule — all issues; no type logic                    |
| `request-initial-comments.yml`    | Reusable; parameterized by `commands_md_path`           |
| `on-admin-mention.yml`            | Admin notification (any issue)                          |
| `runner-setup-helper.yml`         | Setup docs                                              |
| `ci.yml`                          | Pre-commit                                              |

#### References type but doesn't branch (5) → MorphoCloudSharedWorkflow

| File                                 | Key evidence                                                   |
| ------------------------------------ | -------------------------------------------------------------- |
| `control-instance.yml`               | `COURSE_INSTRUCTOR_GITHUB` in allowlist; empty var is harmless |
| `control-instance-from-workflow.yml` | Accepts `roster_sheet_id` input, passes through                |
| `delete-volume.yml`                  | `COURSE_INSTRUCTOR_GITHUB` in allowlist                        |
| `delete-volume-from-workflow.yml`    | Called by auto-deleting; no type logic                         |
| `delete-instance-and-volume.yml`     | `COURSE_INSTRUCTOR_GITHUB` in allowlist                        |
| `labels.yml`                         | Creates labels for all types unconditionally                   |

#### Has type branching — must be split (3)

| File                              | Branching logic                    | Course variant → CourseWorkflow | Instance variant → InstanceWorkflow |
| --------------------------------- | ---------------------------------- | ------------------------------- | ----------------------------------- |
| `send-email.yml`                  | 3-way dispatch by label            | Course-only email dispatch      | Individual + workshop dispatch      |
| `automatic-instance-deleting.yml` | Workshop sub-issue close + cascade | Simple expiration               | Full renewal + workshop cascade     |
| `validate-request.yml`            | Different success comments by type | Course validation message       | Individual + workshop messages      |

### Actions (22 directories)

#### Course-only (1) → MorphoCloudCourseWorkflow

| Directory            | Key evidence                                                             |
| -------------------- | ------------------------------------------------------------------------ |
| `course-send-email/` | Called only by `create-course-instance` and `send-email` (course branch) |

#### Instance/Workshop-only (5) → MorphoCloudInstanceWorkflow

| Directory                       | Key evidence                                                    |
| ------------------------------- | --------------------------------------------------------------- |
| `check-team-membership/`        | Called only by `on-instance-request-opened`, `approve-workshop` |
| `send-email/`                   | Individual connection email (includes `/renew` in body)         |
| `workshop-send-email/`          | Workshop connection email                                       |
| `send-workshop-email-approval/` | Workshop approval notification                                  |
| `extract-workshop-fields/`      | Workshop-specific field parsing                                 |

#### Truly generic (15) → MorphoCloudSharedWorkflow

| Directory                      | Key evidence                                                   |
| ------------------------------ | -------------------------------------------------------------- |
| `check-approval/`              | Used by `control-instance`, `create-workshop`, `delete-volume` |
| `check-instance-exists/`       | Generic OpenStack query                                        |
| `check-js2-status/`            | Jetstream2 outage check                                        |
| `check-volume-exists/`         | Generic OpenStack volume check                                 |
| `comment-progress/`            | Used by `create-instance` (all types)                          |
| `create-instance/`             | Core provisioning                                              |
| `create-ip/`                   | Floating IP allocation                                         |
| `define-instance-name/`        | Name formatting                                                |
| `define-volume-name/`          | Name formatting                                                |
| `delete-volume/`               | Volume deletion                                                |
| `extract-issue-fields/`        | Issue body parsing                                             |
| `generate-connection-url/`     | Guacamole URL generation                                       |
| `retrieve-metadata/`           | Instance IP + passphrase from OpenStack                        |
| `update-approval/`             | Approve/unapprove label management                             |
| `update-request-status-label/` | Status label updates                                           |

#### Has type branching — must be split (2)

| Directory           | Branching logic                                                   | Course variant → CourseWorkflow  | Instance variant → InstanceWorkflow |
| ------------------- | ----------------------------------------------------------------- | -------------------------------- | ----------------------------------- |
| `control-instance/` | Unshelve: dispatches to `send-email/` or `course-send-email/`     | Always uses `course-send-email/` | Always uses `send-email/`           |
| `lookup-email/`     | Branches on `roster_sheet_id` for different Apps Script endpoints | Always uses roster lookup        | Always uses standard lookup         |

### Root files (3)

| File                         | Destination          | Key evidence                                |
| ---------------------------- | -------------------- | ------------------------------------------- |
| `issue-commands.md`          | **InstanceWorkflow** | Individual command list (includes `/renew`) |
| `workshop-issue-commands.md` | **InstanceWorkflow** | Workshop command list                       |
| `course-issue-commands.md`   | **CourseWorkflow**   | Course command list (no `/renew`)           |

### Other shared root files → MorphoCloudSharedWorkflow

| File                      | Key evidence                                     |
| ------------------------- | ------------------------------------------------ |
| `.pre-commit-config.yaml` | Shared linting config                            |
| `cloud-config`            | OpenStack cloud-init template                    |
| `scripts/`                | `list-instance-credentials.sh` — generic utility |

---

## Files changed summary (original plan — completed)

| File                                               | Action                                                           |
| -------------------------------------------------- | ---------------------------------------------------------------- |
| `.github/workflows/validate-command.yml`           | **Delete**                                                       |
| `.github/workflows/validate-command-instance.yml`  | **Create**                                                       |
| `.github/workflows/validate-command-course.yml`    | **Create**                                                       |
| `.github/workflows/validate-command-workshop.yml`  | **Create**                                                       |
| `.github/workflows/on-request-opened.yml`          | **Delete**                                                       |
| `.github/workflows/on-instance-request-opened.yml` | **Create**                                                       |
| `.github/workflows/on-course-request-opened.yml`   | **Create**                                                       |
| `.github/workflows/send-renewal-email.yml`         | **Modify** (add course guard)                                    |
| `noxfile.py`                                       | **Modify** (add `vendorize-course`; update `vendorize` excludes) |
| `workflow-split.md`                                | **Delete after completion** (this file)                          |

---

## Architectural options evaluated and rejected (April 2026)

The shared-file coupling problem (16 shared workflows + 16 shared actions going
to both repo types via exclude lists) prompted evaluation of several
architectural alternatives. All were rejected. This section documents them to
prevent re-evaluation.

### 1. Allow-list instead of deny-list in noxfile

Flip the vendorize logic: explicitly list files to **include** instead of
exclude. A forgotten file is excluded (silent omission) rather than included
(silent pollution).

**Rejected:** Still a curated list. Shifts the failure mode but doesn't
eliminate bookkeeping. A solo maintainer must still update the list for every
new file.

### 2. Naming convention enforcement with glob patterns

Prefix files by type (`course-*.yml`, `instance-*.yml`). Noxfile uses glob
patterns to auto-filter — no per-file list.

**Rejected:** No enforcement mechanism. An agent or contributor who creates a
file without the prefix silently breaks vendorization. The convention relies on
discipline, not tooling — the exact thing that fails with a solo maintainer.

### 3. Noxfile validation that rejects unclassified files

Error at vendorize time if any file doesn't match a known pattern or isn't in a
whitelist.

**Rejected:** Makes failure loud instead of silent, but the whitelist is the
same curated list as option 1. Shifts notification timing, not the root problem.

### 4. Splitting shared workflows into type-specific copies in the same repo

E.g. `control-instance.yml` → `control-course-instance.yml` +
`control-individual-instance.yml`. Both live in the same repo.

**Rejected:** The exclude list grows instead of shrinking — now there are more
files to filter. Doesn't solve the fundamental problem of maintaining lists.

### 5. Hierarchical three-repo split

Three repos: `MorphoCloudSharedWorkflow` → `MorphoCloudCourseWorkflow` /
`MorphoCloudInstanceWorkflow` → production targets. Each vendorize step is
wholesale copy (no lists). 5 files with type-branching logic get split into
type-specific variants.

**Rejected:** Eliminates filtering but adds 2 extra repos. A shared
infrastructure change (rare but important) requires one commit + four vendorize
runs. Day-to-day work only touches one repo, but the cognitive overhead of three
workflow repos and understanding which file goes where is high for a solo
maintainer. The current number of shared files with actual branching logic (5)
is small enough that the exclude-list approach is manageable.

### 6. Git subtree (shared repo as git remote)

Add the shared repo as a git remote and use `git subtree pull` to merge shared
files into each type-specific repo.

**Rejected:** GitHub requires workflow files at `.github/workflows/` and actions
at `.github/actions/`. Subtree is designed for distinct subdirectories, not
overlapping paths. Merging a subtree into `.github/` creates conflicts with
local type-specific files in the same directory tree. Not viable.

### 7. Full cross-repo references (no vendorizing)

Make `MorphoCloudSharedWorkflow` public. Reference shared actions via
`uses: MorphoCloud/MorphoCloudSharedWorkflow/.github/actions/foo@main` and
shared workflows via reusable `workflow_call`. Each consumer has thin trigger
wrappers (~8 lines) that delegate to shared workflows.

**Rejected:** No staging or testing isolation. A broken push to the shared repo
breaks every consumer simultaneously — all MC-\* repos and MorphoCloudInstances.
Cannot test a shared action change in one repo before it hits all of them.
SHA-pinning (`@abc123`) provides version control but reintroduces bookkeeping
(updating SHAs across repos). Also requires plumbing `inputs:` / `outputs:` /
`secrets: inherit` through wrappers, which gets verbose for complex multi-job
workflows.

### 8. Hybrid: cross-repo for shared actions + vendorize for workflows

Use cross-repo `uses:` references for the 15 stable shared actions (OpenStack
operations rarely change). Continue vendorizing shared workflows (which change
more often and benefit from staged rollout to test repos first).

**Rejected:** Only partially solves the problem. Shared actions stop drifting,
but shared workflows — the part that actually changes — still need exclude-list
vendorizing. Mixing two deployment models (cross-repo refs + vendorize) adds
complexity without eliminating the core bookkeeping for the files that matter
most. The benefit (15 stable actions can't drift) doesn't justify the cost
(every workflow file must use fully-qualified cross-repo action paths, making
them harder to read and test locally).

### Conclusion

The current approach — **vendorize with exclude lists** — is imperfect but
understood and stable. The exclude lists are effectively frozen: new shared
workflows and actions are rare because the OpenStack infrastructure layer is
mature. The 5 files with type-branching logic are well-documented (see audit
above). The risk of a silent cross-type break exists but is manageable with
careful review. No alternative evaluated eliminates bookkeeping without
introducing worse trade-offs.
