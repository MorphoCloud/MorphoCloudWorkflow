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
- **Naming convention enforcement (course-_, instance-_, workshop-\*) with
  noxfile glob patterns**: Removes per-file bookkeeping but relies on the
  convention being followed. An agent or contributor who creates a new file
  without the prefix silently breaks vendorization. No enforcement mechanism
  short of a curated list — same problem as option A.
- **Noxfile validation that rejects unclassified files**: Errors at vendorize
  time for files not matching a pattern or not in a shared whitelist. Still
  requires maintaining the whitelist. Shifts the failure from silent to loud but
  doesn't prevent it.

### Open question

The core tension: shared workflows exist because the underlying OpenStack
operations (shelve, unshelve, delete, create IP, etc.) are identical for both
instance types. Splitting `control-instance.yml` into
`control-course-instance.yml` and `control-instance-instance.yml` would
eliminate cross-type coupling at the cost of maintaining two copies of the same
shell logic. Whether the duplication cost is worth the isolation benefit is a
judgment call.

---

## Full audit: file classification

### Workflows (35 files)

#### Course-only (5)

| File                           | Key evidence                                             |
| ------------------------------ | -------------------------------------------------------- |
| `validate-command-course.yml`  | Label guard: `request-type:course-instance`              |
| `validate-request-course.yml`  | Called only by `on-course-request-opened`                |
| `on-course-request-opened.yml` | Label guard: `request-type:course-instance`              |
| `create-course-instance.yml`   | Label guard: `request-type:course-instance`              |
| `enroll-students.yml`          | Refs `COURSE_TEAM_SLUG`; trigger: push to `students.txt` |

#### Instance/Workshop-only (14)

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

#### Shared (16)

| File                                 | Key evidence                                                          |
| ------------------------------------ | --------------------------------------------------------------------- |
| `send-email.yml`                     | 3-way dispatch by label (individual/workshop/course)                  |
| `control-instance.yml`               | `/shelve`, `/unshelve`, `/delete_instance`; label: `instance-request` |
| `control-instance-from-workflow.yml` | Called by auto-shelving (both types)                                  |
| `delete-volume.yml`                  | `/delete_volume`; label: `instance-request`                           |
| `delete-volume-from-workflow.yml`    | Called by auto-deleting                                               |
| `delete-instance-and-volume.yml`     | `/delete_all`; label: `instance-request`                              |
| `automatic-instance-shelving.yml`    | Schedule — scans all OpenStack instances                              |
| `automatic-instance-deleting.yml`    | Schedule — scans all expired instances                                |
| `automatic-volume-deleting.yml`      | Schedule — scans all volumes                                          |
| `collect-instance-uptime.yml`        | Schedule — all instances                                              |
| `update-request-status-label.yml`    | Schedule — all issues                                                 |
| `request-initial-comments.yml`       | Reusable; parameterized by `commands_md_path`                         |
| `validate-request.yml`               | Fires on issue edit (no label guard)                                  |
| `labels.yml`                         | Creates labels for all three types                                    |
| `on-admin-mention.yml`               | Admin notification (any issue)                                        |
| `runner-setup-helper.yml`            | Setup docs                                                            |
| `ci.yml`                             | Pre-commit                                                            |

### Actions (22 directories)

#### Course-only (1)

| Directory            | Key evidence                                                             |
| -------------------- | ------------------------------------------------------------------------ |
| `course-send-email/` | Called only by `create-course-instance` and `send-email` (course branch) |

#### Instance/Workshop-only (5)

| Directory                       | Key evidence                                                    |
| ------------------------------- | --------------------------------------------------------------- |
| `check-team-membership/`        | Called only by `on-instance-request-opened`, `approve-workshop` |
| `send-email/`                   | Individual connection email (includes `/renew` in body)         |
| `workshop-send-email/`          | Workshop connection email                                       |
| `send-workshop-email-approval/` | Workshop approval notification                                  |
| `extract-workshop-fields/`      | Workshop-specific field parsing                                 |

#### Shared (16)

| Directory                      | Key evidence                                                   |
| ------------------------------ | -------------------------------------------------------------- |
| `check-approval/`              | Used by `control-instance`, `create-workshop`, `delete-volume` |
| `check-instance-exists/`       | Generic OpenStack query                                        |
| `check-js2-status/`            | Jetstream2 outage check                                        |
| `check-volume-exists/`         | Generic OpenStack volume check                                 |
| `comment-progress/`            | Used by `create-instance` (all types)                          |
| `control-instance/`            | Core shelve/unshelve/delete                                    |
| `create-instance/`             | Core provisioning                                              |
| `create-ip/`                   | Floating IP allocation                                         |
| `define-instance-name/`        | Name formatting                                                |
| `define-volume-name/`          | Name formatting                                                |
| `delete-volume/`               | Volume deletion                                                |
| `extract-issue-fields/`        | Issue body parsing                                             |
| `generate-connection-url/`     | Guacamole URL generation                                       |
| `lookup-email/`                | Email lookup (supports both standard and roster modes)         |
| `retrieve-metadata/`           | Instance IP + passphrase from OpenStack                        |
| `update-approval/`             | Approve/unapprove label management                             |
| `update-request-status-label/` | Status label updates                                           |

### Root files (3)

| File                         | Type                  | Key evidence                                |
| ---------------------------- | --------------------- | ------------------------------------------- |
| `issue-commands.md`          | **Instance/Workshop** | Individual command list (includes `/renew`) |
| `workshop-issue-commands.md` | **Instance/Workshop** | Workshop command list                       |
| `course-issue-commands.md`   | **Course**            | Course command list (no `/renew`)           |

### Vestigial files (vendorized but unused in target)

These are currently vendorized to repos where nothing calls them:

**In course repos (MC-\*) but unused:**

- `create-instance-from-workflow.yml` — called only by `create-workshop`
- `request-labeler.yml` — called only by `on-instance-request-opened`
- `validate-request.yml` — fires on edit, course uses `validate-request-course`
- `check-team-membership/` action
- `send-email/` action
- `workshop-send-email/` action
- `send-workshop-email-approval/` action
- `extract-workshop-fields/` action

**In MorphoCloudInstances but unused:**

- `course-issue-commands.md` — no workflow references it there

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
