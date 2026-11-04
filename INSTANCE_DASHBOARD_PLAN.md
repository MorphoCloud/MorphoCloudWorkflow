# Instance Dashboard — Manage an Instance Without the Issues Page

New users get lost in the GitHub issues interface. This adds a simple instance
dashboard to the data portal (see
[STORAGE_REDESIGN_PLAN.md](STORAGE_REDESIGN_PLAN.md), section 3), so users sign
in once and see their storage and their instances on one page.

Built and tested on **Test-Instances only**. Production needs explicit
instruction.

**Status (2026-09-25):** paused. The share-based provisioning prototype
(STORAGE_REDESIGN_PLAN.md) comes first; many instance-management choices depend
on whether data stays on volumes or moves to shares. Decisions 4 and 6 (revised)
are not built yet.

## Decisions

Agreed 2026-09-25. Changing any of these needs the maintainer's approval, and
this table is updated first.

| #   | Decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | The dashboard is part of the data portal: same VM, same sign-in, same page as the storage card.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 2   | Each instance card shows the **state** (no instance, active, shelved), the **access details** (Web connect, SSH, TurboVNC) and a **link to the request issue** for diagnostics.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 3   | No Data drop entry. The data portal replaces it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 4   | Buttons: **Create**, **Shelve**, **Unshelve**, **Renew**, **Delete**. Renew runs `/renew` and is shown only while an extension is left (as `/renew` itself decides). Delete runs `/delete_instance` only, always asks for confirmation first, and the request is **closed by the workflow** once the delete has finished (share mode; see STORAGE_REDESIGN_PLAN.md). It never touches a data volume; volumes go away with the storage redesign (per-user shares). Revised 2026-09-25.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 5   | The **passphrase is on the dashboard**, behind GitHub sign-in, **hidden by default** (dots) with a Show/Hide button, as in Exosphere; Copy works while it is hidden, and it stays shown through the cards' refresh until hidden again or the page is reloaded (revised 2026-09-26).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 6   | **Every Create opens a new request** and runs `/create` in one step, with the flavor chosen on the portal each time. A request is one instance's lifetime: the portal never runs `/create` on an existing request. Revised 2026-09-25 (this is how users change flavor).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 7   | GitHub issues and workflows stay the engine and the audit trail. The portal does not start, stop or change instances itself.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 8   | **Primary path:** sign-in moves to a MorphoCloud **GitHub App**, and buttons post the command (`/shelve`, …) on the user's issue **as the user**. The existing workflow checks apply.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 9   | **Backup / second test:** the portal checks that the user owns the issue, then the bot runs the existing `*-from-workflow` dispatch workflows. Built only if needed, or as a comparison.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 10  | The portal gets **no OpenStack credentials**. State comes from the runner's report of what Jetstream2 actually holds (decision 20) while that report is fresh and was read after the last command finished, and from the issue's `status:*` labels otherwise (the second condition: decision 20, amended 2026-10-15). Access details are pushed to the portal over the restricted SSH channel, by the workflow when it runs and by the runner's reconciliation when the workflow's push did not happen. Revised 2026-10-07 by the maintainer (was: state from the labels only, access details from the workflow only; see decision 20 for why).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 11  | Individual instances only. Courses and workshops are out of scope.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 12  | A portal-opened issue is opened with the **user's token** (the user stays the author). A workflow adds the form's three labels plus `request-source:portal`, and the request handler runs once, on that label.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 13  | The user's GitHub token is kept **in the portal's memory only**: never on disk, never in the cookie. A portal restart signs everyone out.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 14  | **One instance per user.** With per-user shares, two instances would mount the same share at once. The portal offers Create instance only when the user has no open request, and never a second one.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 15  | The GitHub App is **public** ("Any account"), so people who have not joined can authorize it and see the portal's "Not a MorphoCloud member" page with the join link. The portal's team check is the gate. The app has **no private key**.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 16  | **Availability banner:** one line at the very top with every instance type and its current Jetstream2 vacancy, colored as on morphocloud.org (4+ green, 1–3 yellow, 0 red, unknown grey). The numbers come from a read-only endpoint on morphocloud.org, the same source as its landing page. Hidden when that endpoint cannot be reached.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 17  | **Expiry dates**, shown separately: the instance (90 days after the request, 180 after `/renew`, followed by "(can be renewed)" or "(no more renewals)", computed from the issue's `expiration:*` and `renewed:*` labels exactly as the expiry sweep does) and the storage (180 days after the share was created, shown as "kept until at least"; nothing enforces it yet, see STORAGE_REDESIGN_PLAN.md decision 7).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 18  | **Only the portal (and admins) drive individual instances.** GitHub cannot stop members from opening issues or commenting, so the workflows act only on what came through the portal. Switched on per repository by `MORPHOCLOUD_PORTAL_ONLY=true`, with the portal's GitHub App named in `MORPHOCLOUD_PORTAL_APP_SLUG` (Test-Instances: `morphocloud-portal-test`); production sets neither and is unchanged. When on: (a) a command on an individual request runs only if its comment was made through the portal app (GitHub's own `performed_via_github_app` field, which users cannot fake) or by an admin; the issue's author and assignees count only through the portal; (b) a refused command gets one reply pointing to the portal (`portal-only-notice.yml`), besides the standard "not an allowed operator" note; (c) an individual request opened directly on GitHub (not through the portal app, not by an admin or a bot) is closed with a pointer to the portal before any other check. Courses and workshops are not affected. Issues stay readable for diagnosis. The E2E suites open requests on Test-Instances through the API, so they are closed while this is on. Agreed 2026-09-26, built 2026-09-26. Taking GitHub out of users' view entirely (bot-only posting, owner recorded in the issue) belongs with the one-site work. |
| 19  | **Session countdown and Extend session.** An active instance's card shows the time left before automatic shelving (session start + the request's `timeout:<N>hrs`, else 4 hours) and an **Extend session** button, which posts a new `/extend` command: the workflow resets the instance's session timer exactly as the desktop ExtendInstanceSession icon does and reports the new start. Create and unshelve report a fresh start; the 5-minute auto-shelving sweep reports every instance's session age, so an extend made on the desktop shows within 5 minutes. `/extend` typed on GitHub works the same. Agreed 2026-09-26.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

