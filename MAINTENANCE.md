# MorphoCloudWorkflow Maintenance Guide

This document provides guidelines for maintainers of MorphoCloudWorkflow,
covering tasks such as vendoring updated MorphoCloud workflows, updating Ansible
scripts, and modifying the version of Slicer installed on instances.

> [!IMPORTANT]
>
> **Prerequisites:** Most maintenance steps require `pipx` to be installed on
> your system. `pipx` enables execution of Python package applications without
> manual installation management. See
> [pipx documentation](https://pipx.pypa.io/).

> [!IMPORTANT]
>
> **Source of truth.** All workflow/action changes are made **here in
> `MorphoCloudWorkflow`** and reach `Instances`/`Test-Instances` (`vendorize`)
> and `MorphoCloudCourseTemplate` + each `MC-*` repo (`vendorize-course`) **only
> by vendorize** — never the reverse. Do **not** hand-edit `.github/workflows/`
> or `.github/actions/` in a target repo or the template; the next vendorize
> silently reverts it. `MC-*` repos are a one-time frozen snapshot of the
> template at creation, so a fix reaches an existing course repo only by
> re-running `vendorize-course` **into that repo**. See SYSTEM-OVERVIEW.md →
> Workflows.

## Changing MorphoCloudWorkflow (PR flow)

`main` is **protected** (since 2026-06-10): every change — including the
maintainers' — lands via a pull request with the **Format** check green. Direct
pushes are rejected (`GH006`), force-pushes and deletions are blocked, history
is linear, and the rules apply to admins too. Auto-merge is enabled, so the
standard loop is:

```bash
git switch -c <branch>
# ...edit, commit (pre-commit runs locally)...
git push -u origin HEAD
gh pr create --fill
gh pr merge --squash --auto   # merges itself when Format passes
```

The vendorize sessions are unaffected — they push to the _target_ repos
(`Instances`, `Test-Instances`, `MorphoCloudCourseTemplate`, `MC-*`), which are
private and unprotected.

### Claude PR reviewer

Two workflows (added 2026-06-09, auth = `CLAUDE_CODE_OAUTH_TOKEN` repo secret
from the maintainer's Claude subscription — no per-token bill; Actions minutes
are free on this public repo):

- **`claude-code-review.yml`** — automatically reviews every human-authored PR
  on open and when a draft is marked ready-for-review (Dependabot/bot PRs
  skipped). The prompt targets this repo's real bug classes: Actions
  expressions, shell quoting, self-hosted-runner assumptions, vendorize blast
  radius.
- **`claude.yml`** — on-demand: comment `@claude <ask>` on any PR/issue. Gated
  to repo owners / org members / collaborators (public repo).

Notes:

- Both workflows are **excluded from vendorize** (see `noxfile.py`) — the secret
  exists only here, and downstream repos take direct pushes. Keep the exclusions
  when editing the session lists.
- On a PR that **adds or edits** a claude workflow file, the auto-review job
  exits early with "Workflow validation failed … must have identical content to
  the default branch". That is a **security feature** (a PR cannot modify the
  review workflow and run it with the secret), not breakage — it arms once the
  file is on `main`.

### Stuck Dependabot PRs

Branch protection no longer requires PR branches to be up to date with `main`
(the `strict` flag was dropped 2026-06-10), so Dependabot PRs rarely get stuck
anymore. If one does (e.g. a real merge conflict after `main` moves):

1. Optionally comment `@claude review` first (worth it on major bumps).
2. Comment **`@dependabot rebase`** — Dependabot recreates the branch on current
   `main` and CI re-runs fresh.
3. `gh pr merge <n> --squash --auto` — merges when Format passes.

## Vendoring `MorphoCloudWorkflow`

