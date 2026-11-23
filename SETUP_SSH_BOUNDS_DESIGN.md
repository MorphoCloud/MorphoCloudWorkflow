# Design note: time limits on runner-to-instance SSH

Status: approved and built, 2026-11-19 (section 8). Tests 2–6 run on
Test-Instances 2026-11-20 (section 9); the gap test 4 found is handled in a
separate note (section 10).

Scope: item 1 of the three found on Test-Instances #575. Items 2 and 3 have
their own notes later:

- Item 2: creates that stop at `setup status [starting]` because the boot-time
  apt update reads `file:/software`, which is on CephFS.
- Item 3: the progress table shows Setup ✅ before the share mount and the
  reboot have finished.

## 1. What happened

On Test-Instances #575 (m3.xl, 2026-11-19), the step "Mount the owner's data
share" sends a script to the instance with `ssh … 'sudo bash -s'`. The script
ran correctly: MyDrive was mounted and the folders were linked. The `ssh`
command still did not return.

The cause is the launcher-trust loop at the end of that script (added in commit
54b6238, 2026-09-27):

```bash
runuser -u exouser -- env HOME="$H" dbus-run-session -- sh -c \
  'gio set "$1" metadata::trusted true && gio set "$2" metadata::trusted true' …
```

1. `dbus-run-session` starts a private session bus.
2. `gio` starts `gvfsd`, which starts
   `/usr/libexec/gvfsd-fuse /home/exouser/.gvfs -f`.
3. `gvfsd-fuse` inherits the script's stdout and stderr, which are the SSH
   session's pipes.
4. When `dbus-run-session` exited, `gvfsd-fuse` did not. It was reparented to
   pid 1 and stuck in `futex_wait`, which is a lock inside its own program, not
   disk or network I/O.
5. It still held sshd's output pipes, so sshd never closed the session and the
   runner's `ssh` waited.

I confirmed this on the live instance: only sshd (pid 12127) and gvfsd-fuse
(pid 12561) held those pipes. After `kill 12561`, the step continued, the
instance rebooted and the create succeeded.

It is intermittent. All 37 earlier successful share creates on Test ran the same
code and finished this step in 9–18 s.

Neither MorphoSource nor CephFS caused it:

- Setup never reads the MorphoSource folder.
- Both CephFS sessions came up normally.
- There were no Ceph errors and no blocked-task warnings in the kernel log.

## 2. The class of problem

**No runner-to-instance SSH call has an overall time limit.** The existing
options cover connecting, not finishing:

- `ConnectTimeout` limits how long the connection takes to open. Once it is
  open, nothing limits how long the call runs.
- No call sets `ServerAliveInterval`. If an instance stops answering in the
  middle of a call (network drop, JS2 pause, kernel hang), the call waits
  indefinitely.
- No workflow except `migrate-volume.yml` sets `timeout-minutes`. GitHub's
  default is 6 hours.

A call can hang in two ways:

1. A process started on the instance keeps the session's output open, as on
   #575.
2. The instance stops answering in the middle of the call.

**Why this matters for production.** A self-hosted runner runs one job at a
time. In production the create job runs on the acquire runner, and that runner
also serializes every other member's `/create` and `/unshelve`. One hung setup
would:

- stop every other member's create and unshelve for up to 6 hours;
- give the member who started it no message, while the portal shows Setup ✅;
- send no alert to admins.

The same applies to the sweeps, which run on the control runner. One hung SSH
call in "Automatic Instance Shelving" stops the whole sweep. No later instance
is checked and nothing is shelved until GitHub's 6-hour limit.

### Every call in scope