| 20 | **The runner reconciles the portal with Jetstream2** (maintainer,
2026-10-07, after Test-Instances #467: a GitHub 500 on the status-label write
ended an otherwise successful create at its last step, which also skipped the
push of the access details, so a running A100 instance showed as "No instance"
with a Create button for over an hour; the 5-minute label sync that should have
repaired the label is a GitHub-scheduled workflow, which GitHub delivers about 8
times a day on Test-Instances). Every run of the runner's `pickup` (once a
minute, under its lock, each OpenStack call with a 60-second timeout) also lists
the project's instances and sends the portal **one snapshot** through a new gate
command `state`: per issue, the status, task state, whether setup finished (the
`exoSetup` marker) and the floating IP — only an active server's, since a
shelved server still lists the address it held before shelving. No secrets. The
portal stores the snapshot in place of the previous one and stamps it with its
own clock (the runner's clock is not trusted), **clears the access details**
itself for every instance that is gone or settled in a state other than active
(nothing is decided mid-task), and answers with the issues whose details it
lacks or whose address changed. For those the runner sends the **access
details** through the existing `connection` command, passphrase from the `exoPw`
tag, else read from the instance over SSH and cached as that tag exactly as the
workflows' retrieve-metadata action does, marked as a restore so the session
timer is left alone. `connection` is an upsert on the issue — last write wins,
identical values are a no-op — and the gate runs one command per SSH connection,
so the workflow's push and this one never interleave. **The reconciliation
writes nothing on GitHub.** The `status:*` labels stay with the workflows and
the scheduled sync, and the portal no longer depends on them: the card, the
assistant's verdicts and the command gates read the runner's report while it is
fresh and fall back to the labels only when it is not. Decided 2026-10-07 by the
maintainer, instead of giving the runner a GitHub credential to keep the labels
right (a PAT would act as an admin, and admin comments are the command control
plane; a dedicated App was the safer shape, and still a credential to watch).
The cost is that an issue's label can lag Jetstream2 for hours on Test-Instances
until the sync fires, so the issue page is no longer the admin's view of a
member's state; `sudo mc-data-admin status` on the portal VM is — it prints, per
request, the runner's last report, whether the portal holds the access details
(never the passphrase) and any Check again now waiting, over the SSH and sudo an
admin already has, with no new credential and no new web surface. In steady
state the reconciliation reads two lists, sends the snapshot and writes nothing
else. **Freshness:** the portal uses the state rows only while the snapshot's
stamp is younger than 150 seconds (two one-minute cycles plus slack,
`MC_RECONCILE_FRESH_SECONDS`); older, the card falls back to the labels and says
the state is unverified, so a stopped runner can never show a gone instance as
alive. The card, the command gates and the "Create replaces an empty request"
check all read the state through one function, so they never disagree. This
costs no GitHub Actions minutes: it is the runner host's crontab, not a
workflow. The runner can read nothing through the gate but the answer to its own
snapshot (issue numbers and logins). **Scope now: the instance only.** The
session (decision 19) and the share's state are the same mechanism and will join
it through the same snapshot later, which is why it is a JSON object rather than
fixed columns. Production is untouched: the portal and its runner exist only on
Test. Built and live on Test 2026-10-07 (data portal PRs #5 and #6, the latter
the assistant's use of it, CHATBOT_PLAN.md decision 19). **Amended 2026-10-15 by
the maintainer: a report never overrides what a workflow did after the report
was read.** Seen on Test-Instances #478: the unshelve's server went ACTIVE at
16:58:15, the workflow pushed the access details at 16:58:26 and reacted 👍 at
16:58:38, but the latest report had been read from Jetstream2 before 16:58:15.
The card's "Unshelving…" ended with the 👍, the stale report said Shelved, the
card stopped refreshing (it only refreshes while something is in progress), and
it still showed Shelved without the access details seven minutes later. Before
this decision the labels were the state and the workflow sets them just before
the 👍, so the card flipped to Active with the details at once. That is the
behavior to keep. So: (1) the report is stamped with the time it was **read**,
not the time it arrived: the runner sends how many seconds passed between its
read of Jetstream2 and the send (a duration, so its clock is still not trusted)
and the portal subtracts that from its own clock; a report read slowly is that
much older. (2) The portal records when it learned that a command finished (the
👍) **in its database, next to the command**, so a portal restart in the window
between the 👍 and the next report does not lose it; while the instance's report
was read before that, the **labels decide** for that card, the command gates and
the assistant, which is the old behavior. The first report read afterwards
decides again, so the reconciliation still repairs what the labels get wrong. A
👎 changes nothing: a failed workflow may not have set its label. (3) The portal
never clears access details pushed after the report was read: this takes
precedence over the clearing described above, since those details are newer than
what the report says. (4) A card that is active but still waiting for its access
details keeps refreshing, since the runner's restore can deliver them minutes
after the workflow finished. |

| 21 | **The runner is the clock; GitHub's schedule is not.** (maintainer,
2026-11-03) GitHub's `on: schedule` fires a few times a day whatever the cron
line says: measured 2026-10-27 → 11-03 on both Instances and Test-Instances,
`7,37 * * * *` and `*/5 * * * *` workflows alike ran 3–5 times a day. Production
already fired its lifecycle workflows from a crontab on the acquire runner, but
that crontab was a hand-kept list: `reconcile-instances` was never added to it
and ran four times a day on both repositories, and the Test runner had no clock
at all, so on Test "every 5 minutes" auto-shelving ran five times a day. Found
when Test-Instances #503 went to ERROR under Jetstream2's 2026-11-03 maintenance
and nothing noticed for hours. So: every time-driven workflow carries a
`# dispatch: <cron>` marker; the runner crontabs under `scripts/` are rendered
from the markers by `scripts/check-workflow-clock.py`, and a pre-commit hook
fails when a schedule has no marker, a marker and schedule differ, a
`workflow_dispatch`-only workflow has no marker, or a committed crontab is
stale. `on: schedule` stays only as a backstop — and as the sole clock in MC-\*
course repositories, by the earlier decision that they get no dispatcher. The
Test runner gets the same dispatcher as production. Nothing this plan adds later
(reachability, unreachable→shelve, floating-IP reserve refill, ERROR detection)
may take its timing from `on: schedule`: it runs in the portal runner's
one-minute `pickup` or is dispatched from the crontab. |

| 22 | **Reachability is part of the instance's state.** (maintainer,
2026-11-03; step 1 of the reliability work) An ACTIVE server that nothing can
reach is not Active. Test-Instances #503 sat four days from 2026-10-30 as
"Active" with its access details on the card while no packet reached it (a
network orphan after unshelve, the class seen on Instances #178 and #185 in
August), because the runner's report reads Nova only. So the runner's one-minute
snapshot probes TCP port 22 on every ACTIVE server's floating IP (five-second
timeout, from the runner, which sits behind the same router as the member) and
reports `reachable`: true, false, or null when there is nothing to probe. The
portal keeps, next to the state row, when it first saw the server unreachable
(`unreachable_since`, carried across reports while the misses continue, cleared
by the first answer). The card reads **Unreachable** once the misses have lasted
`MC_UNREACHABLE_AFTER_SECONDS` (default 180: a server fresh from unshelve needs
a minute for sshd, and one missed probe is not a verdict), with a line saying
since when and what to do: Shelve, then Unshelve for a fresh placement.
Unreachable offers Shelve, Renew and Delete, never Extend; the access details
are hidden while it lasts, since they lead nowhere, and come back by themselves
when the server answers (nothing is cleared). The assistant's verdict for a
failed command on an unreachable server is "unreachable", never "fine". Nothing
in this step acts on the state or writes to GitHub: shelving an unreachable
instance on its own, and telling the admins once with the diagnosis, is step 2,
and it reads this same state. The probe is the cheapest honest check: it says
whether the member's SSH, TurboVNC and web desktop can connect at all, not
whether the desktop works. |

