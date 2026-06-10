# Course Software Customization

How to provision a course with a software suite different from the standard
SlicerMorph image (extra packages, different Slicer configuration, additional
Ansible roles).

## How it works

Instances install software at first boot: `cloud-config` clones
[MorphoCloud/exosphere](https://github.com/MorphoCloud/exosphere) (a plain, full
clone) and `git reset --hard`s to a pinned commit, then runs the Ansible
playbook. For a custom course, the customization lives in a `course/<slug>`
branch of `exosphere`; the course repo's boot path picks it up through a single
repo **variable** — the committed `cloud-config` stays generic.

**`MORPHOCLOUD_EXOSPHERE_REF`** (per `MC-*` repo, absent by default):

- When unset, `/create` uses the standard vendorized pin — default course,
  nothing to do.
- When set, it must be the **full 40-character commit SHA** on
  `MorphoCloud/exosphere` — normally the HEAD of the course's `course/<slug>`
  branch. `create-course-instance.yml` validates it (format + the commit exists)
  **before any OpenStack call**, rewrites the `exosphere_sha` line in the
  workspace copy of `cloud-config`, and records the applied ref in a comment on
  the instance issue.

A SHA rather than a branch name keeps the course reproducible: students
provisioned weeks apart boot the same image. A mid-course software update is a
deliberate act — push to the branch, set the variable to the new SHA; only
instances created afterward are affected.

Because the variable survives `vendorize-course`, re-vendorizing a course repo
no longer risks silently resetting its image pin.

## Setting up a custom course

1. **Branch exosphere** from the current production tag and add the
   customization:

   ```bash
   cd ~/Desktop/Projects/MorphoCloudWorkflow
   pipx run nox -s display-exosphere-version   # the morpho-cloud-portal-… tag

   cd ~/Desktop/Projects/exosphere
   git switch -c course/<slug> <that-tag>
   # edit ansible/roles + playbook as requested
   git commit && git push origin course/<slug>
   ```

2. **Point the course repo at it:**

   ```bash
   gh variable set MORPHOCLOUD_EXOSPHERE_REF \
     --repo MorphoCloud/MC-<COURSE> \
     --body "$(git -C ~/Desktop/Projects/exosphere rev-parse course/<slug>)"
   ```

3. Done — the next `/create` in that repo boots the custom image. No vendorize,
   no MorphoCloudWorkflow branch, no `cloud-config` edit.

**Timing rule:** confirm the suite is custom-and-ready (or confirmed default)
**before** the instructor pushes `students.txt`. Don't enroll students into an
unconfirmed image.

## Validating a new custom image

- Set the variable to the branch SHA, `/create` on a test issue, and verify the
  requested software is present in-guest (the issue gets a "Custom course image"
  comment recording the exact commit).
- A garbage value fails loudly before any resource is created.
- Unset the variable (`gh variable delete MORPHOCLOUD_EXOSPHERE_REF --repo …`)
  to return the course to the standard image.

## Registry — which courses are customized?

The variable doubles as the registry:

```bash
for r in $(gh repo list MorphoCloud --json name --jq '.[].name | select(startswith("MC-"))'); do
  echo -n "$r: "
  gh variable get MORPHOCLOUD_EXOSPHERE_REF --repo "MorphoCloud/$r" 2>/dev/null || echo "(standard)"
done
```

## Branch lifetime

- **While the course is live, the `course/<slug>` branch must stay pushed.** A
  commit reachable only from a deleted branch is eventually garbage-collected on
  GitHub, and the boot clone would no longer contain it.
- When the course ends, delete the variable and the branch.
- A course branch is a point-in-time fork: it does **not** receive later
  production fixes (OS/security/Slicer). If a branch is reused across terms,
  rebase it onto the current production tag between terms, re-validate, and set
  the variable to the new HEAD.
