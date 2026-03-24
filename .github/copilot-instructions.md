# GitHub Copilot Instructions for MorphoCloudWorkflow

## Vendorizing to a target repository

**ALWAYS use the nox vendorize session. NEVER use rsync, `cp`, or any other
manual file copying.**

```bash
cd ~/Desktop/Projects/MorphoCloudWorkflow
# Ensure the correct branch is checked out first (e.g. intake-redesign-individual)
pipx run nox -s vendorize -- ~/Desktop/Projects/<TargetRepo>/ --commit
cd ~/Desktop/Projects/<TargetRepo>
git push origin main
```

The nox session handles correct file selection, exclusions (e.g.
`dependabot.yml`), and generates the proper commit message with a full
`git shortlog`. See [MAINTENANCE.md](../MAINTENANCE.md) for the full protocol
reference.

### Target repositories

| Repo                       | Purpose          | Policy                                             |
| -------------------------- | ---------------- | -------------------------------------------------- |
| `MorphoCloudInstancesTest` | Test environment | OK to vendorize freely                             |
| `MorphoCloudInstances`     | **Production**   | **DO NOT touch without explicit user instruction** |

## Branch policy

| Branch                       | Purpose                                                      |
| ---------------------------- | ------------------------------------------------------------ |
| `main`                       | Stable production-ready code — dep bumps and hotfixes only   |
| `intake-redesign-individual` | Active feature development — vendorize from here for testing |

Never merge `intake-redesign-individual` into `main` without explicit user
instruction.

## git commit hygiene

- Never use `git add -A` or `git add .` — always stage files explicitly by name.