| 23 | **An instance unreachable for half an hour is shelved on its own, with
one word to the member and one to the admins.** (maintainer, 2026-11-04; step 2
of the reliability work; supersedes the last sentence of decision 22) The portal
decides, in the same report that carries the state: an instance ACTIVE with no
task, set up, and unreachable for `MC_UNREACHABLE_SHELVE_AFTER_SECONDS`
(default 1800) is to be shelved — once per episode, never while the member's own
command is running. The runner's report having reached the portal over the
public network is the proof that the runner's own network is not the blind one.
The runner, which holds the dispatcher token that is also the clock (decision
21), fires `control-instance-from-workflow.yml` with `command_name=shelve`: the
same workflow a member's Shelve runs, so the floating IP is released, the labels
are set and the issue gets the shelved comment, which GitHub mails to the
member. The runner answers the portal with whether the dispatch went out. The
member's card says "Being shelved automatically: it has not answered on the
network since …", then "Shelved automatically …; Unshelve places it afresh",
until the member unshelves, creates or deletes. The admins get one email per
episode, with the diagnosis (name, address, how long, the network-orphan
signature, what was done): "shelved automatically" (nothing to do) or "could NOT
be shelved" (no token, the dispatch failed, or no answer from the runner within
ten minutes: shelve it by hand). Never a reboot: Test-Instances #503 showed on
2026-11-03 that a reboot leaves an orphan dead and a fresh placement revives it.
Production gets this with the migration, together with the runner's dispatcher
token. |

