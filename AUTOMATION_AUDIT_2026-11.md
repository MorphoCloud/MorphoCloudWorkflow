# Automation audit, 2026-11-06

Why: two collisions between independent automations reached members on one day
(Test-Instances#523, a create lost its address to the policing script; the
portal refused a whole runner report over `powering-off`). Instead of waiting
for the next one, every actor that changes instances, addresses, shares or the
request issue was inventoried and checked against every other actor.

Scope: MorphoCloudWorkflow `share-prototype` (796250b) and
morphocloud-data-portal `share-prototype` (2db7a50) — what reaches production on
2026-11-23. Production today runs `main` plus hotfixes; where a finding applies
there now, it says so.

Status of each finding: **V** = verified by reading the code myself; **T** =
traced by the audit (both code paths cited) but not yet re-read by me; **P** =
possible, depends on timing or upstream behavior not pinned down. Every T and P
is re-read before its fix is written.

Nothing below has been fixed. This document is for approval first: no class is
coded until its fix is approved.

## The actors

| Actor                                                                      | Cadence               | Lock it holds                                       |
| -------------------------------------------------------------------------- | --------------------- | --------------------------------------------------- |
| Member commands (create, shelve, unshelve, delete, email)                  | on comment            | per-issue group `morphocloud-instance-issue-<n>`    |
| /renew, /extend                                                            | on comment            | **none**                                            |
| control-instance-from-workflow (all automated commands)                    | on dispatch           | per-issue group                                     |
| create-instance-from-workflow (workshop)                                   | on dispatch           | `create-instance-<n>` — **not** the per-issue group |
| resume-setup                                                               | on dispatch           | per-issue group                                     |
| automatic-instance-shelving                                                | \*/5                  | its own workflow only                               |
| follow-through                                                             | \*/5                  | its own workflow only                               |
| update-request-status-label (sweep)                                        | \*/5                  | its own workflow only                               |
| reconcile-instances                                                        | :07, :37              | its own workflow only                               |
| close-expired-issues (End of Life)                                         | daily 12:00           | its own workflow only                               |
| workshop-backfill                                                          | \*/10                 | its own workflow only                               |
| police-floating-ips.py                                                     | \*/5, acquire runner  | flock against itself only                           |
| portal runner `pickup` (claims, ensure, state push, auto-shelve, restores) | every minute          | flock against itself only                           |
| portal web app (commands, recreate, reset, Check again)                    | on request            | none across requests                                |
| portal worker (resets, health, alerts)                                     | loop                  | single thread                                       |
| mc_migrate_volume                                                          | per member, migration | none                                                |

GitHub's concurrency group keeps **one running and one pending** job; a newer
arrival cancels the pending one. The per-issue group is therefore a mutex, not a
queue.

## Findings by class

Each class gets one fix, in one PR, with a test that replays the interleaving.

### Class 1 — Nobody owns a floating IP between "picked" and "attached" (critical)

