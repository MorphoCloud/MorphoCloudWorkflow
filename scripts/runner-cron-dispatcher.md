# Runner-VM cron dispatcher — the clock

GitHub's `on: schedule` is best-effort. Measured 2026-10-27 → 11-03 on both
`Instances` and `Test-Instances`: a `7,37 * * * *` workflow fired 3–5 times a
day, a `*/5` one about the same. Whatever the cron line says, a scheduled
workflow runs a few times a day. So the time-sensitive workflows are fired by a
real **`crontab` on the acquire runner VM**, which calls `gh workflow run` — a
`workflow_dispatch`, which GitHub honors immediately. The workflows keep their
`on: schedule` only as a backstop.

**The crontab is rendered, not hand-edited.** The first dispatcher was a list
kept by hand on the VM; every scheduled workflow written after it
(`reconcile-instances` among them) silently ran a few times a day while its cron
line promised every half hour. Now:

- A time-driven workflow carries a marker comment under `on:`:
  `# dispatch: <cron>`, equal to its `schedule` cron. A workflow that only a
  person or another workflow dispatches says `# dispatch: manual`.
- `scripts/check-workflow-clock.py render` writes the crontabs from the markers:
  `scripts/instances-runner.crontab` (production, plus the `.extra` lines for
  MorphoCloudAnalytics and the token check) and
  `scripts/test-instances-runner.crontab`.
- The pre-commit hook `check-workflow-clock` fails when a schedule has no
  marker, a marker and schedule differ, a `workflow_dispatch`-only workflow has
  no marker, or a committed crontab is stale.

**Scope: `Instances` and `Test-Instances`.** `MC-*` course repos stay on
GitHub's schedule by the maintainer's decision (late shelving there burns the
instructor's per-course allocation, not the main one); the `on: schedule`
backstop is their only clock, which is why it stays in the workflows.

## Files

- [`morphocloud-dispatch.sh`](morphocloud-dispatch.sh) — reads a token and runs
  `gh workflow run <wf> --ref main` for each workflow passed. The `--ref main`
  avoids a default-branch lookup, so the token needs no `Contents` permission.
  The repo comes from `MORPHOCLOUD_DISPATCH_REPO`, set on every rendered line.
- [`check-workflow-clock.py`](check-workflow-clock.py) — the marker check and
  the crontab renderer (docstring has the rules).
- [`instances-runner.crontab`](instances-runner.crontab),
  [`test-instances-runner.crontab`](test-instances-runner.crontab) — rendered;
  edit the markers, not these.
- [`restore-instances-dispatcher.sh`](restore-instances-dispatcher.sh) —
  installs scripts, token(s) and crontab on a runner; refuses a stale crontab.

## Install on a runner (ACQUIRE runner only — never the control runner)

Production (`exouser@149.165.152.104`, defaults):

```bash
scripts/restore-instances-dispatcher.sh
```

Test-Instances (`exouser@149.165.154.162`, no analytics lines):

```bash
MWF_CRONTAB=scripts/test-instances-runner.crontab \
MWF_WF_TOKEN=~/.ssh/GH_MorphoCloud-test-workflow-dispatcher \
MWF_ANALYTICS_TOKEN= \
scripts/restore-instances-dispatcher.sh exouser@149.165.154.162
```

The token is a dedicated fine-grained PAT with **`Actions: Read and write`** on
that one repository (Metadata:read is automatic), saved to
`~/.config/morphocloud/dispatch.pat` on the runner (chmod 600) and backed up in
`~/.ssh/` on the maintainer's machine. It is **not** stored in git. Expiries and
names are logged in `MorphoCloudAppsScripts/SYSTEM-OVERVIEW.md` (Credential
Inventory); `check-dispatch-tokens.sh` (production only) emails the admins four
weeks before one expires. A missing or expired token means dispatch silently
stops and the workflows fall back to GitHub's few-times-a-day schedule.

After changing a marker or adding a time-driven workflow: `render`, commit the
crontab, merge, then re-run the install for each runner.

## Verify / monitor

- Test one dispatch:
  `~/morphocloud-dispatch.sh automatic-instance-shelving.yml`, then check
  `~/morphocloud-dispatch.log` for `dispatched` (not `FAILED`), and confirm a
  `workflow_dispatch` run appears in the repo's Actions tab.
- `crontab -l | grep -c morphocloud-dispatch` equals the number of dispatch
  lines in the committed crontab.
- Log: `~/morphocloud-dispatch.log` on the runner VM.