| Where                                                                                       | Remote command                                  | Today's limit                             |
| ------------------------------------------------------------------------------------------- | ----------------------------------------------- | ----------------------------------------- |
| setup-instance: wait for sshd (share mount, after reboot)                                   | `true`                                          | ConnectTimeout 10 only                    |
| setup-instance: write share key                                                             | `sudo install … cat > /etc/ceph/mc-user.secret` | ConnectTimeout 10 only                    |
| setup-instance: mount script (**the #575 hang**)                                            | `sudo bash -s`                                  | ConnectTimeout 10 only                    |
| setup-instance: share and desktop check after reboot, status dump                           | `$check`, `systemctl status`                    | ConnectTimeout 10 only                    |
| setup-instance: reboot, wait-after-reboot                                                   | `sudo shutdown -r now`, `true`                  | none                                      |
| setup-instance: volume path (rename, attach check, copy, home relocate, Uploads, .Renviron) | various                                         | none or ConnectTimeout 10                 |
| retrieve-metadata: passphrase                                                               | `curl … /openstack/latest/password`             | ConnectTimeout 10 only                    |
| fip.py: identity check                                                                      | `curl --max-time 10 meta_data.json`             | `subprocess` timeout 60 (already bounded) |
| automatic-instance-shelving: session-age read                                               | per instance, 3 tries                           | ConnectTimeout 15 only                    |
| extend-session: reset timer                                                                 | `check-instance-shelve.sh`                      | ConnectTimeout 15 only                    |
| collect-instance-uptime: uptime, startup time                                               | `cat /proc/uptime`, …                           | none (no ConnectTimeout either)           |
| send-email, control-instance, automatic-instance-shelving: runner to data portal            | `connection`, `connection-clear`, `session`     | ConnectTimeout 10–15 only                 |

The runner-to-portal calls are in scope too. A portal that stops answering hangs
them in the same way.

Not in scope: scripts an admin runs by hand from the laptop (`bake-image.sh`,
`install-*.sh`, `restore-instances-dispatcher.sh`). An admin is watching those
runs.

## 3. Proposed change

### A. Processes started by a setup command end when that command ends

Run the launcher-trust command in a transient systemd unit instead of directly
under the SSH session:

```bash
systemd-run --quiet --wait --pipe --collect \
  --uid=exouser --setenv=HOME="$H" \
  -p RuntimeMaxSec=20 -p TimeoutStopSec=5 \
  dbus-run-session -- sh -c 'gio set … && gio set …' sh "$f" "$H/Desktop/…" \
  < /dev/null > /dev/null \
  || echo "Could not mark $(basename "$f") as trusted" >&2
```

When the main process exits, the unit stops. systemd then kills every process
left in the unit's own cgroup, which includes any gvfsd or gvfsd-fuse that gio
started. `--wait` returns only after that. `RuntimeMaxSec=20` limits the unit's
total run time (a normal run takes under 1 s; it was 60 in the first draft,
lowered in the build so that two launchers plus the mount still fit the mount
script's limit). `TimeoutStopSec=5` makes systemd use SIGKILL after 5 s on a
leftover that ignores SIGTERM. Processes outside the unit are not touched,
including the member's real desktop session and its own gvfsd at
`/run/user/1001/gvfs`.

I am not proposing `pkill gvfsd-fuse`. On #575 the real desktop session was
already running with its own gvfsd-fuse, and a name-based kill would hit that
one too.

Failing to mark a launcher as trusted stays non-fatal, as it is today. The
login-time autostart marks the launchers again.

### B. Every SSH call in the table has a total time limit

1. Add one shared helper, `mc_ssh <limit-seconds> <ssh args…>`, in a sourced
   file next to the actions. It runs:

   ```bash
   timeout -k 10 "$limit" ssh <existing options> \
     -o ServerAliveInterval=15 -o ServerAliveCountMax=4 …
   ```

   `ServerAliveInterval=15` with `ServerAliveCountMax=4` detects an instance
   that stopped answering within about 60 s. `timeout` covers the #575 case,
   where the instance answers but the session never closes.

2. When `timeout` fires (exit code 124 or 137), the helper prints
   `::error ::<step>: SSH to <ip> did not finish within <N> s`. The error then
   names the time limit and not a vague failure.
3. Each call site passes its own limit, chosen from the measured durations below
   at roughly 10 times p90, with a 30 s minimum. These numbers are from the 38
   successful share creates on Test (2026-11-02 to 2026-11-19), using the
   sub-step durations GitHub records:

   GitHub records each step's duration, not each SSH call's, so a row covers
   every call in that step. For the single-call steps (identity check, check
   after reboot, portal send), the step time is the call time.

   | Step                                                                       | n   | p50  | p90  | p95  | max (excluding #575) | Proposed per-call limit                                  |
   | -------------------------------------------------------------------------- | --- | ---- | ---- | ---- | -------------------- | -------------------------------------------------------- |
   | Mount the owner's data share (whole step: sshd probe + key write + script) | 37  | 11 s | 13 s | 13 s | 18 s                 | mount script 180 s; key write 30 s; each sshd probe 30 s |
   | Verify the instance is this server                                         | 19  | 3 s  | 4 s  | 5 s  | 5 s                  | already 60 s in fip.py                                   |
   | Reboot instance (whole step: `shutdown` + waits)                           | 38  | 33 s | 79 s | 80 s | 80 s                 | `shutdown` call 30 s; each probe 30 s                    |
   | Verify the data share after reboot                                         | 38  | 1 s  | 1 s  | 2 s  | 6 s                  | each check 30 s                                          |
   | Retrieve metadata (whole step, includes its retry loop)                    | 76  | 8 s  | 12 s | 12 s | 13 s                 | each try 60 s (inside its existing 300 s loop)           |
   | Send access details to the portal                                          | 38  | 1 s  | 1 s  | 1 s  | 1 s                  | 30 s                                                     |

   The mount script's limit is 180 s, not the 120 s of the first draft: inside
   it, the mount unit may take its full `TimeoutSec=90` and each of the two
   launchers its 20 s, so 120 s could cut off a slow but working mount.

   The volume-path steps do not run on Test, so their durations are from the 39
   most recent successful creates on production (`Instances`, read-only):

   | Step (production)                         | n   | p50  | p90  | p95  | max  | Per-call limit                               |
   | ----------------------------------------- | --- | ---- | ---- | ---- | ---- | -------------------------------------------- |
   | Rename MyData directory to MyData-tmp     | 39  | 1 s  | 2 s  | 2 s  | 3 s  | 60 s per try (inside its 300 s loop)         |
   | Copy installed files into attached volume | 39  | 12 s | 13 s | 19 s | 40 s | mount check 30 s; copy 300 s (multi-GB copy) |
   | Relocate home directory                   | 39  | 1 s  | 1 s  | 2 s  | 2 s  | 120 s (first create copies the home)         |
   | Create Uploads folder                     | 35  | 1 s  | 1 s  | 1 s  | 1 s  | 30 s                                         |
   | Create Renviron file                      | 39  | 1 s  | 1 s  | 1 s  | 1 s  | 30 s                                         |
   | Reboot instance (whole step)              | 39  | 33 s | 80 s | 80 s | 80 s | `shutdown` call 30 s; each probe 30 s        |

   The sweep and command calls (shelving, uptime, extend-session, portal sends)
   get 30 s per call. Their whole jobs take under 1 min at p95 (section C), so
   no single call comes near 30 s.

4. The step-level loops (for example, wait up to 300 s for sshd) do not change.
   Only each single call inside them gets a limit.

### C. Job-level backstop: `timeout-minutes`

`timeout-minutes` is a safety net in case B misses a call. It is not the primary
mechanism. It goes on every job that SSHes to instances:

- **Create job: 35 min.** Measured job duration on the 37 successes other than
  #575 (#575 took 24 min with the hang): p50 13 min, p90 18 min, p95 19 min, max
  19 min. The longest legitimate run is the setup poll's 20 min budget plus the
  slowest other parts seen: acquire 4 min (max 229 s, n=62), post-setup steps
  about 2 min (mount + reboot + check + email maxima), job overhead about 1 min.
  That is about 27 min. 35 min leaves 8 min of slack. 30 min would leave 3 min,
  which a slow acquire or one slow reboot could use up, and the member would
  then get a cancellation instead of a reported failure (see below). The 20 min
  poll budget sets the floor. The poll itself has p50 618 s, p95 906 s and max
  976 s (n=38). Failing a create much sooner than 30 min would mean lowering
  that budget, and that is item 2's note.
- **Other jobs:** at least 3 times their measured p90, and never below the sum
  of the job's own built-in waits, so that a slow but working run is not cut
  off. Measured from the last 40 successful runs of each workflow (fewer where
  the workflow has run less), on production where it runs there:

  | Workflow / job                           | Where measured | n   | p95      | max      | Built-in waits add up to                    | `timeout-minutes` |
  | ---------------------------------------- | -------------- | --- | -------- | -------- | ------------------------------------------- | ----------------- |
  | Create Course Instance / create          | Test           | 9   | 11.0 min | 11.0 min | same steps as the individual create         | 35                |
  | Create Instance from Workflow / acquire  | production     | 33  | 3.3 min  | 3.5 min  | ACTIVE wait 5 min + address 2 min           | 20                |
  | Create Instance from Workflow / setup    | Test           | 10  | 11.6 min | 11.6 min | setup poll 20 min + post-setup steps        | 30                |
  | Resume Setup / resume                    | Test           | 1   | 1.6 min  | 1.6 min  | post-setup steps + email, about 25 min      | 30                |
  | Control Instance / control               | production     | 39  | 6.7 min  | 9.6 min  | an unshelve's waits, about 37 min           | 45                |
  | Control Instance from Workflow / control | production     | 40  | 3.1 min  | 4.6 min  | same steps as Control Instance              | 45                |
  | Delete Instance / delete_instance        | production     | 40  | 0.8 min  | 1.0 min  | action wait 10 min + delete wait 3 min      | 20                |
  | Send Email / send-email                  | production     | 37  | 0.6 min  | 5.7 min  | address check 5 min + passphrase read 5 min | 15                |
  | Automatic Instance Shelving              | production     | 40  | 0.8 min  | 1.1 min  | about 2.5 min per instance not answering    | 30                |
  | Collect Instance Uptime                  | production     | 40  | 0.7 min  | 2.8 min  | at most 1 min per instance                  | 20                |
  | Process /extend Command                  | Test           | 40  | 0.3 min  | 0.9 min  | address check 30 s + two 30 s calls         | 10                |

A job that reaches `timeout-minutes` is cancelled, and its `failure()` steps do
not run. The admin email and the member's retry guidance therefore come only
from B. Whether "Report Dropped Command" reports a job cancelled this way is an
open question. It was written for runs cancelled before their first step. Test 4
below checks it.

## 4. Other actors during the changed steps

| Actor                                                                | What it does at the same time                                                          | Interaction with this change                                                                                                                                |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Member's desktop session (vncserver@1, GNOME autologin)              | Already running by the mount step on #575, with its own gvfsd at `/run/user/1001/gvfs` | A kills only its own unit's cgroup, so the member's session is untouched. A name-based kill would have hit it, which is why it is not proposed.             |
| Login-time launcher-trust autostart                                  | Marks the same launchers when the member's session starts                              | Unchanged. Both runs are safe to repeat; whichever finishes last wins, with the same value.                                                                 |
| Member connecting during setup                                       | The portal shows the passphrase and links before setup finishes (#575 screenshot)      | Unchanged. The reboot in setup still interrupts them, as it does today.                                                                                     |
| Reboot step (right after the mount)                                  | Restarts the instance                                                                  | Unchanged. Its `shutdown` call can end with 255 when the connection drops; the helper passes 255 through as today and uses 124/137 only for the time limit. |
| unattended-upgrades                                                  | Turned on only at the end of setup                                                     | No overlap.                                                                                                                                                 |
| Automatic Instance Shelving, Collect Instance Uptime, Extend Session | SSH to every running instance, including one that is being set up                      | With B, an instance that stops answering costs each sweep at most its per-call limit instead of stopping it.                                                |
| Per-issue concurrency group / runner slot                            | A hung create holds the runner and the issue's group                                   | With B, held for at most the per-call limit plus the normal failure path.                                                                                   |

## 5. What the member and the admins see

| Case                                           | Today                                                                          | After                                                                                                                                                                                                  |
| ---------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| #575-style leftover process                    | Not possible in the share mount (A); if it happens elsewhere, as below         | Not possible in the share mount (A)                                                                                                                                                                    |
| A call hangs for any other reason              | Portal shows Setup ✅ and nothing else for up to 6 h; runner blocked; no alert | Within that call's limit the step fails. Existing path: "Failed to mount the data share" (or that step's message) on the issue, retry guidance, admin email "create failed", portal shows the failure. |
| Sweep meets an instance that stopped answering | Sweep stops; no later instance is checked                                      | Warning for that instance; sweep continues                                                                                                                                                             |

## 6. Tests (Test-Instances, BIO240357_IU, as amm554 through the portal)

Real resources used: one m3.xl and one g3.medium instance on Test, about 1 h
each, and no production resources.

1. **Reproduce the hang first.** On a Test instance with the desktop session
   running, run today's `gio` loop over SSH 50 times and count the hangs. Then
   run the A version 50 times. Pass: the A version has 0 hangs, and no
   gvfsd-fuse is left after each run. If today's version never hangs in 50 runs,
   I report that and we decide whether A is still worth shipping on the evidence
   from #575 alone.
2. **Forced hang.** On a Test branch, add `sleep 600 &` with inherited stdout to
   the mount script. Pass: the step fails at about 120 s with the time-limit
   message, and the member sees the failed-mount comment and retry guidance. The
   admin email arrives at maga@uw.edu.
3. **Instance stops answering mid-call.** On the instance, drop port 22 traffic
   during the mount call with `iptables`. Pass: the step fails within about 75
   s.
4. **Job backstop.** A Test-only dispatch of a job with a 2-minute
   `timeout-minutes` and a `sleep 300`. Record what "Report Dropped Command",
   the issue and the portal show. This answers the open question in C.
5. **Sweep.** Make one Test instance stop answering on port 22, then dispatch
   Automatic Instance Shelving. Pass: the sweep finishes, with a warning for
   that instance, and the other instances are processed.
6. **Full creates.** Use the portal to create an m3.xl and a g3.medium. Pass:
   both succeed, and on the first desktop login the launchers start without the
   "Allow Launching" prompt, which is the original purpose of the gio call.

## 7. Delivery

One PR to `share-prototype` with A, B and C together, the test results and the
measured sweep limits. Then one review, then vendorize to Test-Instances. No
production change until the cutover. With the cutover about 2026-11-24, the
target is a merged PR by 2026-11-22.

## 8. Approval

Approved by the user on 2026-11-19: the scope as in the table (including the
volume-path calls), and 35 min `timeout-minutes` on the create job (lowered from
the draft's 60 at the user's request).

Changes made in the build, not in the approved draft:

- Mount script limit 180 s instead of 120 s, and the gio unit's `RuntimeMaxSec`
  20 s instead of 60 s, with `TimeoutStopSec=5` (section A).
- Volume-path limits set from production measurements (section B).
- extend-session: the timer-reset call now ends with `|| elapsed=""`. Before, a
  failed call ended the step under `-e` without its "Could not reset the session
  timer" error; now the error is printed, for a timeout too.

## 9. Test results

Local (2026-11-19):

- pre-commit (actionlint, workflow schema, YAML, codespell, prettier): passed.
- `pytest scripts/`: 198 passed.

On the Test runner (`MC-Test-runner`) against Test instance #575, before any
workflow change was vendorized:

| Test                                                                      | Result                                                                                       |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Helper, normal call                                                       | exit 0 in 1 s                                                                                |
| Helper, remote `sleep 40 &` holding stdout, limit 10 s (the #575 failure) | exit 124 at 10 s, `::error` printed                                                          |
| Helper, remote `exit 3`                                                   | exit 3 passed through                                                                        |
| Helper inside `$(...)`                                                    | output captured, `::error` not captured                                                      |
| Helper under `set -euo pipefail`, plain and inside `$(...)`               | annotation printed, then the shell ends with 124                                             |
| Test 1: today's gio loop, 50 runs, desktop session running                | 0 hangs, 0 leftover gvfsd-fuse, slowest 10 s. **The #575 race did not reproduce on demand.** |
| Test 1: new gio loop (section A), 50 runs                                 | 0 hangs, 0 leftover gvfsd-fuse, 0 trust failures, slowest 1 s                                |
| Forced leftover, today's code (`setsid sleep 300 &` next to gio)          | SSH hung until the 40 s limit; the sleep was still running afterwards                        |
| Forced leftover, new code                                                 | returned in 2 s; the sleep was killed with the unit                                          |
| gio that never returns, new code                                          | unit stopped by `RuntimeMaxSec` at 21 s; "Could not mark" printed; script continued          |
| Launcher still trusted after the trials                                   | `metadata::trusted: true` on `/home/exouser/Desktop/Slicer.desktop`                          |

Because the race did not reproduce on demand, A is shipped on the evidence of
#575 and of the forced-leftover test, which shows the same failure (a process
left by the command holds the SSH session) under today's code and its absence
under A.

Tests 2–6 on Test-Instances (2026-11-20). The PR was vendorized as `51835208`.
Tests 2–4 used the TEST-ONLY branch `test-only-ssh-bounds-hooks` (218889f,
vendorized as `efec45df`). That branch leaves a `sleep 600` holding the mount
script's output on amm554's requests, and sets a 2-minute create job limit on
requests labeled `test:job-timeout`. The PR code was vendorized back as
`cfb11e1d`, identical to `51835208`. All requests were opened by amm554 in the
portal's format, and the admin typed the commands.

| Test                                           | Request    | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ---------------------------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 6, m3.xl create                                | #578       | **Passed.** Success in 9.3 min. Mount step 13.4 s, reboot and post-reboot check passed. Both launchers `metadata::trusted: true`. No private gvfsd-fuse left.                                                                                                                                                                                                                                                                                                              |
| 6, first try (before the JS2 fix)              | #576       | Not a test result. FIP 149.165.149.82 could not reach the Ceph monitors (a JS2 fault in 149.165.149.0/24, fixed by JS2 on 2026-11-20). The mount failed in 70 s with an error, the failed-mount comment and the admin email: the right behaviour.                                                                                                                                                                                                                          |
| 6, g3.medium create                            | #579, #583 | Not run: JS2 had no g3.medium host on two tries ("No instance of type g3.medium is available"). Replaced by g3.large, which uses the same vGPU image.                                                                                                                                                                                                                                                                                                                      |
| 6, g3.large create (vGPU image)                | #584       | **Passed.** Success in 6.9 min. Mount step 10.5 s, reboot and post-reboot check passed. GPU `GRID A100X-20C` present. Both launchers `metadata::trusted: true`. The one gvfsd-fuse running was started after the reboot by the desktop session, not left by setup.                                                                                                                                                                                                         |
| 5, sweep with one instance whose command hangs | #578       | **Passed.** `check-instance-shelve.sh` made to sleep 600 s on #578. Sweep: `Shelving check on 149.165.155.234: SSH did not finish within 30s`, then `exit 124; it answers, so it is left alone`; the sweep went on through the six other Test instances and finished in 42 s. Script restored.                                                                                                                                                                             |
| 2, forced hang in a real create                | #580       | **Passed.** Mount script finished at 19:18:43; the hook left `sleep 600`; at 19:21:41 (180 s) `Mount script: SSH did not finish within 180s (time limit reached)`. Step failed; admin email sent; member got the failed-mount comment.                                                                                                                                                                                                                                     |
| 3, instance stops answering mid-call           | #581       | **Passed.** Port 22 dropped on the instance at 19:33:17 while the hook held the call; the step ended at 19:34:28 (71 s) with ssh exit 255, not at the 180 s limit. GitHub shows only the generic "Process completed with exit code 255" for this case; the failed-mount comment and admin email follow as for any failure.                                                                                                                                                 |
| 4, job reaches `timeout-minutes`               | #582       | **Gap found.** The job was cancelled at 2:00 ("The job has exceeded the maximum execution time of 2m0s"). Only the `always()` outcome step ran: 👎 on `/create`. No admin email (that step is `failure()` only), no explanation for the member (progress comment left at ⏳), and the half-set-up instance stayed ACTIVE with its address. Report Dropped Command skipped it on purpose ("cancelled after its worker job had started; that run reported its own outcome"). |

Test 4 answers the open question in section C: a job stopped by
`timeout-minutes` reports nothing on GitHub beyond the 👎. Members do not read
the request issue, though; they see the portal card and the assistant. There the
stopped create is read as a failed `/create`, but the runner reports the server
as set up (the workflow marks `exoSetup` complete before the MyDrive mount), so
the assistant says "Nothing needs to be done". The same happens after an
ordinary failure of any step after that point.

## 10. Follow-up (not in this change)

The gap test 4 found is handled in a separate design note,
`SETUP_FINISHED_SIGNAL_DESIGN.md`. It is a sign that MorphoCloud's own setup
finished, set at the end of a create, so the portal and the assistant can tell a
finished instance from one that stopped partway and guide the member to delete
it and create a new one. This change ships with test 4's gap as it is.

## 11. Regression: launchers left untrusted (found 2026-11-21, Test-Instances #592)

**What the member saw.** On #592, created with this change, the desktop showed
`Slicer.desktop` and `ExtendInstanceSession.desktop` as file names with a red
"not allowed" emblem, instead of "3D Slicer" and "Extend Instance Session".

**Cause.** Change A runs the launcher-trust `gio set` in a transient unit. When
the unit's main process ends, systemd kills every process in the unit. That
includes the `gvfsd-metadata` that `gio` started on the private bus. That
process saves the mark when its bus closes, and it was killed before it
finished. On #592, two half-written `root.XXXXXX` files from 19:57 (the trust
step) were left in `~/.local/share/gvfs-metadata`.

The mark was set again later by the login-time autostart (section 4). But the
desktop reads the mark only when it draws an icon, and by then it had drawn
them. The code comment "before the first desktop session" was also wrong: the
image's GNOME session was already running during setup, as section 4 says.

**Evidence (on #592, with its desktop session running, scratch files only):**

| Method                                                               | Mark visible afterwards, to the session and to a fresh read |
| -------------------------------------------------------------------- | ----------------------------------------------------------- |
| A as shipped (private bus in a unit)                                 | no; a new half-written `home.XXXXXX` file was left behind   |
| Before this change (private bus, no unit, 54b6238)                   | yes, still at +75 s                                         |
| Fixed A (the unit waits for its own `gvfsd-metadata` to exit, ≤ 5 s) | yes, 3 of 3, still at +60 s; each run about 0.2 s           |

Without a running session (scratch home), both A and the old method kept the
mark. The loss needs the session's own metadata service on the same files.

**Fix.** The unit runs a small helper written to `/run` by the mount script. It
marks the launcher through the private bus, then waits up to 5 s for the unit's
own `gvfsd-metadata` (found through the unit's cgroup, so the member's session
is not touched) to exit by itself. Then the unit ends, and systemd still kills
`gvfsd-fuse`, the #575 cause. RuntimeMaxSec=20 still bounds the whole helper.
The 180 s limit on the mount script is unchanged.

**First test (#593) found a bug in the fix.** `/run` is mounted `noexec`, so the
unit could not execute the helper ("Failed to find executable
/run/mc-trust-launcher.sh: Permission denied"). Both launchers printed "Could
not mark ... as trusted", setup went on, and the login-time autostart marked
them after the reboot. The #592 runs had used a helper in `/tmp`. The unit now
runs `/bin/sh /run/mc-trust-launcher.sh`, tested on #593 with its session
running: 3 of 3 saved, still there at +45 s, no half-written files.

**Test.** One create on Test-Instances as amm554 through the portal. After
setup, the desktop shows both launchers by their names without the emblem, and
`~/.local/share/gvfs-metadata` has no `root.XXXXXX` or `home.XXXXXX` files.

**Second test (#594): the mark was set, the emblem stayed.** With the helper
fixed, setup marked both launchers before the reboot (`run-u131`/`run-u132`,
"Deactivated successfully"), and no half-written files were left. After the
reboot the desktop still drew both launchers with the emblem. In the desktop's
own environment every one of its checks passed (valid file, executable, mode
755, `metadata::trusted` true, Desktop folder 750). The desktop decides when it
draws a file and reads it again only when the file changes; touching the two
launchers on #594 cleared the emblem at once (confirmed by the maintainer). So
whatever the desktop saw when it first drew them (the mark lost when the
pre-reboot session shut down, or set later by the login autostart), a change to
the files after the mark fixes it.

**Fix, part 2 (approved 2026-11-21):** the login autostart in the Exosphere fork
(`ansible/roles/slicer/autostart-update-desktop-settings.desktop`, installed by
Ansible at create) touches both launchers after marking them (exosphere#13,
`5d0c289`). exosphere#13 was merged with a merge commit (`547bd530`, so
`5d0c289` stays reachable), and `cloud-config` pins `547bd530` on
`share-prototype`. Instances check out the pin from a plain clone, so the pin
must always be on a branch: when exosphere#13 merges, move the pin to the merge
commit on `share-prototype` before the `launcher-touch` branch is deleted. The
setup step above stays: it no longer kills the metadata service before it saves.

**Result (2026-11-21, Test-Instances #595, amm554, MWF fbe5c14 + exosphere
5d0c289): PASS.** Setup marked both launchers before the reboot (no "Could not
mark" lines, no half-written metadata files). After the reboot (22:35:18) the
login autostart marked and touched them (22:35:33). The maintainer confirmed "3D
Slicer" and "Extend Instance Session" without the emblem; nothing was touched by
hand.