| 24 | **Floating IPs: the project holds `max(N, running + 1)` and nothing more,
N = 5.** (maintainer, 2026-11-04; step 4 of the reliability work; replaces the
reserve of 8 unattached addresses minted on 2026-11-03) Jetstream2 runs out of
public addresses routinely and, when dry, hands out 172.17.x, which does not
route: Test-Instances #513 came up "Active" on one and was dead. Holding spares
on top of what runs is what Jetstream2's admins dislike, so the rule counts
attached addresses too: at quiet times N are held, at busy times exactly one
more than is running. (1) `create-ip` takes only a routable (149.165/16)
unattached address, deletes unattached non-routable ones on sight, and when it
has to mint, refuses a non-routable result (two tries) and fails fast with "no
routable floating IP available on Jetstream2". Having taken the last spare, it
mints one in the same run, so the next create or unshelve finds one in hand —
this is what makes N a quiet-time floor rather than a burst buffer: creates and
unshelves are serialized on the acquire lane, and each leaves a spare behind.
(2) `scripts/police-floating-ips.py` runs from the runner's crontab every five
minutes (decision 21): it releases an address bound to a shelved server
(Jetstream2's policy; a failed unshelve leaves one) — amended 2026-11-04 after
Instances #214: production binds the address before `server unshelve` (until
decision 25 reaches it), and the script released #214's address three minutes
into a hung unshelve, so the server came up ACTIVE with no public address; now
an address on a shelved server is released only when the server has no action in
flight (task_state empty) and the address has been bound for ten minutes or
more, and until then it counts as running — deletes non-routable addresses and
reports one bound to a running server, mints one when no spare is in hand, and
when more than `max(N, running + 1)` are held, deletes unattached routable
addresses idle for ten minutes or more, oldest first — ten minutes keeps clear
of an unshelve that just picked one. (3) Workshops are not pre-minted: their
instances draw on the same pool during creation and the backfill retries a start
refused for want of an address as it retries "no host". Nothing is exempt. (4)
What N buys is a drought: while Jetstream2 has nothing to give, `N − running`
more starts succeed, then starts fail with the clear message until it recovers;
the admins hear once per drought, through the new dispatchable `admin-alert.yml`
(the runner cannot send mail), and once per non-routable address found on a
running server. The two-week review reads the policing log (failed mints,
refused starts) to move N. The first run on production releases the reserve of 8
down to the rule. **Amended 2026-11-04 by the maintainer, after the first
production run:** only MorphoCloud instances (`…_instance-<n>`) count — "held"
is the addresses bound to them plus the unattached ones, "running" the addresses
bound to them. The runners, the portal, the MorphoDepot and morphocloud.org
servers and any other server of the project are infrastructure: never counted,
never touched (a non-routable address on one is still reported). And the pool is
filled up to the rule, one mint per run, not only when the last spare is taken:
with one instance running and four infrastructure servers, the project holds 5 =
1 running + 4 spare. **The infrastructure policy, confirmed by the maintainer
2026-11-04:** the name decides, nothing else — a server is a MorphoCloud
instance if and only if its name ends in `instance-<number>`, which is what the
workflows give. So (1) an infrastructure server must never be named so that it
ends in `instance-<number>` (`instances-runner-5` is fine, `runner-instance-5`
is not); (2) an unattached address is always the pool — below the rule the next
instance start takes it, above the rule it is released after ten idle minutes —
so an address for new infrastructure is allocated and attached in one step
(Exosphere's create does that), never parked. New infrastructure servers need
nothing else: they show up as `infra=N` in the policing log and the pool is
unaffected. No tag or marker scheme; the maintainer declined one. |

| 25 | **An unshelve takes and binds its address only once the server is ACTIVE;
no address, no running server.** (maintainer, 2026-11-04; step 5 of the
reliability work) `control-instance` bound the floating IP before
`server unshelve`, so every host-side unshelve failure (17 between September and
November 2026) left an address stuck on a shelved server — the policy violation
Jetstream2 forbids and the reconciler's most frequent email. Now the address
steps run after the status poll has seen ACTIVE (or after a no-op unshelve of an
already active server that lost its address): a host-failed or timed-out
unshelve leaves nothing bound, and the member's own retry starts clean. If the
server reaches ACTIVE but no address can be obtained (Jetstream2 dry,
decision 24) or attached, the run releases whatever was half-bound, shelves the
server again — best effort — and tells the member: "No public address could be
obtained — Jetstream2 may be out of addresses right now. The instance has been
shelved again; try again later." A server is never left running without an
address: it would bill and be unreachable, and the portal would only shelve it
half an hour later (decision 23). The stuck-address release before the unshelve
and the end-state check after it are unchanged. Create already had this order. |

| 26 | **The shelving job never skips an instance it cannot reach.**
(maintainer, 2026-11-04; step 3 of the reliability work)
`automatic-instance-shelving.yml` judged idleness by reading uptime over SSH,
and when SSH failed it `continue`d: an unreachable instance could never be
judged idle, so a network-orphaned GPU instance ran for days (Instances #178,
Test-Instances #503) with only a warning in a green run. Now an active instance
that does not answer (three SSH tries, 20 s apart) or has no public address is
shelved on the wall clock once it has been up for 30 minutes — "up" dated from
its latest create, unshelve, start or reboot in Nova's action log — through the
same dispatch the workshop shelve uses; younger, it is left to finish booting
and looked at again next tick. The member's issue gets one line ("has not
answered on the network for N minutes since it started, so it is being shelved;
Unshelve places it afresh") before the shelve. Where the portal runs, decision
23 reaches the same instance first (half an hour of consecutive misses, measured
every minute); this is the backstop, and production's only guard until the
migration. The uptime path for instances that answer is unchanged. |

| 27 | **One email per incident, with the diagnosis and the action; none for
what needs no admin; none ever lost.** (maintainer, 2026-11-04; step 6 of the
reliability work) What the admins got until now: a bare "command failed" email
from `control-instance` with links and no diagnosis; two emails per
reconciliation violation (the issue comment's `@MorphoCloud/morphocloud-admins`
mention through `on-admin-mention.yml`, plus the direct email); and no email at
all for a failed create — its comments are posted with `GITHUB_TOKEN`, which
raises no `issue_comment` event, so their admin mentions reached nobody. And a
mail server failure (the SMTP 528 of 2026-11-03 02:12Z) was swallowed by
`continue-on-error` on every mail step. Now: (1) **one channel per incident** —
the workflow that found it emails directly through the new `notify-admins`
action, with a body that says what happened and what to do (the failure class is
read from the step outcomes: host failure, no address, address not attached,
wrong end state, API error, timeout, setup not finished, credentials email
failed, unhandled) and attaches the unshelve report or the Jetstream2
diagnostics when there are any; bot comments carry no admin mention ("The
MorphoCloud admins have been emailed"), so `on-admin-mention.yml` is for humans
asking for help. (2) **No email for what needs no admin**: unapproved, does not
exist, a plain capacity shortage (the member is told and retries, the backfill
retries workshops), no storage, an instance already there. (3) **A failed send
is never lost**: the action labels the issue `admin-email-failed` and posts the
full notification as a comment; `reconcile-instances` sends one catch-up email
listing those issues when mail works again and clears the labels; its own
violation email, when it fails, un-flags the violations so they alert again next
run instead of being silenced by the `invariant-violation` label. (4) Creates
email the admins for the first time. The portal's own alerts (decisions 23
and 20) keep their one-per-episode rule; an outbox with retry for them and a
daily digest for the purely informational ones ("shelved automatically", "report
is back") is the portal half of this step, to follow. |

| 28 | **A command the workflow had to abandon is finished by the clock, not by
a human.** (maintainer, 2026-11-04; after Instances#214: an unshelve whose boot
hung on the compute host for 26 minutes was reported as "may be stuck" and
dropped; the member's delete ten minutes later was refused because the task was
still in flight and dropped too; the server then came up on its own and nothing
finished the job — the maintainer traced it by hand) `control-instance` now
leaves a `followup:<command>` label on the issue whenever it gives up on a
command that may still complete: the wait for an in-flight action ran out, the
status poll timed out, or the compute host failed the unshelve. The new
`follow-through.yml` sweep (every five minutes from the runner clock) finishes
it: once the server has settled — no task_state, unchanged for two minutes — it
dispatches the command again through `control-instance-from-workflow.yml`, at
most twice per episode (`followup-count:<n>`), announcing each attempt on the
issue ("Jetstream2 has settled; retrying `unshelve` automatically (attempt 1 of
2)"). An unshelve whose server is already ACTIVE takes the action's no-op path,
which attaches a missing address, sets the properties and sends the access email
— what #214 was owed. A shelve whose server is already shelved, or a delete
whose server is gone, is closed with a note. Retries spent, or the server in
ERROR: the issue gets `needs-admin`, the member is told the admins have it, and
the admins get one email listing the issues (a failed send leaves
`admin-email-failed`, decision 27). A run that starts drops any stale
`followup:` marker (the member's own command supersedes the sweep); a run that
succeeds clears the count and `needs-admin`. The failure comments say so: "There
is nothing you need to do: MorphoCloud retries the command by itself once
Jetstream2 has settled". A retried delete does not unapprove the request (the
original flag is not kept). Not covered: capacity shortages (the member retries,
decision 27), corrupt shelved state, address failures (decision 25 shelves again
and tells the member). | | 29 | **The public address of a server is read from
Neutron, never from Nova's address view.** (maintainer, 2026-11-04;
Instances#214) Nova's `addresses` field is a cache of the network state; it is
rebuilt when a server is spawned, so the normal unshelve path never showed the
problem, but after an address is attached to a server that is already running it
can stay stale for a long time — on #214 it still listed only the fixed address
40 minutes after Neutron had the floating IP bound to the port, while Exosphere
(which reads Neutron) showed the address and SSH answered on it. Every `/email`
meanwhile spent five minutes waiting for Nova and failed ("Failed to retrieve
metadata"), and the automatic-shelving and uptime sweeps saw "no address" too.
Now `retrieve-metadata`, `control-instance` (has-address check),
`automatic-instance-shelving` and `collect-instance-uptime` take the address
bound to the server's port (`openstack port list --server`,
`openstack floating ip list --port`), as `reconcile-instances`, `create-ip` and
the policing script already did. The fixed address still comes from Nova (it
never changes). |

| 30 | **An unshelve that fails at scheduling is reported as what it is: no host
free for the flavor.** (maintainer, 2026-11-04; Instances#214, the same
afternoon: after the member shelved, two unshelves failed within seconds with
`schedule_instances` Error while Jetstream2 showed 0 g3.xl free, and the
workflow told them "failing on Jetstream2's side, 2 attempts" — the host-error
wording, with delete + create as the way out; the maintainer asked why it did
not say "no capacity", as it once could) The old capacity check keys on the
server going to ERROR with no host, which a build does and an unshelve from
SHELVED_OFFLOADED never does: Nova puts the server back as shelved and records
no fault. The signal is in the action record instead:
`openstack server event show` lists the failed step — `schedule_instances` Error
is capacity, `compute_unshelve_instance` Error is the chosen host. The poll
reads it the moment it sees a new failed unshelve: scheduling failure →
`no_capacity`, no automatic second attempt (the cloud is full now), the member's
comment says "Jetstream2 has no **g3.xl** host free right now (Jetstream2
reports 0 available). Your instance is safely shelved and nothing was changed.
Try `/unshelve` again later", with the Grafana vacancy link; no admin email
(decision 27), no follow-through marker (decision 28), and never the create
path's capacity branch, which deletes the server. A compute failure keeps the
host-error path (one automatic retry, then the report). Not yet: queuing the
unshelve until capacity returns — a candidate for the follow-through sweep, not
decided. |

| 31 | **A maintenance switch closes MorphoCloud to members with one sentence,
and is the rollback lever.** (maintainer, 2026-11-04, for the migration of
2026-11-23 to 26) A repository variable `MORPHOCLOUD_MAINTENANCE` holding the
reopening date. While it is set, every member entry point — the command
validators (`validate-command-*`) and the request validators
(`validate-request*`) — answers once on the issue: "MorphoCloud is closed for
the storage migration until <date>. Your instance and your files are kept;
nothing is lost. This command was not run." and stops; the request handler does
the same for a new request. Admin-dispatched runs
(`control-instance-from-workflow`, `create-instance-from-workflow`) and the
scheduled sweeps (shelving, reconcile, follow-through, policing) keep working,
so the admins can shelve, delete and rebuild during the closure and nothing
rots. The landing banner is the join app's `ONBOARDING_PAUSED` flag, already up
since 2026-10-28. Rollback at any point is: vendorize from the
`pre-migration-2026-11` tag, restore the old names, unset the variable. |

## Sign-in (decision 8)

The current OAuth App only reads team membership (`read:org`). Posting a command
as the user would need the `public_repo` scope, which is write access to every
public repo the user can write to. A GitHub App limits the same sign-in to what
the portal needs.

GitHub App settings:

- Callback URL: `https://<portal host>/auth/callback`
- Expire user authorization tokens: **on** (8 hours)
- Webhook: **off**
- Repository permissions: **Issues: read and write**; **Contents: read-only**
  (the portal reads the instance types from the request form)
- Organization permissions: **Members: read** (team check)
- Where can this GitHub App be installed: **Any account** (decision 15)
- Private keys: **none**
- Installed on **Test-Instances only**

The user's token is kept in memory only (decision 13). The OAuth App is retired
once the GitHub App works.

## What the page shows

- The link on each card reads "Go to Issue N for full history".
- **Storage comes first.** The storage card is the first card and the instance
  card the second. Until the user has storage, the instance card only says
  "Create your storage first to enable your instance" (no buttons, no instance
  types). Storage the portal cannot reach at the moment still counts: instances
  mount the share themselves.
- **No reset under a running instance.** "Reset my storage" works only when
  every instance is shelved or deleted and no action is in progress; otherwise
  the button is unavailable with "Shelve or delete your instance first", and the
  reset page refuses too (also when the instance state cannot be checked). While
  a reset is queued or running, Unshelve and Create are refused, so an instance
  cannot mount the storage mid-reset. Decided by the maintainer 2026-09-28.
- The header has two links right of the logo, one above the other:
  **Documentation** (https://github.com/MorphoCloud/docs) and **User guide**
  (its `user-guide` folder).
- The top line ends with the funding acknowledgement, "Funded by NSF
  (DBI/2301405)", linked to the funding section of morphocloud.org. It is shown
  even when the availability numbers are not.
- "Open my files" opens the file browser in a panel to the right of the cards
  (windows 700 px and wider), so the instance status stays in view; the panel
  also links to a full tab. Narrower windows (phones) open the file browser in a
  new tab instead; the panel is never stacked under the cards. The panel runs
  from the top of the storage card to the bottom of the instance card (at least
  460 px tall). A closed panel takes no space. The Help card (CHATBOT_PLAN.md)
  spans the full page width below everything, with or without the panel; the
  storage and instance cards stay at most 640 px wide. While an action is in
  progress the page refreshes only the cards, never the panel, so uploads are
  not cut off. The buttons (Create, Shelve, Unshelve, Renew, Delete) also act
  without leaving the page, so an upload keeps running while an instance is
  created. The panel's header is the current path as plain text links, for
  example "MyDrive / Documents /": each folder in it opens that folder and
  "MyDrive" the top one, whatever the panel shows (also after a file was opened
  in it). copyparty's own path heading is hidden in the panel and stays in the
  full tab.
- The section is titled "Your instance" (one per user, decision 14). The expiry
  date sits below the buttons; storage is a card like the instance.
- While an instance is being created, the card shows the steps from the issue's
  "Instance Creation Progress" comment (✅ done, ⏳ in progress). The volume
  column is not shown, and "Attach volume" reads "Storage".
- One card per open individual request the user opened. The per-user instance
  limit is still enforced by the workflows
  (`MORPHOCLOUD_MAX_INSTANCES_PER_USER`).
- State from `status:*` labels: `status:active` → active; `status:shelved` or
  `status:shelved_offloaded` → shelved; `status:deleted` or no label → no
  instance. Any other label is shown as-is with the issue link.
- Buttons match the state: active → Shelve, Renew and Delete; shelved →
  Unshelve, Renew and Delete (Renew only while an extension is left); no
  instance → none (the Create form below applies); no storage → no buttons and
  no Create form, only "Create your storage first to enable your instance".
  After a press, the card names the action ("Shelving…", "Creating…") and the
  buttons are hidden until the workflow reacts on the command comment (👍
  finished, 👎 failed; see `report-command-outcome`), at most 3 hours. A failed
  command is noted on the card for a day.
- Delete asks for confirmation in a dialog (a confirmation page without
  JavaScript): "This removes your instance. Anything saved only on the instance
  is lost. Your files in your storage are not affected." with **Delete
  instance** and **Cancel**.
- After a successful delete, the workflow closes the request (share mode), the
  same for Delete on the portal and `/delete_instance` typed on GitHub. If the
  delete failed, the request stays open.
- **Create instance** form: radio buttons for the instance types, read from the
  request form. Shown when the user has no instance (decision 14). If the user
  still has an open request with no instance (for example opened on GitHub, or a
  failed create), the portal closes that request and opens a new one. It never
  closes a request that has an instance.
- Instance types listed in the portal setting `MC_UNAVAILABLE_FLAVORS` (names or
  patterns, for example `g4.xl,r3.*`) stay in the list, greyed out with "(not
  available for the prototype)", and the portal refuses them. On the Test
  portal, during the evaluations: g4.xl and every r3 type.
- Closing a request runs `guard-issue-close.yml`. With no instance and no volume
  it does nothing. On Test-Instances, where instances still get a `My-Data-<n>`
  volume, the volume stays after the close (only the volume expiry schedule
  deletes volumes) until shares replace volumes.

## Access details (decisions 5, 10)

The workflow step that sends the credentials email also sends the Web connect,
SSH and TurboVNC details and the passphrase to the portal. It uses the same
restricted SSH channel as share creation, with two new commands in the gate
(`connection` and `connection-clear`). Only a runner with
`~/mc-data/portal_target` sends them (the Test-Instances runner); on every other
runner the steps do nothing.

- Replaced on every create and unshelve (the address changes).
- Cleared on shelve and delete.
- Also written and cleared by the runner's reconciliation (decision 20) when the
  workflow's own push did not happen or no longer matches: an active instance
  with no details on the portal gets them within a minute; a gone or shelved one
  loses them. The workflow's push stays as the fast path.
- Shown only to the issue's author.
- Under Open in browser, one line: "Works best in Chrome or Edge; copy and paste
  won't work in Firefox or Safari." (The web desktop has no side panel in share
  mode, STORAGE_REDESIGN_PLAN.md.)
- Order: Username and Passphrase first, then the ways to connect: Web connect,
  SSH, TurboVNC. The TurboVNC label links to the latest TurboVNC release on
  GitHub (https://github.com/TurboVNC/turbovnc/releases/latest, never a fixed
  version), and a line under the list reads "Click TurboVNC to download the
  viewer."

## Portal-opened issues (decision 12)

The request form adds three labels, and the workflows depend on all of them:

- `request-type:instance` and `request-creator:user`: the request handler
  (`on-instance-request-opened.yml`) and `/create`.
- `instance-request`: `/shelve`, `/unshelve`, the credentials email, and the
  close guard.

GitHub drops labels on issues opened through the API by users without write
access, so an issue the portal opens with the user's token arrives unlabeled.
The user stays the author, so the team check, the per-user limit and the command
allowlists work unchanged once the labels are on.

Workflow changes:

1. The portal writes a marker line, `<!-- morphocloud-portal -->`, at the
   **end** of the issue body. At the start it crashes the issue-form parser
   (`zentered/issue-forms-body-parser` fails on HTML before the first heading;
   found 2026-09-26 on Test-Instances#440); after a heading it is ignored.
2. A new labeler workflow runs on `issues: opened`. When the body contains the
   marker and the issue has no `request-type:*` label, it adds the three form
   labels, then, in a second call, `request-source:portal`. The handler reads
   the other labels from that event's payload, so they must already be on the
   issue. It uses the workflow GitHub App token
   (`vars.MORPHOCLOUD_WORKFLOW_APP_ID` +
   `secrets.MORPHOCLOUD_WORKFLOW_APP_PRIVATE_KEY`, as the request handler does).
   It **must not** use `GITHUB_TOKEN`: label changes made with it do not trigger
   other workflows, so the handler would never run and nothing would report an
   error.
3. The request handler gains an `issues: labeled` trigger that runs **only when
   the label just added is `request-source:portal`**. Form-opened issues never
   get that label, so they run once, on `opened`. Portal-opened issues run once,
   on that label. The existing per-issue concurrency group still applies; every
   other `labeled` event gets a group of its own, so it cannot take the pending
   slot of a real run.
4. `request-source:portal` is added to `labels.yml`.

Someone could put the marker in an issue opened by hand through the API. That
issue would get the same labels and the same checks as a form request, so it
gains nothing.

The portal holds no GitHub App private key; only the workflows label.

**Order of the first Create.** The portal opens the issue, then waits for the
handler's result before posting `/create`:

The portal reads the `### Validation Results` comment posted by
`validate-request.yml`:

- It contains `Instance request validated` → post `/create`.
- It contains `The validation checks failed` → show that and the issue link.
  `/create` is not posted.
- Issue closed (not a member, or over the per-user limit; no validation comment
  is posted in these cases) → show "Your request was closed" and the issue link,
  where the reason is. `/create` is not posted. The portal checks the issue's
  open/closed state on every poll, so this shows at once, and it does not parse
  the close-reason comment.
- None of these within 10 minutes → show "Something went wrong" with the issue
  link, and email the admins. `/create` is not posted.

These strings are a contract: changing them in `validate-request.yml` requires
changing the portal.

The per-user limit counts open individual **and** course-instance requests by
the same author, as it does today.

Both paths (decisions 8 and 9) open the first issue with the user's token. The
backup path changes only how commands run later.

Rejected: the bot opens the issue and assigns the user. Every check that uses
the issue author would need rewriting.

## User token (decision 13)

Posting a command after sign-in needs the user's GitHub token. It is kept **in
the portal process's memory only**, keyed by a random session ID in the cookie.
It is never written to disk and never put in the cookie (the cookie is signed,
not encrypted).

- Dropped on sign-out, after 8 hours, or when GitHub rejects it.
- The refresh token is not kept; the user signs in again.
- A portal restart signs everyone out. Accepted.

Rejected: encrypted in the portal database (token and key both on disk).

## Security

- A user sees and acts only on issues they opened (matched by GitHub numeric ID,
  not login).
- The workflows' own authorization stays the real gate on the primary path.
- The data portal's rules carry over unchanged: nothing here can reset,
  overwrite or delete a user's share.
- The reconciliation (decision 20) only reads Jetstream2 and only writes to the
  portal through the gate's fixed commands, each about one issue; it starts,
  stops and changes nothing (decision 7 holds). The state row holds no secret;
  the passphrase travels only in the existing `connection` command.

## Migration (2026-11-23 to 26; maintainer, 2026-11-04)

MorphoCloud is closed to members for four days and reopens on 2026-11-27 with
the share-based stack on production. Six steps, in this order; the build work is
done before the 23rd so the four days hold only execution and testing.

1. **Close.** `MORPHOCLOUD_MAINTENANCE` set (decision 31); the running
   production instances shelved. Tags `pre-migration-2026-11` on
   MorphoCloudWorkflow `main`, Instances `main`, the exosphere fork's base
   branch and the portal repository; a copy of the Test portal database.
   `share-prototype` merged into `main` as a merge commit (history kept; #311
   closes as superseded); `main` vendorized into Instances with the diff shown
   first; the production runner's crontab re-rendered and reinstalled.
2. **Shares.** One share per existing member (the 32 approved requests;
   two-instance owners get one share and a separate email, decided 2026-10-24).
   Each `My-Data-N` volume is attached to a migration VM that mounts the
   member's share, measured, copied (rsync, checksums), verified — file counts
   and sizes posted on the member's issue — and detached. Then the old volumes
   and servers are renamed (`legacy-…`) so no workflow can find them; they are
   the rollback copy and are deleted 30 days after reopening, not before. The
   same script, rehearsed first on the dozen Test-Instances members, creates
   their shares. A member whose volume holds more than the share size is handled
   by hand before the copy.
3. **Instances.** The admins create each member's instance (decided 2026-10-24:
   members do not create their own), of their previous type when Jetstream2 has
   it and of an available type otherwise (2026-11-04), shelved right after setup
   so nothing bills idle; the member's first click is an unshelve. The portal's
   functions are then walked through on production with the `amm554` account
   (the Test-Instances test plan, re-run).
4. **MorphoSource cart.** A design document first. Members choose datasets on
   MorphoSource and add them to the cart; on the portal they enter their
   MorphoSource API key and the portal downloads and unzips the cart into their
   storage, with or without an instance running. Built against the endpoints
   MorphoSource exposes; if they are late or the integration gives trouble, it
   is deferred and ships after reopening (maintainer, 2026-11-04) — the 25th
   then becomes a buffer day.
5. **Cards and chatbot.** The card layouts redesigned and the chatbot aligned
   with them, built and reviewed on Test before the 23rd, deployed on the 25th.
6. **Landing page and documentation.** The landing page reviewed, the
   documentation links updated, the credentials email copy checked. A full
   walk-through as a new member and as an existing one. The rollback decision
   point is the evening of the 26th.

Day by day: the 23rd is step 1 in the morning and step 2 started in the
afternoon (copies in parallel, three at a time); the 24th finishes step 2 and
does step 3; the 25th is steps 4 and 5; the 26th is step 6. The 27th: the switch
off, reopen.

Before the 23rd, on Test: the maintenance switch; the migration script and its
report; the production portal VM provisioned and dark (GitHub App, team gate,
dispatcher token, sudoers, pickup line); the two golden images on the production
allocation; the cart design document and, if the endpoints exist, the
integration; the card and chatbot work; the landing page draft; items 2, 3 and 5
of the reliability work (decisions 28 to 30 are 1 and 4); a rehearsal of the
`share-prototype` into `main` merge in a scratch branch (today it conflicts in
two files, the hotfixes of 2026-11-04 that exist on both branches).

## Not decided

Nothing open. Renew (decision 4) and the expiry dates (decision 17) were decided
on 2026-09-26. Anything new needs the maintainer's decision before it is added.

## Later: one site (agreed direction, 2026-09-26)

MorphoCloud should have one front door, morphocloud.org, whose page adapts to
the visitor's state, instead of three places (morphocloud.org, join, the data
portal) with two different sign-ins. Built when shares move to production (after
the prototype is adopted and the quota increase lands), not before.