| ID  | Finding                                                                                                                                                                                                                                                                                                  | Impact                                                                                                                                                                                                                                                                                                             | Status                                                                           |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| 1a  | Unshelve jobs run `runs-on: self-hosted` (control-instance.yml:190, control-instance-from-workflow.yml:61, delete-instance-and-volume.yml:92), so they run on either runner, beside creates on the acquire runner. The planned acquire lane (decision 24, RUNNER_ARCHITECTURE_PLAN §3) was never built.  | Two address-takers run at once.                                                                                                                                                                                                                                                                                    | V                                                                                |
| 1b  | create-ip takes `.[0]` of the unattached addresses with no claim (create-ip:45).                                                                                                                                                                                                                         | Two takers pick the same address.                                                                                                                                                                                                                                                                                  | V                                                                                |
| 1c  | The create's associate step treats "the address has any port" as "already associated" (create-instance:691-698) without checking the port is its own server's.                                                                                                                                           | A create that lost its address to an unshelve **reports success with the other member's address; setup then SSHes there, writes the creator's CephFS key onto the other member's instance and reboots it** (setup-instance:308-309, 421, 804-848). Cross-member exposure. Production is exposed now (two runners). | V (1c code), T (setup consequence)                                               |
| 1d  | The reverse order: the unshelve's associate skips on the same check, its end-state check fails, and the instance is left ACTIVE with no address and no re-shelve.                                                                                                                                        | Billing, unreachable instance.                                                                                                                                                                                                                                                                                     | T                                                                                |
| 1e  | Policing deletes an over-the-hold spare a create picked before its server exists (`starting` is still 0); the 600 s idle check reads `updated_at`, which picking does not change. #378 narrowed but did not close #523.                                                                                  | #523 again.                                                                                                                                                                                                                                                                                                        | T                                                                                |
| 1f  | Policing unsets the address of a shelve that just reached SHELVED_OFFLOADED and posts "released" on the issue; `server remove floating ip <server> <ip>` ignores the server (openstackclient 10.4.0), so if another taker attached that address meanwhile, the shelve strips it from the other instance. | Another member's running instance loses its address.                                                                                                                                                                                                                                                               | T (client behavior verified in source by the audit, not on the runner's version) |
| 1g  | Policing acts on IDs from a snapshot up to ~3 min old (alert waits) without re-reading `Port`.                                                                                                                                                                                                           | Deletes an address just attached.                                                                                                                                                                                                                                                                                  | P                                                                                |
| 1h  | create-ip's non-routable sweep deletes policing's fresh mint; policing's own delete then raises, the run aborts and loses its state.                                                                                                                                                                     | A lost drought alert.                                                                                                                                                                                                                                                                                              | P                                                                                |
| 1i  | Found in review: after a failed unshelve, "Shelve again" runs `floating ip unset --port <picked id>` (control-instance:1321) on the address it picked, wherever that address is now bound.                                                                                                               | In the 1d collision, it detaches the address from the **other member's running instance**.                                                                                                                                                                                                                         | V                                                                                |

**Fix (one PR) — design, 2026-11-06.** Jetstream2's Neutron has the
`revision-if-match` extension, and openstacksdk on all three runners (4.11 on
Test, 4.14 on production) passes it as `if_revision` on `update_ip` and
`delete_ip`. That gives a compare-and-swap at the source of truth: "attach this
address to my port only if it is still at the revision I read (unattached)". Two
takers on two runners cannot both win; the loser gets HTTP 412 and moves on. No
lock, no lane, no job routing, nothing that queues an unshelve behind a
20-minute create. This replaces the flock design above it was approved as.

1. **No early pick.** Nothing holds an address between steps. The create's early
   `create-ip` becomes `fip.py ensure-spare`: make sure one routable unattached
   address exists (mint if not), so an exhausted pool still fails before a
   volume and a server are built. It reserves nothing.
