# Runner-VM cron dispatcher

GitHub's `on: schedule` triggers are best-effort and heavily throttled — a `*/5`
workflow here was observed firing only every ~4–5 hours, so idle instances
shelved hours late (wasting credits). For reliable, predictable timing, the
time-sensitive scheduled workflows are triggered by a real **`crontab` on the
`MorphoCloud/Instances` self-hosted runner VM** (BIO180006, `149.165.152.104`),
which calls `gh workflow run` — a `workflow_dispatch`, which GitHub honors
immediately. The workflows keep their `on: schedule` only as a loose backstop.

**Scope: `MorphoCloud/Instances` only.** Test-Instances and `MC-*` course repos
are intentionally **not** covered (they stay on GitHub's schedule — late
shelving there burns the instructor's per-course allocation, not the main one).

## Files

- [`morphocloud-dispatch.sh`](morphocloud-dispatch.sh) — reads a token and runs
  `gh workflow run <wf> --ref main` for each workflow passed. The `--ref main`
  avoids a default-branch lookup, so the token needs no `Contents` permission.

## One-time setup on the runner VM

1. **Deploy the script:**

   ```bash
   scp scripts/morphocloud-dispatch.sh exouser@149.165.152.104:~/morphocloud-dispatch.sh
   ssh exouser@149.165.152.104 'chmod +x ~/morphocloud-dispatch.sh && mkdir -p ~/.config/morphocloud && chmod 700 ~/.config/morphocloud'
   ```

2. **Place the token** — a dedicated fine-grained PAT with
   **`Actions: Read and write` on `Instances` only** (Metadata:read is
   automatic). Save it to `~/.config/morphocloud/dispatch.pat` (chmod 600). It
   is **not** stored in git. The current token expires **2027-06-03** — rotate
   before then or dispatch silently stops. See the
   `GH_MorphoCloud-workflow-dispatcher` row in
   `MorphoCloudAppsScripts/SYSTEM-OVERVIEW.md`.

   ```bash
   scp ~/.ssh/GH_MorphoCloud-workflow-dispatcher exouser@149.165.152.104:/home/exouser/.config/morphocloud/dispatch.pat
   ssh exouser@149.165.152.104 'chmod 600 ~/.config/morphocloud/dispatch.pat'
   ```

3. **Install the crontab** (`crontab -e` on the VM):

   ```cron
   # MorphoCloud workflow dispatcher — GitHub on:schedule is throttled; trigger reliably here.
   */5 * * * * /home/exouser/morphocloud-dispatch.sh automatic-instance-shelving.yml update-request-status-label.yml
   */10 * * * * /home/exouser/morphocloud-dispatch.sh workshop-backfill.yml
   */15 * * * * /home/exouser/morphocloud-dispatch.sh collect-instance-uptime.yml
   0 * * * * /home/exouser/morphocloud-dispatch.sh update-workshop.yml
   0 0 * * * /home/exouser/morphocloud-dispatch.sh automatic-instance-deleting.yml automatic-volume-deleting.yml
   # Monthly usage report -> MorphoCloudAnalytics (separate repo + dedicated token)
   0 0 1 * * MORPHOCLOUD_DISPATCH_REPO=MorphoCloud/MorphoCloudAnalytics MORPHOCLOUD_DISPATCH_TOKEN_FILE=/home/exouser/.config/morphocloud/analytics-dispatch.pat /home/exouser/morphocloud-dispatch.sh monthly-usage.yml
   ```

   The monthly-usage line targets **MorphoCloudAnalytics** (not Instances) via
   the `MORPHOCLOUD_DISPATCH_REPO` + `MORPHOCLOUD_DISPATCH_TOKEN_FILE`
   overrides, using the dedicated `GH_MorphoCloud-analytics-dispatcher` token at
   `~/.config/morphocloud/analytics-dispatch.pat` (Actions:rw on
   MorphoCloudAnalytics only).

## Verify / monitor

- Test one dispatch:
  `~/morphocloud-dispatch.sh automatic-instance-shelving.yml`, then check
  `~/morphocloud-dispatch.log` for `dispatched` (not `FAILED`), and confirm a
  `workflow_dispatch` run appears in the repo's Actions tab.
- Log: `~/morphocloud-dispatch.log` on the runner VM.