**States of the one page:**

| State                       | Page shows                                                         |
| --------------------------- | ------------------------------------------------------------------ |
| Visitor                     | What MorphoCloud is, availability, "Sign in with GitHub"           |
| Signed in, not a member     | The application: verify ORCID and email, fill the form             |
| Applied, invitation pending | "Accept your invitation" with a link to it                         |
| Member                      | The dashboard: instance and storage cards (later the support card) |

- **Sign in with GitHub first.** The application then knows the applicant's real
  GitHub account instead of a typed username; ORCID becomes a verification step
  inside the application.
- **One front door, two services behind it.** The public app (anonymous traffic,
  the application form, the Google credentials and the GitHub App that manages
  org membership) and the storage and instance service (every share's key and
  mount) stay on separate VMs. A reverse proxy routes the dashboard's actions
  and `/files` to the storage service and everything else to the front door, as
  the portal already does for `/files`. A bug in the public form must not be
  able to reach anyone's files.
- **One sign-in both services trust:** a shared signed session, or the front
  door vouching for the user to the storage service.
- **Unchanged addresses:** the email lookup API the workflows call keeps a
  stable path.
- **Applicant state** comes from data that already exists: the application sheet
  and the org membership.

## Later: members of more than one team (agreed direction, 2026-09-26)

