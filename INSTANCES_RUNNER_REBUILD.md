# Instances Runner — Rebuild Runbook

Authoritative procedure for rebuilding a **`MorphoCloud/Instances`** self-hosted
runner on JetStream2. **Scope: the Instances runners only** (the `acquire` +
`control` lanes). This does **not** apply to `MC-*` course runners (those have
their own per-allocation setup — see `runner-setup-helper.yml`).

Use this whenever a runner VM is recreated/rebuilt. Registering the runner is
the easy part; the danger is the **off-runner host state** (crontab, dispatcher
scripts, PAT token files, clouds.yaml, SSH key) being silently missed, so
automation comes back half-working.

## Architecture (what each runner does)

Two runners back `Instances` (see `SYSTEM-OVERVIEW.md` → Runner Architecture):

- **`acquire` (runner-1, `149.165.152.104`)** — the floating-IP-safe acquire
  lane **and the "clock"**: its crontab fires all the time-sensitive scheduled
  workflows via `morphocloud-dispatch.sh` (GitHub's `on: schedule` is
  throttled).
- **`control` (runner-2)** — runs the dispatched workflows (cloud-init setup,
  lifecycle crons, commands).

> ⚠️ **The crontab/dispatcher lives on the `acquire` runner ONLY.** A copy on
> the `control` runner double-fires every cron. (A stray clone copy was removed
> 2026-06-03.)

## What's lost on a rebuild (restore checklist)

| State                                                                                | On which runner  | Restore from                                                    |
| ------------------------------------------------------------------------------------ | ---------------- | --------------------------------------------------------------- |
| Actions runner registration + label                                                  | both             | re-register (fresh token)                                       |
| `~/venv` + OpenStack client, `jq`, `gh`, docker group                                | both             | `provision-instances-runner-host.sh`                            |
| `~/.config/openstack/clouds.yaml`                                                    | both             | Exosphere → allocation → Credentials → clouds.yaml              |
| `~/.ssh/id_ed25519` (murat-key, instance SSH)                                        | both             | your key backup                                                 |
| `morphocloud-dispatch.sh`, `check-dispatch-tokens.sh`                                | **acquire only** | this repo `scripts/`                                            |
| `dispatch.pat`, `analytics-dispatch.pat` (PAT **values**, unrecoverable from GitHub) | **acquire only** | `~/.ssh/GH_MorphoCloud-{workflow,analytics}-dispatcher` backups |
| crontab (the clock)                                                                  | **acquire only** | `scripts/instances-runner.crontab`                              |

## Rebuild steps

### 1. Build the VM

Exosphere → correct allocation → `m3.tiny`, custom root disk **30 GB**, upload
your SSH key, **disable web desktop + Guacamole**. (Same as MWF README →
"Setting Up a MorphoCloud GitHub Runner".) SSH in as `exouser`.

### 2. Base host setup (both runners)

Copy and run the base script on the runner:

```bash
scp scripts/provision-instances-runner-host.sh exouser@<RUNNER>:~/
ssh exouser@<RUNNER> 'bash ~/provision-instances-runner-host.sh'
```

Then place `~/.config/openstack/clouds.yaml` (Exosphere → Credentials →
clouds.yaml). The allocation name (e.g. `BIO180006_IU`) is the
`MORPHOCLOUD_OS_CLOUD` repo variable.

### 3. SSH key for instances (both runners)

Copy the murat-key so the runner can reach instances:

```bash
scp ~/.ssh/id_ed25519 exouser@<RUNNER>:~/.ssh/id_ed25519
ssh exouser@<RUNNER> 'chmod 600 ~/.ssh/id_ed25519'
```

### 4. Register the runner (both runners)

GitHub → `MorphoCloud/Instances` → Settings → Actions → Runners → New
self-hosted runner (Linux x64). Download/extract, then configure with the
**label for this runner's lane**:

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner   # one dir per runner
./config.sh --url https://github.com/MorphoCloud/Instances --token <REG_TOKEN> --labels acquire   # or: control
sudo ./svc.sh install && sudo ./svc.sh start && sudo ./svc.sh status
```

Repo variables: `MORPHOCLOUD_ACQUIRE_RUNNER=acquire`,
`MORPHOCLOUD_CONTROL_RUNNER=control`.

### 5. Restore the clock — **ACQUIRE runner ONLY**

From your machine (has this checkout + the PAT backups in `~/.ssh`):

```bash
scripts/restore-instances-dispatcher.sh exouser@<ACQUIRE_RUNNER>
```

It copies the two dispatcher scripts + the two token files (from your `~/.ssh`
backups), installs `instances-runner.crontab`, and verifies each token
authenticates. **Do not run this against the control runner.**

If the PAT backups are missing, regenerate the fine-grained PATs (Actions:
read/write — workflow-dispatcher on `Instances`, analytics-dispatcher on
`MorphoCloudAnalytics`), save them to `~/.ssh/`, log the new expiries in
`SYSTEM-OVERVIEW.md` Credential Inventory, then re-run.

## Verify

- `ssh exouser@<RUNNER> 'systemctl --type=service | grep actions.runner'` →
  service active; runner shows **online** in the GitHub Runners list.
- **Acquire only:**
  `ssh exouser@<ACQUIRE> 'crontab -l | grep -c morphocloud-dispatch'` → 7 lines;
  after the next `*/5`, `~/morphocloud-dispatch.log` shows `dispatched` (not
  `FAILED`), and `workflow_dispatch` runs appear in the Actions tab.
- A test instance `/create` succeeds (acquire lane + OpenStack creds working).

## Recovery time & safety net

With the PAT backups + clouds.yaml in hand, a full rebuild is **~15–20 min**
(mostly VM build + runner registration). The token-expiry alerts and (if added)
a dispatcher dead-man's-switch tell you if the clock stops; otherwise a missed
crontab shows up only as instances not auto-shelving.

## Related

- `scripts/runner-cron-dispatcher.md` — dispatcher rationale + the crontab.
- MWF `README.md` → "Setting Up a MorphoCloud GitHub Runner" — base runner
  setup.
- `MorphoCloudAppsScripts/SYSTEM-OVERVIEW.md` → Runner Architecture + Credential
  Inventory.
