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

| #   | Decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | The dashboard is part of the data portal: same VM, same sign-in, same page as the storage card.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 2   | Each instance card shows the **state** (no instance, active, shelved), the **access details** (Web connect, SSH, TurboVNC) and a **link to the request issue** for diagnostics.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 3   | No Data drop entry. The data portal replaces it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 4   | Buttons: **Create**, **Shelve**, **Unshelve**, **Renew**, **Delete**. Renew runs `/renew` and is shown only while an extension is left (as `/renew` itself decides). Delete runs `/delete_instance` only, always asks for confirmation first, and the request is **closed by the workflow** once the delete has finished (share mode; see STORAGE_REDESIGN_PLAN.md). It never touches a data volume; volumes go away with the storage redesign (per-user shares). Revised 2026-09-25.                                                                                                                                                                             |
| 5   | The **passphrase is shown** on the dashboard, behind GitHub sign-in.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 6   | **Every Create opens a new request** and runs `/create` in one step, with the flavor chosen on the portal each time. A request is one instance's lifetime: the portal never runs `/create` on an existing request. Revised 2026-09-25 (this is how users change flavor).                                                                                                                                                                                                                                                                                                                                                                                          |
| 7   | GitHub issues and workflows stay the engine and the audit trail. The portal does not start, stop or change instances itself.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| 8   | **Primary path:** sign-in moves to a MorphoCloud **GitHub App**, and buttons post the command (`/shelve`, …) on the user's issue **as the user**. The existing workflow checks apply.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 9   | **Backup / second test:** the portal checks that the user owns the issue, then the bot runs the existing `*-from-workflow` dispatch workflows. Built only if needed, or as a comparison.                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 10  | The portal gets **no OpenStack credentials**. State comes from the issue's `status:*` labels. Access details are pushed to the portal by the workflow over the restricted SSH channel.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 11  | Individual instances only. Courses and workshops are out of scope.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 12  | A portal-opened issue is opened with the **user's token** (the user stays the author). A workflow adds the form's three labels plus `request-source:portal`, and the request handler runs once, on that label.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 13  | The user's GitHub token is kept **in the portal's memory only**: never on disk, never in the cookie. A portal restart signs everyone out.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 14  | **One instance per user.** With per-user shares, two instances would mount the same share at once. The portal offers Create instance only when the user has no open request, and never a second one.                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 15  | The GitHub App is **public** ("Any account"), so people who have not joined can authorize it and see the portal's "Not a MorphoCloud member" page with the join link. The portal's team check is the gate. The app has **no private key**.                                                                                                                                                                                                                                                                                                                                                                                                                        |
| 16  | **Availability banner:** one line at the very top with every instance type and its current Jetstream2 vacancy, colored as on morphocloud.org (4+ green, 1–3 yellow, 0 red, unknown grey). The numbers come from a read-only endpoint on morphocloud.org, the same source as its landing page. Hidden when that endpoint cannot be reached.                                                                                                                                                                                                                                                                                                                        |
| 17  | **Expiry dates**, shown separately: the instance (90 days after the request, 180 after `/renew`, followed by "(can be renewed)" or "(no more renewals)", computed from the issue's `expiration:*` and `renewed:*` labels exactly as the expiry sweep does) and the storage (180 days after the share was created, shown as "kept until at least"; nothing enforces it yet, see STORAGE_REDESIGN_PLAN.md decision 7).                                                                                                                                                                                                                                              |
| 18  | **Only the portal (and admins) drive instances.** GitHub cannot stop members from opening issues or commenting, so the workflows act only on what came through the portal: a command runs only if its comment was made through the MorphoCloud Portal app (GitHub's own `performed_via_github_app` field, which users cannot fake) or by an admin, and an issue opened directly on GitHub is closed with a pointer to the portal. Issues stay readable for diagnosis. Agreed 2026-09-26; not built yet (with the next production step). Taking GitHub out of users' view entirely (bot-only posting, owner recorded in the issue) belongs with the one-site work. |
| 19  | **Session countdown and Extend session.** An active instance's card shows the time left before automatic shelving (session start + the request's `timeout:<N>hrs`, else 4 hours) and an **Extend session** button, which posts a new `/extend` command: the workflow resets the instance's session timer exactly as the desktop ExtendInstanceSession icon does and reports the new start. Create and unshelve report a fresh start; the 5-minute auto-shelving sweep reports every instance's session age, so an extend made on the desktop shows within 5 minutes. `/extend` typed on GitHub works the same. Agreed 2026-09-26.                                 |

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
- The header has two links right of the logo, one above the other:
  **Documentation** (https://github.com/MorphoCloud/docs) and **User guide**
  (its `user-guide` folder).
- The top line ends with the funding acknowledgement, "Funded by NSF
  (DBI/2301405)", linked to the funding section of morphocloud.org. It is shown
  even when the availability numbers are not.
- "Open my files" opens the file browser in a panel on the right half of the
  window (below the cards on narrow screens), so the instance status stays in
  view; the panel also links to a full tab. On wide screens the panel runs from
  the top of the storage card to the bottom of the instance card (at least 460
  px tall), and the Help card (CHATBOT_PLAN.md) spans the full width below both.
  While an action is in progress the page refreshes only the cards, never the
  panel, so uploads are not cut off. The buttons (Create, Shelve, Unshelve,
  Renew, Delete) also act without leaving the page, so an upload keeps running
  while an instance is created.
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
- Shown only to the issue's author.

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
