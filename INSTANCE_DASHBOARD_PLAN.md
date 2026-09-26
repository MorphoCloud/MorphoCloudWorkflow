# Instance Dashboard — Manage an Instance Without the Issues Page

New users get lost in the GitHub issues interface. This adds a simple instance
dashboard to the data portal (see
[STORAGE_REDESIGN_PLAN.md](STORAGE_REDESIGN_PLAN.md), section 3), so users sign
in once and see their storage and their instances on one page.

Built and tested on **Test-Instances only**. Production needs explicit
instruction.

## Decisions

Agreed 2026-09-25. Changing any of these needs the maintainer's approval, and
this table is updated first.

| #   | Decision                                                                                                                                                                                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | The dashboard is part of the data portal: same VM, same sign-in, same page as the storage card.                                                                                                                                                                                                               |
| 2   | Each instance card shows the **state** (no instance, active, shelved), the **access details** (Web connect, SSH, TurboVNC) and a **link to the request issue** for diagnostics.                                                                                                                               |
| 3   | No Data drop entry. The data portal replaces it.                                                                                                                                                                                                                                                              |
| 4   | Buttons: **Create**, **Shelve**, **Unshelve**, **Delete**. Delete runs `/delete_instance` only, always asks for confirmation first, and **closes the request** once the delete has finished. It never touches a data volume; volumes go away with the storage redesign (per-user shares). Revised 2026-09-25. |
| 5   | The **passphrase is shown** on the dashboard, behind GitHub sign-in.                                                                                                                                                                                                                                          |
| 6   | **Every Create opens a new request** and runs `/create` in one step, with the flavor chosen on the portal each time. A request is one instance's lifetime: the portal never runs `/create` on an existing request. Revised 2026-09-25 (this is how users change flavor).                                      |
| 7   | GitHub issues and workflows stay the engine and the audit trail. The portal does not start, stop or change instances itself.                                                                                                                                                                                  |
| 8   | **Primary path:** sign-in moves to a MorphoCloud **GitHub App**, and buttons post the command (`/shelve`, …) on the user's issue **as the user**. The existing workflow checks apply.                                                                                                                         |
| 9   | **Backup / second test:** the portal checks that the user owns the issue, then the bot runs the existing `*-from-workflow` dispatch workflows. Built only if needed, or as a comparison.                                                                                                                      |
| 10  | The portal gets **no OpenStack credentials**. State comes from the issue's `status:*` labels. Access details are pushed to the portal by the workflow over the restricted SSH channel.                                                                                                                        |
| 11  | Individual instances only. Courses and workshops are out of scope.                                                                                                                                                                                                                                            |
| 12  | A portal-opened issue is opened with the **user's token** (the user stays the author). A workflow adds the form's three labels plus `request-source:portal`, and the request handler runs once, on that label.                                                                                                |
| 13  | The user's GitHub token is kept **in the portal's memory only**: never on disk, never in the cookie. A portal restart signs everyone out.                                                                                                                                                                     |
| 14  | **One instance per user.** With per-user shares, two instances would mount the same share at once. The portal offers Create instance only when the user has no open request, and never a second one.                                                                                                          |
| 15  | The GitHub App is **public** ("Any account"), so people who have not joined can authorize it and see the portal's "Not a MorphoCloud member" page with the join link. The portal's team check is the gate. The app has **no private key**.                                                                    |

## Sign-in (decision 8)

The current OAuth App only reads team membership (`read:org`). Posting a command
as the user would need the `public_repo` scope, which is write access to every
public repo the user can write to. A GitHub App limits the same sign-in to what
the portal needs.

GitHub App settings:

- Callback URL: `https://<portal host>/auth/callback`
- Expire user authorization tokens: **on** (8 hours)
- Webhook: **off**
- Repository permissions: **Issues: read and write**
- Organization permissions: **Members: read** (team check)
- Where can this GitHub App be installed: **Any account** (decision 15)
- Private keys: **none**
- Installed on **Test-Instances only**

The user's token is kept in memory only (decision 13). The OAuth App is retired
once the GitHub App works.

## What the page shows

- One card per open individual request the user opened. The per-user instance
  limit is still enforced by the workflows
  (`MORPHOCLOUD_MAX_INSTANCES_PER_USER`).
- State from `status:*` labels: `status:active` → active; `status:shelved` or
  `status:shelved_offloaded` → shelved; `status:deleted` or no label → no
  instance. Any other label is shown as-is with the issue link.
- Buttons match the state: active → Shelve and Delete; shelved → Unshelve and
  Delete; no instance → none (the Create form below applies). After a press, the
  card shows "Working…" and the buttons are hidden until the workflow reacts on
  the command comment (👍 finished, 👎 failed; see `report-command-outcome`), at
  most 3 hours. A failed command is noted on the card for a day.
- Delete opens a confirmation page: "This removes your instance. Anything saved
  only on the instance is lost. Your files in your storage are not affected."
  with **Delete instance** and **Cancel**.
- After Delete finishes (👍 on `/delete_instance`), the portal closes the
  request with the user's token. If the user is signed out by then, it closes at
  their next sign-in. If the delete failed (👎), the request stays open.
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

1. The portal writes a marker line in the issue body.
2. A new labeler workflow runs on `issues: opened`. When the body has the marker
   and the issue has no `request-type:*` label, it adds the three form labels,
   then, in a second call, `request-source:portal`. The handler reads the other
   labels from that event's payload, so they must already be on the issue. It
   uses the workflow GitHub App token (`vars.MORPHOCLOUD_WORKFLOW_APP_ID` +
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

## Not decided (not in the first version)

Renew and an expiration display. Each needs the maintainer's decision before it
is added.

## Test plan (Test-Instances)

1. Sign in with the GitHub App; non-members are refused.
2. First Create: issue opened, `/create` runs, card turns active, access details
   and passphrase shown.
3. Shelve and Unshelve from the dashboard; the new address appears after
   unshelve.
4. The issue timeline shows the commands as posted by the user.
5. A second user cannot see or act on the first user's instance.
6. Backup path (decision 9) run once as a comparison.
