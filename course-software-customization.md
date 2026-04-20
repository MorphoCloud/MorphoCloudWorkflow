# Course Software Customization

This document describes how to provision a course with custom software (e.g.,
additional packages, Ansible roles, or a different Slicer configuration) that
differs from the standard MorphoCloud instance.

## Overview

MorphoCloud instances boot using a `cloud-config` that clones a specific commit
of [MorphoCloud/exosphere](https://github.com/MorphoCloud/exosphere) and runs
its Ansible playbook. By pointing a course repo's `cloud-config` at a
course-specific exosphere branch, you can customize the software stack without
affecting other courses or the standard instance.

**Key principle:** Each course with custom software gets a dedicated branch in
both `exosphere` and `MorphoCloudWorkflow`. The standard `main` branch remains
untouched.

### How the pieces fit together

Three repositories are involved:

1. **`exosphere`** — Contains the Ansible playbook and roles that install
   software on instances. A course branch here (e.g., `course/ut-geo101`) holds
   the custom software configuration (different Slicer version, extra
   extensions, additional packages, etc.).

2. **`MorphoCloudWorkflow`** — Contains the GitHub Actions workflows and
   `cloud-config` template. The `cloud-config` has a hardcoded SHA that tells
   the instance which exosphere commit to clone at boot. A course branch here
   has only one difference from the parent branch: the `cloud-config` points to
   the course-specific exosphere commit instead of the standard one. Currently,
   the active development branch is `implement-workflow-split` (`main` is stale
   and maintained only for the current production environment).

3. **`MC-<COURSE>` (e.g., `MC-UT-GEO101`)** — The course's instance repo. It
   receives vendorized copies of the workflows and `cloud-config` from
   MorphoCloudWorkflow. This repo always uses its `main` branch — the course
   branching happens upstream in the other two repos.

## Prerequisites

- The course has already been created through the normal intake process (MC-\*
  repo exists, standard vendorize done)
- `pipx` is installed (see [MAINTENANCE.md](MAINTENANCE.md))

## Workflow

### Step 1: Identify the current exosphere branch

All MorphoCloud instances use a tagged exosphere branch (e.g.,
`morpho-cloud-portal-2025.09.17-96cec41fb`). The course branch will be created
from this same starting point so it inherits the current standard configuration.

```bash
PROJECTS_DIR=~/Desktop/Projects
cd $PROJECTS_DIR/MorphoCloudWorkflow

pipx run nox -s display-exosphere-version
exosphere_branch=$(pipx run nox -s display-exosphere-version 2>&1 | grep -o 'morpho-cloud[^]]*')
echo $exosphere_branch
```

### Step 2: Create a course branch in exosphere

Clone exosphere and create a branch where you'll make the software changes. The
branch name should follow the `course/<short-name>` convention.

```bash
cd $PROJECTS_DIR

# Clone exosphere on the current branch
git clone https://github.com/MorphoCloud/exosphere -b $exosphere_branch

cd exosphere

# Create a course-specific branch
git switch -c course/ut-geo101
```

Add custom software. For example, to add a new Ansible role:

```bash
# Edit ansible/roles as needed, e.g.:
# - Add a new role in ansible/roles/custom-package/
# - Add the role to the playbook
# - Modify existing role variables

git add {new files}
git commit -m "feat: Add custom packages for UT GEO101"
git push origin course/ut-geo101
```

### Step 3: Create a matching branch in MorphoCloudWorkflow

This branch will be identical to the current development branch except for the
exosphere SHA in `cloud-config`. Using the same `course/*` branch name makes it
easy to see which courses have custom software.

```bash
cd $PROJECTS_DIR/MorphoCloudWorkflow
git switch -c course/ut-geo101
```

### Step 4: Bump exosphere to the course branch

```bash
pipx run nox -s bump-exosphere -- $PROJECTS_DIR/exosphere --commit
```

This updates `cloud-config`'s `exosphere_sha=` line to point to the course
branch's HEAD commit. The change is committed on the `course/ut-geo101` branch —
`main` is untouched.

### Step 5: Vendorize to the course repo

Copy the workflows and the course-specific `cloud-config` into the MC-\* repo.
From this point on, any instance created from MC-UT-GEO101 will boot with the
custom software.

```bash
pipx run nox -s vendorize -- $PROJECTS_DIR/MC-UT-GEO101/ --commit

cd $PROJECTS_DIR/MC-UT-GEO101
git push origin main
```

### Step 6: Switch back to the parent branch

Return to the parent branch so that future work (vendorizing other courses,
making workflow improvements) starts from the standard branch.

```bash
cd $PROJECTS_DIR/MorphoCloudWorkflow
git switch implement-workflow-split
```

## Re-vendorizing After Workflow Updates

When `MorphoCloudWorkflow` receives fixes or improvements (bug fixes, new
workflow features, etc.) that should be propagated to a course with custom
software, merge the parent branch into the course branch. The parent branch is
whichever branch the course branch was created from (e.g.,
`implement-workflow-split`).

```bash
cd $PROJECTS_DIR/MorphoCloudWorkflow

# Switch to the course branch and merge the parent branch
git switch course/ut-geo101
git merge implement-workflow-split

# Vendorize the merged result to the course repo
pipx run nox -s vendorize -- $PROJECTS_DIR/MC-UT-GEO101/ --commit

cd $PROJECTS_DIR/MC-UT-GEO101
git push origin main

# Switch back
cd $PROJECTS_DIR/MorphoCloudWorkflow
git switch implement-workflow-split
```

**Why merge the parent branch into the course branch?** The course branch's only
difference from its parent is the exosphere SHA in `cloud-config`. Merging the
parent branch brings in all the workflow fixes and improvements while preserving
that custom SHA. The result is a branch that has both the latest workflows _and_
the course-specific software configuration. This merged branch is what gets
vendorized into the MC-\* repo.

If there are merge conflicts (unlikely — they would only occur if the parent
branch also changed the exosphere SHA line), resolve them by keeping the course
branch's SHA.

## Re-vendorizing After Exosphere Updates

When the course-specific exosphere branch receives new commits (e.g., the
instructor requests an additional Slicer extension or a package version bump):

```bash
cd $PROJECTS_DIR/MorphoCloudWorkflow
git switch course/ut-geo101

# Bump to the latest exosphere commit on the course branch
pipx run nox -s bump-exosphere -- $PROJECTS_DIR/exosphere --commit

# Vendorize
pipx run nox -s vendorize -- $PROJECTS_DIR/MC-UT-GEO101/ --commit

cd $PROJECTS_DIR/MC-UT-GEO101
git push origin main

cd $PROJECTS_DIR/MorphoCloudWorkflow
git switch implement-workflow-split
```

## Notes

- The `course/*` branch naming convention in both repos makes it easy to
  identify which branches exist for custom courses
- Standard courses (no custom software) continue to be vendorized from `main` as
  usual — this workflow does not affect them
- When a course ends and is archived, the `course/*` branches in both
  `exosphere` and `MorphoCloudWorkflow` can be deleted
