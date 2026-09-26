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

| #   | Decision                                                                                                                                                                                 |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | The dashboard is part of the data portal: same VM, same sign-in, same page as the storage card.                                                                                          |
| 2   | Each instance card shows the **state** (no instance, active, shelved), the **access details** (Web connect, SSH, TurboVNC) and a **link to the request issue** for diagnostics.          |
| 3   | No Data drop entry. The data portal replaces it.                                                                                                                                         |
| 4   | Buttons: **Create**, **Shelve**, **Unshelve**. Nothing else in the first version.                                                                                                        |
| 5   | The **passphrase is shown** on the dashboard, behind GitHub sign-in.                                                                                                                     |
| 6   | The first **Create** opens the request issue and runs `/create` in one step. The user picks the flavor on the portal. There is no separate "request" step.                               |
| 7   | GitHub issues and workflows stay the engine and the audit trail. The portal does not start, stop or change instances itself.                                                             |
| 8   | **Primary path:** sign-in moves to a MorphoCloud **GitHub App**, and buttons post the command (`/shelve`, …) on the user's issue **as the user**. The existing workflow checks apply.    |
| 9   | **Backup / second test:** the portal checks that the user owns the issue, then the bot runs the existing `*-from-workflow` dispatch workflows. Built only if needed, or as a comparison. |
| 10  | The portal gets **no OpenStack credentials**. State comes from the issue's `status:*` labels. Access details are pushed to the portal by the workflow over the restricted SSH channel.   |
| 11  | Individual instances only. Courses and workshops are out of scope.                                                                                                                       |

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
- Installed on **Test-Instances only**

The user's token is kept on the server, never in the browser cookie, and is
dropped on sign-out or expiry. The OAuth App is retired once the GitHub App
works.

## What the page shows

- One card per open individual request the user opened. The per-user instance
  limit is still enforced by the workflows
  (`MORPHOCLOUD_MAX_INSTANCES_PER_USER`).
- State from `status:*` labels: `status:active` → active; `status:shelved` or
  `status:shelved_offloaded` → shelved; `status:deleted` or no label → no
  instance. Any other label is shown as-is with the issue link.
- Buttons match the state: no instance → Create; active → Shelve; shelved →
  Unshelve. After a press, the card shows "Working…" and the buttons are
  disabled until the label changes.
- No open request → a **Create instance** button with the flavor list from the
  request form.

## Access details (decisions 5, 10)

The workflow step that sends the credentials email also sends the Web connect,
SSH and TurboVNC details and the passphrase to the portal. It uses the same
restricted SSH channel as share creation, with one new command in the gate.

- Replaced on every create and unshelve (the address changes).
- Cleared on shelve and delete.
- Shown only to the issue's author.

## Open question to settle before building

**Labels on a portal-opened issue.** The request handler and `/create` run only
when the issue already has the `request-type:instance` label. GitHub drops
labels on issues opened through the API by users without write access. So an
issue the portal opens with the user's token arrives unlabeled, and nothing
runs. Options:

- **A.** The user's token opens the issue; the handler also runs on the
  `labeled` event, and the bot adds the labels.
- **B.** The bot opens the issue and assigns the user. The `/create` allowlist
  already includes assignees, but the handler's author checks (team membership,
  per-user limit, `request-creator:user`) need changing.

## Security

- A user sees and acts only on issues they opened (matched by GitHub numeric ID,
  not login).
- The workflows' own authorization stays the real gate on the primary path.
- The data portal's rules carry over unchanged: nothing here can reset,
  overwrite or delete a user's share.

## Not decided (not in the first version)

Delete, renew, and an expiration display. Each needs the maintainer's decision
before it is added.

## Test plan (Test-Instances)

1. Sign in with the GitHub App; non-members are refused.
2. First Create: issue opened, `/create` runs, card turns active, access details
   and passphrase shown.
3. Shelve and Unshelve from the dashboard; the new address appears after
   unshelve.
4. The issue timeline shows the commands as posted by the user.
5. A second user cannot see or act on the first user's instance.
6. Backup path (decision 9) run once as a comparison.