2. **`fip.py attach --server NAME [--prefer IP]`**, the only way an address is
   attached (create associate; unshelve, decision 25; the reconcile repair,
   which is the unshelve no-op path). Under a deadline:
   - read the server's port;
   - if a routable address already has `port_id ==` that port: done
     (idempotent);
   - else for each unattached routable address (the preferred one first):
     `update_ip(port_id=mine, if_revision=r)`; 412 or 404 → next address; any
     other error (Jetstream2's spurious 500) → re-read it: mine → done,
     unattached → retry, someone else's → next;
   - none left → mint one (non-routable → delete, as decision 24) and attach it
     the same way; the pool is dry → exit with "no address";
   - success is decided only by a fresh read showing `port_id ==` my port.
     Output: that address and its id, which setup and the email then use.
   - keep one in hand afterwards (decision 24), best effort.
3. **`fip.py detach --server NAME`**, the only way an address is detached
   (shelve; the unshelve's stuck-address release; shelve-again after a failed
   address step): for each address whose `port_id ==` this server's port,
   `update_ip(port_id=None, if_revision=r)`; on 412 re-read: no longer mine →
   done. It never names an address, so it cannot strip another instance's.
4. **Policing** re-reads each address immediately before acting and acts with
   `if_revision` from that read: delete only if still unattached; unset only if
   still on the same port. A changed address is skipped this tick. create-ip's
   own non-routable sweep is removed (policing owns it), closing 1h.
5. The create sets `exoFloatingIpReuseOption` after the attach, to the id
   actually attached.

`fip.py` lives in `.github/actions/fip/` (vendorized with the actions) and
imports openstacksdk from the runner's venv; policing imports the same module.

**Tests before any run on Jetstream2:** a fake Neutron that enforces revisions,
and tests replaying 1a–1h: two attachers on one spare; a create and an unshelve
interleaved at every step; policing's delete and unset racing an attach; a
shelve detaching while another server attaches its old address; the spurious 500
that did apply; an exhausted pool.

**Tests on Jetstream2 (Test), presented for approval before running:** (a) one
fresh address on BIO240357: update its description with a stale revision →
expect 412, delete with a stale revision → expect 412, then delete it; (b) as
amm554: create, shelve, unshelve, delete on one request; then a create on one
amm554 request started while an unshelve runs on another, both picking from a
single spare, expecting both instances to end with distinct addresses and the
logs to show one 412.

**Revised 2026-11-06 after testing on Jetstream2 (approved by the maintainer).**
Throwaway addresses and ports on BIO240357, all deleted: (1) openstacksdk drops
`If-Match` when the same resource object is reused with the same revision (it
sends only changed headers), and Neutron then accepts a stale write; with the
address's id it always refuses (412). fip.py and policing pass ids, and both
test fakes refuse objects. (2) In sequence, every stale attach, detach, update
and delete is refused. (3) Two attaches of one address to two ports at the same
instant: in 4 of 6 rounds both succeeded and the last writer kept it (likely
Neutron retrying the conflicting write internally without re-checking the
condition). So the compare-and-swap closes every stale-snapshot case (policing,
shelve, stuck release, 1e-1i) but not two takers in the same second. Production
history (2026-10-01 to 11-06, 199 jobs) shows two overlapping jobs on different
runners, both shelves; no two address takers ever overlapped. The residual case
is therefore contained, not raced: **nothing logs in to an instance by address
until the instance proves it is the server meant** (`fip.py verify`: the
metadata service's `uuid`, which the guest cannot change, equals the server id).
Setup verifies before its first login and replaces an address that answers as
another server; the access email verifies before mailing the address or reading
the password; /extend reads its address from Neutron (it read Nova's cache:
another instance of class 5) and verifies before resetting the timer.
Auto-shelve and uptime read Neutron right before a read-only login and are left
as they are. A 15-second re-check after each attach was considered and dropped:
it would slow every start for a case never observed, and the identity check
already contains the harm.

**Production:** production's control action still binds before the unshelve
(decision 25 is not there), so its hotfix is a separate, smaller diff on the
`prod-hotfix` baseline: the same `fip.py attach`/`detach` in the create's
associate, the pre-unshelve bind and the shelve. Shown to the maintainer as a
diff against the last vendorized SHA before it is pushed.

### Class 2 — Automation decides from a stale read and nobody re-checks (high)

Automated actors decide outside the per-issue lock and dispatch a command; the
command runs minutes later and does not re-check why it was sent.

| ID  | Finding                                                                                                                                                                           | Impact                                                                                                                                    | Status |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 2a  | Auto-shelve's timeout dispatch is not re-checked; /extend has no lock.                                                                                                            | Member clicks Extend, is told "Session extended", then is shelved.                                                                        | V      |
| 2b  | End of Life snapshots all labels at 12:00, deletes volumes directly and dispatches a delete; /renew has no lock and no check.                                                     | Member is told "Renewal applied", then the instance (and, in volume mode, the data volume) is deleted.                                    | V      |
| 2c  | The status-label sweep writes from a server list taken at its start (update-request-status-label:31-58).                                                                          | `status:deleted` overwritten by a stale `status:active`, permanently; workshop backfill then recreates an instance the organizer deleted. | V      |
| 2d  | Auto-shelve, reconcile repair and follow-through all act on "ACTIVE without an address" from separate reads; queue order decides.                                                 | An access email, then a shelve; or "being shelved", then it comes back up.                                                                | T      |
| 2e  | Follow-through retries from a label read taken before the member's own new command removes the marker.                                                                            | The retry undoes the member's command (their unshelve, then a retried shelve).                                                            | T      |
| 2f  | Auto-shelve's unreachable/SHUTOFF branch fires during a member's shelve or delete (the delete stops the server first).                                                            | False "not answered on the network" comments; a failed shelve on a closed request.                                                        | T      |
| 2g  | Reconcile's single address snapshot ages across its loop; a create or unshelve still attaching looks broken.                                                                      | Duplicate access email, false `invariant-violation`.                                                                                      | T      |
| 2h  | The portal's card ignores Nova's task state (instances.py:81-95): an automated unshelve or delete in flight shows "Shelved"/"Active" with the buttons and **Reset storage** open. | Duplicate command; **reset empties the share while the instance mounts it**.                                                              | V      |

**Fix (one PR in each repo):** every automated dispatch carries its reason
(`timeout`, `unreachable`, `expired`, `retry:<cmd>`, `repair:no-address`), and
control-instance re-checks that reason under the per-issue lock before acting;
if it no longer holds, it exits with a log line and no comment. /renew and
/extend join the per-issue lock. The status-label sweep re-reads the server
right before each write and never replaces `status:deleted`. The portal shows
any non-empty task state as working and blocks commands and reset meanwhile.

### Class 3 — The lock is a mutex with a one-slot queue (high)

| ID  | Finding                                                                                                                                                                                                                                                               | Impact                                                                                 | Status |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------ |
| 3a  | A newer arrival evicts the pending job. Automation evicting a member is reported to the member as "a later command took its place"; a member evicting automation is silent (a lost End of Life delete, a lost retry with its marker already consumed, a lost repair). | Commands vanish; wrong messages.                                                       | T      |
| 3b  | Workshop creates use a different group from per-issue commands.                                                                                                                                                                                                       | An organizer's delete runs beside a create; the queued create re-creates the instance. | T      |
| 3c  | Member control commands have no in-flight refusal; authorize timing varies 2-11 s.                                                                                                                                                                                    | Shelve then Unshelve a few seconds apart can run reversed.                             | P      |

**Fix:** after class 2, automated dispatches are safe to repeat, so each
automated actor re-dispatches on its next tick if its command did not run; a
dropped automated run is logged for the admins, never blamed on the member.
Workshop creates take the per-issue group. Control commands refuse while one is
in flight, as create already does.

### Class 4 — Markers are removed by actors that did not set them (medium)

| ID  | Finding                                                                                                                   | Status |
| --- | ------------------------------------------------------------------------------------------------------------------------- | ------ |
| 4a  | Any successful command clears `needs-admin` and `followup-count:*`, whoever set them; reconcile clears `needs-admin` too. | T      |
| 4b  | Any control run removes `followup:setup`; setup is then never resumed and the status shows building forever.              | T      |
| 4c  | `exoSetup=complete` is written before the mount and reboot; a resume after that point refuses.                            | T      |
| 4d  | `invariant-violation` and `needs-admin` stay on a deleted instance's issue (reconcile walks only existing servers).       | T      |

**Fix:** each marker has one owner that sets and clears it; a delete's close-out
clears all; `exoSetup=complete` moves to after the reboot check.

### Class 5 — The runner reads Nova's cached address (critical)

| ID  | Finding                                                                                                                                             | Impact                                                             | Status                                 |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | -------------------------------------- |
| 5a  | The runner's snapshot takes the address from Nova's `Networks` (runner:575) — the cache decision 29 replaced with Neutron in the workflows.         | A stale address is probed (the wrong host answers "reachable") and | V                                      |
| 5b  | without an `exoPw` tag, `passphrase()` SSHes to that address with `accept-new` and caches what it reads as this server's password (runner:588-607). | **Another member's password and web link pushed to this member.**  | V (code), P (needs a recycled address) |
| 5c  | `PRIVATE_IP` treats 172.16/12 as private, so a server on a non-routable 172.17 floating IP is never probed or auto-shelved.                         | A dead instance stays "Active".                                    | T                                      |

**Fix — design, 2026-11-06 (portal repo, runner/mc_share_runner.py):**

1. **Addresses from Neutron.** Each cycle reads `floating ip list` once (one
   call) and keys attached addresses by their fixed IP; the fixed IP comes from
   Nova's `Networks`, which is stable for the life of a port (only the floating
   part of that cache goes stale). A server's address is the floating IP bound
   to its fixed IP, or none. This is how the policing script already reads them.
2. **A non-routable address is dead.** An attached address outside 149.165 is
   reported with `reachable: false` and no probe, so the card shows Unreachable
   and decision 23 shelves it; the next unshelve gets a routable one.
3. **The password only from this server.** `passphrase()` keeps the `exoPw` tag
   first. Without it, the same SSH session also reads the instance's
   `meta_data.json` and the password is used and cached only when its `uuid` is
   this server's id; a mismatch is logged and alerted (a recycled address), and
   nothing is pushed. Host-key checking stays as it is: a changed key already
   fails safe.

**Tests:** a snapshot where Nova's cache shows an old address and Neutron shows
none or another; a 172.17 attached address; a passphrase read where the metadata
uuid differs (refused, alerted) and matches (cached). **On Test:** install on
the Test runner, compare one cycle's snapshot against
`openstack floating ip list` by hand, and watch the card for amm554's request.

### Class 6 — Portal state machine races (medium)

| ID  | Finding                                                                                                                                                        | Status |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 6a  | Two requests pass the "no command working" check before either records its command: duplicate commands (double click, two tabs, Try again plus a button).      | T      |
| 6b  | Two card builds both see a 👎 first: two admin emails.                                                                                                         | T      |
| 6c  | Try again: the 30-minute check runs before the success check (instances.py:537 vs 543); a delete seen late drops the recreate and emails a false failure.      | V      |
| 6d  | `finished_at` is written only when the owner's card polls; a member who closes the tab holds off auto-shelve for 3 h.                                          | T      |
| 6e  | An auto-shelve is recorded in `state` before the runner dispatches it; a timeout after the record loses it forever, and "dispatched" is reported as "shelved". | T      |
| 6f  | The worker's 600 s timeout fails a request in `posting` and emails "/create was not posted" while the create was posted.                                       | T      |
| 6g  | Health check overwrites `expiring`/`expired` with `unavailable` from a stale read, leaving the member unable to create storage.                                | P      |
| 6h  | A long share `ensure` (migration requests 32) stalls the state report for every member.                                                                        | T      |

**Fix:** command recording and outcomes become conditional updates (claim before
post); recreate checks success before age; auto-shelve completes only when a
later snapshot shows the instance shelved; the state push runs on its own lock,
not behind ensures; health writes are conditional on the state it read.

### Class 7 — Share lifecycle vs. instances and the migration (high for 2026-11-23)

| ID  | Finding                                                                                                                                           | Status |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 7a  | `mc_mounted_by` is written at the end of setup, so a share mounted by a create in progress looks unheld; the daily expiry can delete it.          | P      |
| 7b  | Expiry ignores a pending Keep my storage press.                                                                                                   | T      |
| 7c  | The migration calls `ensure` without `ensure_lock`; with a member's Create my storage at the same time, both create a share and both then refuse. | T      |
| 7d  | The maintenance switch does not close Reset storage; reset and the migration's copy can write the same share.                                     | P      |
| 7e  | The migration reads the old server's state once; the sweeps keep acting on it during the copy.                                                    | P      |
| 7f  | A stale `--warned` mark can let a share expire with no warning in the next idle period.                                                           | P      |

**Fix:** the holder is recorded before the mount; expiry honors a pending Keep;
the migration takes `ensure_lock`, the maintenance switch closes reset, and the
sweeps skip an issue under migration.

### Class 8 — Failures that tell nobody (medium)

Uncaught SSH timeouts in the runner (lost claims and auto-shelves), refusal
alerts throttled per kind rather than per issue, unaudited session refusals,
password-restore failures logged only, a 👎 reaching the admins only when the
owner loads the page, the worker blocked during a reset. **Fix:** each becomes
an alert or an audit row, per the no-silent-failures rule. All T.

## Order of work

1. Class 1 and class 5 first: they can expose one member's instance or password
   to another, and class 1 applies to production now.
2. Class 2 and class 3 together (the reason re-check is what makes re-dispatch
   safe).
3. Class 7 before 2026-11-23 (the migration).
4. Classes 4, 6, 8.

Each class: design note in this file, code, tests replaying the interleavings
above, one review, Test as amm554 with automatic actions report-only first, then
merge. Class 1 also needs a production hotfix (production runs two runners now);
the others reach production with the 2026-11-23 merge.