Once an ACCESS allocation and associated target GitHub (runner and repository)
are set up according to
[these instructions](https://github.com/MorphoCloud/MorphoCloudWorkflow/blob/main/README.md),
follow these steps to vendor or re-vendor the GitHub Workflow scripts.

### Example: Vendoring to `Instances`

```bash
PROJECTS_DIR=/home/jcfr/Projects
cd $PROJECTS_DIR

# Clone the target GitHub repository
git clone https://github.com/MorphoCloud/Instances

# Clone MorphoCloudWorkflow
git clone https://github.com/MorphoCloud/MorphoCloudWorkflow

cd MorphoCloudWorkflow

# Vendor scripts
pipx run nox -s vendorize -- $PROJECTS_DIR/Instances/ --commit

# Publish updated target repository
cd $PROJECTS_DIR/Instances
git push origin main
```

This runs the `vendorize` nox session, copying the relevant files to the target
directory and creating a commit with relevant details.

**Example Commit Message:**

```bash
fix: Update to https://github.com/MorphoCloud/MorphoCloudWorkflow/commit/4c7e076

List of MorphoCloudWorkflow changes:

$ git shortlog 726d1fe..4c7e076 --no-merges
Jean-Christophe Fillion-Robin (6):
      fix(send-renewal-email): Fix comment ensuring instance name is defined
      fix(automatic-instance-deleting): Workaround GitHub command limitation
      feat: Associate "volume:deleted" label after volume deletion
      feat(deletion): Decouple instance and volume deletion with grace period
      feat(automatic-volume-deleting): Support specifying expiration grace period
      fix(control-instance-from-workflow): Use current command name in comment

See https://github.com/MorphoCloud/MorphoCloudWorkflow/compare/726d1fe..4c7e076
```

> [!TIP]
>
> Inspect the nox sessions specific to this project in
> [noxfile.py](https://github.com/MorphoCloud/MorphoCloudWorkflow/blob/main/noxfile.py).
> Learn more about nox at https://nox.thea.codes/.

## Vendoring `exosphere`

Ansible scripts for MorphoCloud instances are maintained in our fork of
`exosphere` (https://github.com/MorphoCloud/exosphere).

### Steps to Update Ansible Scripts

1. Identify the version of the `exosphere` used in `MorphoCloudWorkflow`
2. Update the `exosphere` version in `MorphoCloudWorkflow`
3. Vendor the updated `MorphoCloudWorkflow` into the target repository (e.g.,
   `Instances` or `Test-Instances`)

### Example: Vendoring to `Instances`

**Step 1**: Identify the version of `exosphere` used in `MorphoCloudWorkflow`

```
PROJECTS_DIR=/home/jcfr/Projects
cd $PROJECTS_DIR
cd MorphoCloudWorkflow
pipx run nox -s display-exosphere-version
exosphere_branch=$(pipx run nox -s display-exosphere-version 2>&1 | grep -o 'morpho-cloud[^]]*')
echo $exosphere_branch
```

**Step 2**: Update `exosphere` in `MorphoCloudWorkflow`

> `main` is a protected branch (required CI + no direct pushes, admins included
> — since 2026-06-10), so the bump lands via a pull request. The `--branch` flag
> does the branching for you.

```bash
cd $PROJECTS_DIR

# Clone exosphere and check out the desired branch
git clone https://github.com/MorphoCloud/exosphere -b $exosphere_branch

cd MorphoCloudWorkflow

# Update exosphere version in cloud-config and commit on a new
# update-to-exosphere-<sha> branch
pipx run nox -s bump-exosphere -- $PROJECTS_DIR/exosphere --commit --branch

# Open the PR; merge it once the "Format" check is green
git push -u origin HEAD
gh pr create --fill
gh pr merge --squash --auto
```

**Step 3:** Vendor changes into the `Instances` target repository

```
cd $PROJECTS_DIR

# Clone the target GitHub repository
git clone https://github.com/MorphoCloud/Instances.git

cd MorphoCloudWorkflow

# Vendor scripts
pipx run nox -s vendorize -- $PROJECTS_DIR/Instances/ --commit

# Push updates to target repository
cd $PROJECTS_DIR/Instances
git push origin main
```

## Custom Course Software (per-course exosphere image)

A course needing a software suite different from the standard SlicerMorph image
is handled by a `course/<slug>` branch of `exosphere` plus **one repo variable**
on the course repo — `MORPHOCLOUD_EXOSPHERE_REF` (full commit SHA, validated at
`/create` time). No MorphoCloudWorkflow branch, no `cloud-config` edit, and the
variable survives `vendorize-course`.

Full procedure (setup, validation, mid-course updates, branch lifetime,
between-term re-baseline):
[course-software-customization.md](course-software-customization.md).

Which courses are customized — the variable doubles as the registry:

```bash
for r in $(gh repo list MorphoCloud --json name --jq '.[].name | select(startswith("MC-"))'); do
  echo -n "$r: "
  gh variable get MORPHOCLOUD_EXOSPHERE_REF --repo "MorphoCloud/$r" 2>/dev/null || echo "(standard)"
done
```

## Updating Slicer Version

The version of Slicer is defined in the Ansible script maintained in `exosphere`
(https://github.com/MorphoCloud/exosphere) in the file
[ansible/roles/slicer/vars/main.yml][ansible-slicer-vars-main-yml].

### Example: Updating Slicer Version

**Step 1**: Identify the version of `exosphere` branch used in

```bash
cd MorphoCloudWorkflow
pipx run nox -s display-exosphere-version
exosphere_branch=$(pipx run nox -s display-exosphere-version 2>&1 | grep -o 'morpho-cloud[^]]*')
echo $exosphere_branch
```

**Step 2:** Update the Slicer version in the `exosphere` Ansible script:

```bash
cd $PROJECTS_DIR

# Clone exosphere and check out the appropriate branch
git clone https://github.com/MorphoCloud/exosphere.git -b $exosphere_branch

cd $PROJECTS_DIR/exosphere

# Modify ansible/roles/slicer/vars/main.yml to update Slicer version
```

Example commit (see
[MorphoCloud/exosphere@a939cb9aa](https://github.com/MorphoCloud/exosphere/commit/a939cb9aa05ff91e106bcf8d4e438e8c0358c773)):

```diff
  slicer_version:
    major: 5
-   minor: 7
+   minor: 8
    patch: 0

- slicer_release_id: "33153"
+ slicer_release_id: "33216"
```

```bash
git add ansible/roles/slicer/vars/main.yml

# Commit the change. For example:
git commit -m "feat(Slicer role): Download Slicer 5.8.0 r33216 instead of 5.7.0 r33153"

# Push changes to exosphere
git push origin $exosphere_branch
```

**Step 3**: Update `exosphere` in `MorphoCloudWorkflow` and vendor changes in
target repository.

```bash
cd $PROJECTS_DIR/MorphoCloudWorkflow

# Update exosphere version in cloud-config and commit on a new branch
# (main is protected — see "Vendoring exosphere" Step 2)
pipx run nox -s bump-exosphere -- $PROJECTS_DIR/exosphere --commit --branch
git push -u origin HEAD
gh pr create --fill
gh pr merge --squash --auto

# After the PR merges: vendor scripts from updated main
git switch main && git pull
pipx run nox -s vendorize -- $PROJECTS_DIR/Instances/ --commit

# Publish updated target repository
cd $PROJECTS_DIR/Instances
git push origin main
```

## Updating Slicer Extensions

Slicer extensions are defined in
[ansible/roles/slicer/vars/main.yml][ansible-slicer-vars-main-yml] and
dependencies in
[ansible/roles/slicer/files/slicer-install-extension-dependencies.py][slicer-install-extension-dependencies-py].

To update:

1. Modify `ansible/roles/slicer/vars/main.yml`
2. Update dependencies if needed in `slicer-install-extension-dependencies.py`
3. Follow the same workflow for bumping `exosphere` and vendoring changes into
   the target repository.

[ansible-slicer-vars-main-yml]:
  https://github.com/MorphoCloud/exosphere/blob/morpho-cloud-portal-2026.06-ubuntu24-prep/ansible/roles/slicer/vars/main.yml
[slicer-install-extension-dependencies-py]:
  https://github.com/MorphoCloud/exosphere/blob/morpho-cloud-portal-2026.06-ubuntu24-prep/ansible/roles/slicer/files/slicer-install-extension-dependencies.py
