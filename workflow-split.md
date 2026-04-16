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

## Files changed summary

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