A member can belong to MorphoCloudUsers and to a course team at the same time.
Each of these is a **context**: a team and the repository its requests go to
(MorphoCloudUsers → Instances; a course team → that course's repository). Built
after shares reach production for individual members. It reopens decision 11 for
courses; workshops stay out of scope.

- **Selector:** a menu next to the user name in the header, shown only to
  members of two or more contexts. The choice is kept in the session.
- **Everything follows the context:** the storage card, the instance card, the
  instance types, the limits and the request repository.
- **Storage per context:** one personal storage and one per course, never shared
  between them.
- **One instance per context** (decision 14 applies within each context), so no
  share is mounted by two instances.
- **Course members manage their own instance** exactly like individual members:
  Create, Shelve, Unshelve, Extend session, Renew and Delete. Only workshop
  organizers provision instances in bulk.
- **Courses are short** (a few months at most).
- **Course storage lives on the course's own Jetstream2 allocation.** That needs
  share quota there and the portal's and runner's access to that project.
- **Course storage after the course ends** is not planned now: it is on the
  course's own allocation, and what happens to it is up to the instructor.
- **Images:** a course chooses its instance types when it is created, and its
  instances use the base image for that type (vGPU or regular). To do: let
  instructors customize their course's image.
- **The availability line stays** for every context: it shows Jetstream2-wide
  vacancy, not an allocation's.
- The portal needs the list of contexts instead of one request repository, the
  GitHub App installed on each course repository, and permission to read the
  member's teams.
- **Possibly one management VM per course** instead: courses have their own
  allocation and resources, so each could run its own copy of the portal from a
  template, which also isolates courses from each other. Decided after the
  prototype for individual members is complete.

## Later: support chatbot (idea, not planned)

Requested 2026-09-26, for later. A third card on the page, below the instance
and storage cards: a support assistant that helps users diagnose problems.

- **Model:** Jetstream2's own inference service, since the portal runs inside
  Jetstream2. To check first: which models are offered, whether one accepts
  images, rate limits, and the terms for sending user content to it.
- **Knowledge:** MorphoCloud's documentation (the user guide, the issue command
  lists, the known problems), given to the model as a skill or retrieved per
  question. A small fine-tune on the documentation is the fallback if retrieval
  is not good enough.
