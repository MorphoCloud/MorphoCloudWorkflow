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

## Vendoring `MorphoCloudWorkflow`

Once an ACCESS allocation and associated target GitHub (runner and repository)
are set up according to
[these instructions](https://github.com/MorphoCloud/MorphoCloudWorkflow/blob/main/README.md),
follow these steps to vendor or re-vendor the GitHub Workflow scripts.

### Example: Vendoring to `MorphoCloudInstances`

```bash
PROJECTS_DIR=/home/jcfr/Projects
cd $PROJECTS_DIR

# Clone the target GitHub repository
git clone https://github.com/MorphoCloud/MorphoCloudInstances

# Clone MorphoCloudWorkflow
git clone https://github.com/MorphoCloud/MorphoCloudWorkflow

cd MorphoCloudWorkflow

# Vendor scripts
pipx run nox -s vendorize -- $PROJECTS_DIR/MorphoCloudInstances/ --commit

# Publish updated target repository
cd $PROJECTS_DIR/MorphoCloudInstances
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
   `MorphoCloudInstances` or `MorphoCloudInstancesTest`)

### Example: Vendoring to `MorphoCloudInstances`

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

```bash
cd $PROJECTS_DIR

# Clone exosphere and check out the desired branch
git clone https://github.com/MorphoCloud/exosphere -b $exosphere_branch

cd MorphoCloudWorkflow

# Update exosphere version in cloud-config and commit
pipx run nox -s bump-exosphere -- $PROJECTS_DIR/exosphere --commit

# Push updates to the MorphoCloudWorkflow repository
git push origin main
```

**Step 3:** Vendor changes into the `MorphoCloudInstances` target repository

```
cd $PROJECTS_DIR

# Clone the target GitHub repository
git clone https://github.com/MorphoCloud/MorphoCloudInstances.git

cd MorphoCloudWorkflow

# Vendor scripts
pipx run nox -s vendorize -- $PROJECTS_DIR/MorphoCloudInstances/ --commit

# Push updates to target repository
cd $PROJECTS_DIR/MorphoCloudInstances
git push origin main
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
pipx run nox -s bump-exosphere -- $PROJECTS_DIR/exosphere --commit

# Push updates to the MorphoCloudWorkflow repository
git push origin main
```
pipx run nox -s vendorize -- $PROJECTS_DIR/MorphoCloudInstances/ --commit
cd $PROJECTS_DIR/MorphoCloudInstances
git push origin main


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
  https://github.com/MorphoCloud/exosphere/blob/morpho-cloud-portal-2024.07.17-78a7e2d93/ansible/roles/slicer/vars/main.yml
[slicer-install-extension-dependencies-py]:
  https://github.com/MorphoCloud/exosphere/blob/morpho-cloud-portal-2024.07.17-78a7e2d93/ansible/roles/slicer/files/slicer-install-extension-dependencies.py
