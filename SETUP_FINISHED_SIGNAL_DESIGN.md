# Design note: a sign that MorphoCloud's setup of an instance finished

Status: approved 2026-11-20 (section 9); being built.

Origin: test 4 of `SETUP_SSH_BOUNDS_DESIGN.md` (Test-Instances #582). Code
references are to MorphoCloudWorkflow (MWF) `share-prototype` at `aabe3f5`, the
data portal `share-prototype` at `c299183`, and the Exosphere fork
`share-prototype`.

## 1. What happened

Test 4 stopped a create with `timeout-minutes`. On GitHub the member's `/create`
got a 👎 and nothing else. Members do not read the request issue, though; they
see the portal card and the assistant. Following that up in the code showed a
wider gap. When a create fails or is stopped after the instance's own setup
finished (for example in the MyDrive mount step), this is what the member gets:

1. The portal reads the 👎 as a failed `/create`, so the assistant speaks first
   (portal `app/guidance.py`, `attention()`).
2. The runner's report to the portal says the server is ACTIVE and its setup is
   done. It takes "setup done" from the `exoSetup` server property
   (`runner/mc_share_runner.py`, `snapshot()`). The workflow writes
   `exoSetup=complete` right after the instance's own setup script finishes
   (`.github/actions/setup-instance/action.yml:168`), and that is before the
   MyDrive mount, the reboot, the check after the reboot, and the access
   details.
3. So the card shows "Active", and the assistant's rule table picks "fine":
   "Your instance is running. Nothing needs to be done." There is no button.
4. The portal reconciler also sees "ACTIVE, setup done, no access details" and
   asks the runner to push the access details (`admin/cli.py`, `need.append`),
   so the card fills in the details of an instance without MyDrive.

The member is left with an instance that looks ready and is not, and no way
forward. This follows from the code above; it was not watched on the portal.

Correction (2026-11-20, during the build): #582 itself was stopped 26 s into
"Setup instance", inside the setup poll, before `action.yml:168`. Its `exoSetup`
was still `waiting`, so for #582 the portal would have shown "Building" and
offered Delete, not "fine". #582 is how the gap was found, not an example of it.
#576 (mount failed after the poll) is one.

## 2. The class of problem

Any create that ends after `action.yml:168` and before the last step of setup.
That covers:

- a step that fails: the MyDrive mount, the reboot, the check after the reboot
  (#576 failed its mount this way on 2026-11-20 and would have got the same
  "fine");
- a job stopped by `timeout-minutes` or cancelled by hand during that stretch
  (the 35-minute limit firing on a hang in the mount step, for example);
- in volume mode, the volume steps (attach, copy, move home, Uploads, Renviron),
  which sit in the same stretch.

A second way into the same state: the Exosphere web client writes `exoSetup`
too. When anyone has the project open in Exosphere, the client reads each new
server's console log and sets `exoSetup=complete` as soon as the instance's own
setup script prints its complete line (Exosphere fork `src/State/State.elm`,
`ReceiveConsoleLog`, `requestSetServerMetadata`). Our servers carry
`exoServerVersion` and `exoClientUuid`, so Exosphere treats them as its own. So
even a create that is still running, or one whose setup poll timed out, can be
marked complete early by an admin's open browser tab.

So `exoSetup` means "the instance's own setup finished". Nothing records "the
whole MorphoCloud setup finished", yet several readers use `exoSetup` as if it
did.

## 3. Evidence inventory (every writer and reader)

Writers of `exoSetup`:

| Where                                                      | Writes                         | When                                                 |
| ---------------------------------------------------------- | ------------------------------ | ---------------------------------------------------- |
| `create-instance/action.yml:462`                           | `waiting`                      | server create                                        |
| `setup-instance/action.yml:136`                            | `error`                        | the instance's setup script reported an error        |
| `setup-instance/action.yml:168`                            | `complete`                     | the instance's setup script finished; before MyDrive |
| `setup-instance/action.yml:1005` (Set instance properties) | `complete`                     | last setup step, after the check after the reboot    |
| `follow-through.yml:137`                                   | `error`                        | follow-through sweep sees the setup script's error   |
| Exosphere web client                                       | `running`, `complete`, `error` | any open Exosphere session, from the console log     |
| `scripts/bake-image.sh:68`                                 | `waiting`                      | the image bake VM only (not an instance; no reader)  |

Readers that take `complete` to mean "ready for the member":

| Reader                                                      | What it decides                                                                                                                     |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| portal runner `snapshot()` → `setup_done`                   | card "Active" vs "Building"; the assistant's verdict; re-push of access details; auto-shelve of unreachable instances (decision 23) |
| `update-request-status-label.yml:32–45`                     | `status:active` vs `status:building` (the portal's fallback when no runner report)                                                  |
| `check-instance-exists/action.yml:88–92` (`setup_complete`) | the advice in "Instance Already Exists" on a second `/create` (#290)                                                                |
| `follow-through.yml:114`                                    | "setup already finished": drops the follow-through marker, so the rest of setup never runs                                          |
| `resume-setup.yml:67`                                       | "nothing to resume": refuses to run the rest of setup                                                                               |
| Exosphere UI                                                | the admin's view of the server; not a member surface                                                                                |

`setup-instance`'s own poll reads the console log, not the property
(`cloud-config:146`), so it is not a reader.

## 4. Proposed change

### A. A new server property written only by the workflow: `mcSetup`

1. At server create, next to the `exoSetup` seed
   (`create-instance/action.yml:462`), add `--property mcSetup=pending`.
2. In the last setup step, "Set instance properties"
   (`setup-instance/action.yml:998`), also set `mcSetup=complete`. That step
   runs after the check after the reboot, in share mode and volume mode, for
   individual, workshop and course creates, and for `resume-setup.yml`, which
   ends through the same action.
3. Nothing else writes `mcSetup`. Exosphere does not know it and leaves it
   alone. A failure or a stop anywhere before the last step leaves it at
   `pending`.
4. `exoSetup` stays as it is, so Exosphere's own view is unchanged.

The access details come after this step (the Send email step in the create job).
If they are lost, the portal reconciler already re-pushes them for an instance
that is set up (#467's fix), so the property marks the right point.

### B. One rule in every reader

"Setup finished" =

- `mcSetup` present: finished only when `mcSetup` is `complete`;
- `mcSetup` absent: the old rule, `exoSetup` status `complete`.

The fallback keeps every instance created before this change, and every instance
made by a course repo pinned to an older branch, reading as it does today. No
backfill of production servers. The readers changed: the portal runner's
`snapshot()`, `update-request-status-label.yml`, `check-instance-exists`,
`follow-through.yml:114` and `resume-setup.yml:67`. The four MWF readers share
one jq function, `mc_setup_done` in `.github/actions/setup-state/mc-setup.jq`
(next to `.github/actions/ssh/mc-ssh.sh`), so the rule exists once in MWF and
once in the portal runner. The `error` checks on `exoSetup` stay as they are.

The function handles both value shapes and both property shapes (review of
#436):

- `exoSetup` is a JSON string (`{"status":"complete","epoch":…}`) and is
  unwrapped to its `status`; `mcSetup` is a plain word and is compared as it is.
- Some `openstack` client versions print a server's whole properties as one flat
  string (`exoSetup='…', mcSetup='pending'`) instead of an object.
  `check-instance-exists` already guards against this; the shared function
  carries that guard to all four readers. `update-request-status-label.yml` read
  `.Properties.exoSetup` directly before and gets the guard with it.

### C. What the assistant says for a create that did not finish

With A and B, a stopped or failed create reads as ACTIVE and not set up. The
rule table already maps that to "recreate" (`guidance.py`, `verdict()`), with a
Delete button only. Two changes:

1. Offer "Try again" next to Delete, when the request's type is known. It
   deletes the instance and creates a new one of the same type
   (`instances.request_recreate`, already used for the shortage case), behind
   the existing confirmation "Delete this instance and create a new {type}? Your
   files in your storage are kept." Members keep the consent step for a delete
   (decision 19).
2. Count tries. Today "recreate" is picked before the twice-in-a-row check, so a
   create that keeps failing the same way loops the member. Proposed: the second
   failed create in a row gets "contact" (Send to the MorphoCloud team) instead.

Proposed line, for the maintainer to reword: "Setting up your instance did not
finish. Click Try again to delete it and create a new one of the same type."

### Not in this change

- No admin email for a stopped job. The incident store already harvests the
  record of every run, "including crashed, cancelled and dropped ones"
  (INCIDENT_STORE_PLAN.md, the Envelope row); a hang that got past the per-call
  time limits is found there.
- No automatic delete without the member's click.
- The card label. The card keeps showing "Building" for an instance whose create
  failed, while the assistant says it did not finish. This is how an
  `exoSetup=error` create already looks today. Changing the card is a separate
  question.
- A create whose runner died mid-job (no 👎 at all) stays "still working" on the
  card. That is the dropped-command class, not this one.

## 5. Other actors on the same servers

| Actor                                           | Touches                                                 | Effect of this change                                                                                       |
| ----------------------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Exosphere web client (admins)                   | writes `exoSetup` from the console                      | no longer able to mark a MorphoCloud create ready early                                                     |
| Follow-through sweep (`follow-through.yml`)     | reads; writes `exoSetup=error`                          | stops dropping the marker on an Exosphere-written `complete`; still resumes when the console says complete  |
| Resume Setup (`resume-setup.yml`)               | reads; runs the rest of setup                           | refuses only when `mcSetup=complete`; its own run ends by writing it                                        |
| Status-label sync                               | reads                                                   | `status:building` until `mcSetup=complete`                                                                  |
| Portal runner (`pickup`, every minute)          | reads                                                   | `setup_done` false until `mcSetup=complete`; no access details pushed and no portal auto-shelve before that |
| Instance's idle auto-shelve (on the instance)   | none                                                    | unchanged; still shelves an idle half-built instance                                                        |
| Shelve / unshelve                               | Nova keeps server properties                            | an unshelved instance keeps `mcSetup=complete`                                                              |
| Course repos on pinned branches                 | write only `exoSetup`                                   | no `mcSetup`, so the fallback rule applies; same as today                                                   |
| Production instances created before the change  | have no `mcSetup`                                       | fallback rule; same as today                                                                                |
| Admins by hand (`openstack server set`)         | can set either property                                 | an admin can mark a repaired instance finished with `mcSetup=complete`                                      |
| E2E tests (MorphoCloudE2ETests `_lifecycle.py`) | read `exoSetup` before their own in-guest check         | unchanged: `exoSetup` is still written; the E2E in-guest probe checks the rest. Not changed here            |
| Kubernetes POC (`morphocloud-k8s`)              | its own copy of the share runner; no per-member servers | unaffected; not changed here                                                                                |

## 6. What the member and the admins see

- Member, create succeeds: no change.
- Member, create fails or is stopped after the instance's own setup: card
  "Building", no access details, the assistant says setup did not finish, with
  Try again and Delete. A second failure in a row: Send to the MorphoCloud team.
- Admins: a failed step still emails them as today. A stopped job is in the
  incident store. An escalation from the assistant carries the run link when a
  bot comment on the request has one.

## 7. Tests (Test-Instances, BIO240357_IU, as amm554 through the portal)

Portal unit tests for the rule (present/absent/pending/complete, flat-string
properties), `verdict()` and `actions()`; a jq test for the MWF function.

On Test-Instances (about 4 m3.xl creates, about 1 h of m3.xl time):

1. Normal create. `mcSetup=pending` at boot, `complete` at the end; card Active
   with access details; label `status:active`.
2. Stopped create, after the setup poll. TEST-ONLY branch: a `sleep 600` hook
   holds the mount script on requests labeled `test:hang-mount`, and the run is
   cancelled by hand while it holds. (A 2-minute job limit, as in test 4 of the
   earlier note, stops the create inside the setup poll, before the state this
   tests; a cancel by hand ends the job the same way a time limit does.) Card
   Building, no access details, label `status:building`; the assistant's line
   with Try again and Delete. Then, on the same server, set `exoSetup=complete`
   by hand to act as the Exosphere client: still Building, still no details.
3. Try again from test 2 deletes the instance and creates a new one, which
   finishes (the new request has no hook label).
4. Failed step (the mount script hits its 180 s limit through the hook): same
   member view as test 2.
5. An instance created before the deploy (no `mcSetup`) still shows Active with
   its details.

### Results (2026-11-20/21)

The portal build under test was the Test portal's combined build (the
MorphoSource cart plus this change), last 9386ad7. All runs were on
Test-Instances, with requests opened as amm554 and viewed in the portal signed
in as amm554.

| Test             | Request     | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 Normal create  | #585        | PASS. `mcSetup` went pending → complete. The card went Building → Active with access details. Label `status:active`.                                                                                                                                                                                                                                                                                                                                      |
| 2 Stopped create | #587        | PASS. Cancelled by hand while the hook held the mount script. The server was ACTIVE, `exoSetup` complete, `mcSetup` pending. The card showed Building, no details, and "The last create did not complete". The assistant said the line with Try again and Delete. The Exosphere step is moot: `exoSetup` was already complete, which is the case this change exists for. A second failed create in a row gave "Send to the MorphoCloud team" as designed. |
| 3 Try again      | #587 → #588 | PASS. The portal deleted #587 and opened #588 as amm554, with no hook label. #588 finished with `mcSetup` complete and access details on the card.                                                                                                                                                                                                                                                                                                        |
| 4 Failed step    | #590        | PASS. "Mount script: SSH did not finish within 180s (time limit reached)", and the job failed. The server was ACTIVE, `exoSetup` complete, `mcSetup` pending. Label `status:building`. The card showed Building with no details. In a new tab, the assistant's line came with Try again and Delete at once. The verdict was unfinished, not contact, because #588 succeeded in between.                                                                   |
| 5 Old instances  | —           | PASS. Instances created before the deploy (no `mcSetup`) still read as set up.                                                                                                                                                                                                                                                                                                                                                                            |

Found during the tests, not part of this change:

- A portal tab left open across a portal restart did not speak about the #590
  failure. The card in that tab refreshed, but no `/chatbot/auto` request
  reached the portal. A new tab spoke at once. This belongs with the known issue
  that the assistant's message is lost on reload while the card still points to
  it.
- After an unshelve, the card shows Active with "Access details appear here once
  the instance is ready" for about 1–2 minutes. That is the time until the
  unshelve job sends the details (#588: unshelve finished 06:25:06, details
  stored 06:25:04). This is by design.

## 8. Delivery

Two PRs against `share-prototype`: MWF (A, B's four readers, the jq file) and
the portal (B's runner rule, C). Either order is safe: before the MWF change no
server has `mcSetup`, so the portal's new rule reads like the old one; before
the portal change the portal ignores `mcSetup`. Vendorize MWF to Test-Instances
and deploy the portal to the Test portal, then run section 7. Whether this goes
in before or after the ~11-24 cutover is the maintainer's call.

## 9. Approval

Approved by the maintainer, 2026-11-20 ("do as designed"), after the review of
#436. The review's two points are folded into section 4.B. Timing against the
~11-24 cutover is still the maintainer's call.