- **Input:** a description, pasted output (for example a failed command's
  comment or a terminal error), or a screenshot.
- **Output:** the likely cause and the steps to fix it. When it cannot find an
  answer, it offers to ask the MorphoCloud admins for help, sending the
  conversation to them with the user's consent.
- **Care needed:** screenshots and pasted output can contain the passphrase or
  other personal information; warn before sending, and keep as little as
  possible (collect no more personal data than needed). The assistant answers
  questions only; it never runs commands on the user's instance.

## Test plan (Test-Instances)

1. Sign in with the GitHub App; non-members are refused.
2. First Create: issue opened, `/create` runs, card turns active, access details
   and passphrase shown. **Passed 2026-09-26** (Test-Instances#441).
3. Shelve and Unshelve from the dashboard; the new address appears after
   unshelve. **Passed 2026-09-26** (Test-Instances#438).
4. The issue timeline shows the commands as posted by the user.
5. A second user cannot see or act on the first user's instance.
6. Backup path (decision 9) run once as a comparison.
7. Delete: the confirmation, the clean shutdown, the request closes itself, and
   the Create form comes back.
8. Renew once: the expiry date moves to the next rung and the button disappears.
9. Storage first: without storage the instance card only asks for it, and
   `/create` on GitHub refuses. **The refusal passed 2026-09-26** (as amm554,
   Test-Instances#443).
10. A failed create (for example a type the allocation lacks) leaves the Create
    form available; creating replaces the empty request.
11. Unreachable (decision 22): on an orphaned instance (Test-Instances #503 as
    left on 2026-11-03, or an instance whose security group is closed for the
    test), the card turns Unreachable within four minutes with the access
    details hidden and the line saying since when; Shelve works from the card;
    after Unshelve the card is Active again with the details. A fresh unshelve
    never shows Unreachable during its first minutes.
12. Auto-shelve (decision 23): with `MC_UNREACHABLE_SHELVE_AFTER_SECONDS=300` on
    the Test portal and an instance whose security group is closed, the card
    goes Unreachable at about 3 minutes and "Being shelved automatically" at
    about 5; the shelve workflow runs; the card reads Shelved with the note; one
    admin email arrives with the diagnosis; Unshelve clears the note. **Passed
    2026-11-04** (Test-Instances#503: port closed 00:43:49Z, shelve dispatched
    00:49:43Z, shelved 00:52:20Z, email sent).
13. Floating IPs (decision 24): `police-floating-ips.py --dry-run` on the Test
    runner lists the two 172.17 addresses for deletion and the address stuck on
    the shelved #503 for release; the real run does it and the next run is
    quiet; an unshelve on Test takes the spare and leaves one behind (the "Keep
    one in hand" step in the run log). **Passed 2026-11-04** (dry run, then the
    02:00Z run on Test deleted 172.17.20.187 and released + deleted
    172.17.21.23; production's 02:05Z run released the reserve of 8 and, after
    the amendment, filled to 5 = 1 running + 4 spare by 02:45Z).
14. Unshelve order (decision 25): in the run log of an unshelve on Test, "Poll
    instance status" finishes before "Retrieve or create a floating IP" and
    "Associate floating IP"; the card shows the new address; the floating-IP
    list shows nothing bound to a shelved instance before or after. A failed
    address step (forced by a wrong preferred address or a dry pool) ends with
    the server shelved again and the member's comment saying so. **Passed
    2026-11-04** (Test-Instances#503, run 41767626047: Poll 03:32:36 → Retrieve
    03:33:17 → Keep one in hand → Associate 03:33:20 → Verify 03:33:35;
    shelve-to-unshelved 2 min 37 s; nothing bound to a shelved instance).
15. Unreachable backstop (decision 26): with #503's security group closed, the
    next auto-shelve run on Test says "does not answer on …; up for N min (<
    30): waiting" and, had it been up 30 minutes, "Shelving on the wall clock"
    with the member's line on the issue; the shelve itself is the workshop
    dispatch already proven. On Test the portal (decision 23, 5 min) shelves
    first, so the waiting line is what the test observes. **Passed 2026-11-04**
    (second run, after #345 fixed the job shell's `-e` ending the run: #503,
    "does not answer on 149.165.174.219 after 34 min up. Shelving on the wall
    clock.", the member's line at 04:06:39Z, shelve dispatched 04:06:41Z, run
    green).
16. Admin email (decision 27): a forced unshelve failure on Test (a wrong
    preferred address, or an unshelve while the pool is dry) produces exactly
    one email, whose body names the failure class and what to do, and no
    `on-admin-mention` email; a failed create (a type the allocation lacks is
    capacity — use a bad image variable instead) produces one email for the
    first time; with the mail password broken on Test, the same failure leaves
    the `admin-email-failed` label and the comment, and the next
    reconcile-instances run after the password is restored sends the catch-up
    and clears the label. **Passed 2026-11-04** (Test-Instances#503 locked so
    its unshelve failed with an API error while `MAIL_PASSWORD` was wrong for 92
    s: the diagnosis email was refused, the label and the full notification
    comment landed at 06:19:35Z; password restored, a dispatched reconcile run
    sent the catch-up and cleared the label, 06:21Z). The forced create and the
    dry-pool unshelve were not exercised: the create path shares the action, and
    a dry pool cannot be forced.
17. Follow-through (decision 28): on Test, a `followup:unshelve` label put by
    hand on a shelved #503 makes the next `follow-through` run post "retrying
    `unshelve` automatically (attempt 1 of 2)", set `followup-count:1` and
    dispatch an unshelve that comes up with its address and email, after which
    the count label is gone; `followup:shelve` on the running #503 shelves it
    the same way; `followup:shelve` on a shelved #503 is closed with "completed
    on Jetstream2 after all"; `followup:unshelve` + `followup-count:2` on a
    shelved #503 yields `needs-admin`, the member's "could not be completed
    automatically" comment and one admin email, with no dispatch. **Exhaustion
    case passed 2026-11-04 18:50Z on #514** (cron tick 16:50:07Z: comment,
    `needs-admin`, one admin email; a second run ten seconds later found nothing
    to do). The shelve/unshelve cases were not run: #503 and #514 were deleted
    by the maintainer the same day.
18. Maintenance switch (decision 31): with `MORPHOCLOUD_MAINTENANCE` set on
    Test-Instances, a typed `/email` on an instance issue gets only the notice
    and every command workflow run is skipped; with the variable removed, the
    same command runs. **Passed 2026-11-04 18:32Z** on #514 (notice "closed for
    the storage migration until 2026-11-27 … This command was not run"; Validate
    Instance Command: `maintenance-notice` success, `validate` skipped; Send
    Email, Control Instance, Create Instance, Delete Instance, /extend, /renew
    all skipped; variable removed → Send Email ran, and failed only because the
    instance had been deleted minutes earlier).
