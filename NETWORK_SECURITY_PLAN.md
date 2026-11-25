# Network exposure on production (BIO180006) — design note

Status: **proposed, 2026-11-25** (not applied; the date changes to the approval
date when merged). Nothing here is applied until the maintainer approves this
note. Scope agreed with the maintainer on 2026-11-25: items 1–3 below. The
Exosphere web-desktop proxy stays (members who need smooth 3D use TurboVNC). The
k8s proof of concept (`mc-k8s-*`, `mc-jump`) and the MorphoDepot server are out
of scope here; their findings are listed at the end.

## What was found (read-only inventory, 2026-11-25)

- **Member instances accept every TCP port from the whole internet.** They get
  the security group `exosphere`. Exosphere's built-in default rules include
  "Expose all incoming ports" (any TCP port, any source), plus SSH, ping, Mosh
  (UDP 60000–61000) and Guacamole (49528). The image runs no host firewall.
  Anything that listens on an instance is therefore public: SSH (22), Guacamole
  (49528, plain HTTP inside the container), TurboVNC (5901, TLS only), and
  anything a member starts (Slicer's web server, Jupyter, …).
- The same group is on `morphocloud-instances-runner`,
  `morphocloud-instances-runner-2`, `morphocloud-org-server` (join) and
  `morphoDepot-server`. On the first three `ufw` (INPUT policy DROP) allows only
  22, or 22/80/443, so the group's openness has no effect there today; the
  security group is not a second layer.
- `mc-portal` has its own group: 80/443 from anywhere; 22 only from the two
  runners (floating and fixed addresses) and from `149.165.149.15` (`mc-jump`,
  the k8s proof of concept's jump host). `ufw` is off; the group is the only
  layer.
- Exosphere **rewrites the rules of the group named `exosphere`** to its own
  defaults whenever anyone opens the project in the Exosphere web app
  (`docs/security-groups.md` in the Exosphere fork: "If rules are added or
  removed manually, the application will delete ones not in the config & restore
  ones which are"). Tightening that group in place would not last.

**What each member-facing path needs**

| Path                    | Who connects to the instance                                   | Port  |
| ----------------------- | -------------------------------------------------------------- | ----- |
| Web desktop (Guacamole) | Exosphere's proxy `proxy-js2-iu.exosphere.app`                 | 49528 |
| SSH                     | the member's computer; the runners (setup, diagnostics, probe) | 22    |
| TurboVNC                | the member's computer (TLS-only security types)                | 5901  |
| MyDrive, `/software`    | outgoing only, to Jetstream2's Ceph monitors and OSDs          | —     |
| Internet (apt, pip, …)  | outgoing only                                                  | —     |

Measured on `instance-288`, 2026-11-25 (packet capture of SYN packets to 49528
while loading the page through the proxy): the proxy connects **from
149.165.159.111**, the address its hostname resolves to. Exosphere does not
document this address (its own rule for 49528 is open to any source; the proxy
exists to give browsers a trusted TLS certificate, not to restrict access). None
of our code connects to 49528 directly: every web-desktop link and the portal's
Guacamole token request go through the proxy, and the portal's reachability
probe (`mc_share_runner.py` `reachable`) uses port 22.

Storage and download speed do not depend on incoming rules (measured the same
day: instance ↔ MyDrive 0.8–1.6 GB/s, portal file browser ~1 GB/s). Nothing
below changes outgoing traffic.

## The change

### 1. Member instances: a new group `mc-instance`

A **new** group, so the Exosphere web app never rewrites it:

| Direction | Protocol | Port  | From                                                 | Why                                   |
| --------- | -------- | ----- | ---------------------------------------------------- | ------------------------------------- |
| in        | tcp      | 22    | anywhere                                             | member SSH; runners                   |
| in        | tcp      | 5901  | anywhere                                             | TurboVNC (TLS only on the server)     |
| in        | tcp      | 49528 | the proxy's address (2026-11-25: 149.165.159.111/32) | web desktop through Exosphere's proxy |
| in        | icmp     | —     | anywhere                                             | ping, for diagnosis                   |
| out       | any      | —     | anywhere                                             | unchanged                             |

Dropped compared with `exosphere`: every other TCP port, Mosh, and the IPv6
rules (the project network has no IPv6 subnet).

- **The proxy's address set** (checked 2026-11-25): `proxy-js2-iu.exosphere.app`
  resolved to the one address 149.165.159.111 on every lookup, from the Mac's
  resolver, 8.8.8.8, 1.1.1.1 and runner-1 (TTL 300 s), with no IPv6 address. The
  rule allows every IPv4 address the name resolves to (today one), and the check
  below compares the whole set.
- **What members may lose.** No MorphoCloud document (MWF, the portal and its
  chatbot knowledge, the docs site) tells members to use Mosh, Jupyter, port
  forwarding or any port other than 22, 5901 and the web desktop. The docs list
  "R / RStudio, provided by Jetstream2"; whether that runs a web server on the
  instance is checked at rollout step 4, before anything is closed. A member who
  starts their own web server (Jupyter, Slicer's web server) reaches it through
  SSH port forwarding (`ssh -L 8888:localhost:8888 exouser@<ip>`); one FAQ entry
  in the docs says so, and the chatbot's knowledge is rebuilt from it.

Code (one MWF PR, vendorized to Instances):

- `create-instance` takes the group from a new input fed by the repository
  variable `MORPHOCLOUD_SECURITY_GROUP`, **default `exosphere`**. An unset
  variable is an empty string, so the default is applied twice: in the workflow
  expression (`vars.MORPHOCLOUD_SECURITY_GROUP || 'exosphere'`) and in the shell
  step (`${SECURITY_GROUP:-exosphere}`). Before `server create`, the step checks
  that the group exists (`openstack security group show`) and stops with a clear
  error if not, so no half-created instance is left.
- The variable is set **at the repository level on `MorphoCloud/Instances`
  only**, never at the organization level (an organization variable would reach
  every course repository and ask for a group that does not exist on their
  allocations). Course and workshop repositories on other allocations keep
  `exosphere` and change nothing. Workshops and courses created from
  `MorphoCloud/Instances` itself get `mc-instance`, which serves them the same
  way (same ports).
- `bake-image.sh` and `check-image.sh` use `mc-instance` (they run only on
  BIO180006; the build VM needs only SSH from runner-1).
- `scripts/network-security-groups.sh`: writes `mc-instance`, `mc-runner` and
  `mc-join`. It is **declarative**: it creates each group, adds the rules in
  this note's tables and removes any other rule in those groups (so a rule added
  by hand is reverted). It takes the proxy's address the same way the hourly
  check does (below), never from a fixed value, so the two never undo each
  other. It is a **dry run by default** and prints the difference; `--apply`
  changes. It runs by hand (maintainer, or runner-1 with its existing OpenStack
  credentials), never inside a workflow.
- Checked by `git grep` (MWF, portal) and GitHub code search (Instances): every
  use of 49528 builds a proxy URL (`generate-connection-url`,
  `list-instance-credentials.sh`, the portal's `web_url`); nothing reaches 49528
  or 5901 directly. The portal's probe uses 22.

**The proxy's address can change without notice.** If it does, every web desktop
stops working while SSH and TurboVNC keep working. To catch it:

- A security-group rule can hold only addresses, not a hostname, so the rule
  follows the name instead: an **hourly** check on runner-1 resolves
  `proxy-js2-iu.exosphere.app` and compares the set of addresses with the 49528
  rules of `mc-instance`.
- On a difference it **updates the rules itself** (adds the new addresses first,
  then removes the old ones, so there is no gap) and emails the admins
  (`admin-alert.yml`) what changed: old and new addresses, time. Decided by the
  maintainer 2026-11-25, to see how often, if ever, the address changes.
- It accepts only addresses inside Jetstream2's public range 149.165.0.0/16. An
  answer outside it, no answer, or a failed OpenStack call changes nothing and
  emails the admins (no silent failure). Trusting the name adds little risk:
  whoever could repoint `exosphere.app`'s DNS could already send every member's
  browser to their own server.
- Every run that changes something, and every refusal, writes one line to
  `~/netsec/proxy-check.log` (and the uniform `DISPATCH` line for the email), so
  the number of changes over time can be read back. A run with no change writes
  nothing.
- It is a crontab line on runner-1 only (BIO180006), rendered by
  `check-workflow-clock.py` with the same `PATH`, `~/venv` and `OS_CLOUD` as the
  other lines; it does not exist in course repositories.
- Accepted by design: the update follows the change, so in the worst case the
  web desktop is down for up to an hour. SSH and TurboVNC are not affected.

### 2. Runners and join VM: their own groups

| Server                                                           | New group   | In                                  |
| ---------------------------------------------------------------- | ----------- | ----------------------------------- |
| `morphocloud-instances-runner`, `morphocloud-instances-runner-2` | `mc-runner` | tcp 22 from anywhere; icmp          |
| `morphocloud-org-server` (join)                                  | `mc-join`   | tcp 22, 80, 443 from anywhere; icmp |

The same ports `ufw` allows today, so nothing reachable changes; the security
group becomes a second layer. 80 stays on join for the certbot HTTP challenge
and the redirect. SSH stays open to any source on the runners: the maintainer
connects from changing addresses, and logins are key-only (password and
keyboard-interactive off, root off; checked 2026-11-25).

### 3. Portal: one rule removed

Remove `tcp 22 from 149.165.149.15/32` (`mc-jump`) from `mc-portal`'s group.
Nothing in the portal, the runners or the harvester uses it; the runners keep
their four rules.

## Actor map: who else touches each resource

| Resource                                     | Who writes or acts on it                                                                                             | What this change does                                                                                    |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| group `exosphere` (BIO180006)                | the Exosphere web app (restores its defaults on use); `create-instance`, `bake-image.sh`, `check-image.sh` attach it | left as it is; after the switch no production server needs it except `morphoDepot-server` (out of scope) |
| groups `mc-instance`, `mc-runner`, `mc-join` | `network-security-groups.sh` (maintainer); the hourly check (only `mc-instance`'s 49528 rules)                       | new                                                                                                      |
| group `mc-portal`                            | maintainer (by hand so far)                                                                                          | one rule removed; `network-security-groups.sh` does not manage it                                        |
| member instance ports                        | `create-instance` (at create); unshelve keeps the port and its groups; delete removes the port                       | new instances get `mc-instance`; existing ones are switched once (below)                                 |
| Exosphere's proxy                            | Exosphere's operators; we cannot change it                                                                           | it alone may reach 49528; the hourly check follows its address                                           |
| runner-1 → runner-2, join, portal SSH        | the harvester (`mcjournal`), pickup and usage (`mcrunner`), the maintainer                                           | unchanged: 22 stays open on runner-2 and join; the portal rules for the runners stay                     |
| the reconcile probe (port 22)                | portal pickup, every minute                                                                                          | unchanged                                                                                                |
| k8s session (`mc-jump`)                      | the k8s proof of concept                                                                                             | loses SSH to `mc-portal`; it is told before the rule is removed                                          |
| course repositories (other allocations)      | their own runners, vendorized `create-instance`                                                                      | none: the variable is unset there, so `exosphere` as before                                              |

## Rollout, in order (each production step after the maintainer's OK)

1. This note merged.
2. The MWF PR (variable, scripts, hourly check, tests), Claude review, merged.
   Merging changes nothing yet: the variable is unset, so `create-instance`
   still uses `exosphere`.
3. `network-security-groups.sh` on BIO180006: creates the three groups. Nothing
   uses them yet.
4. Live check on one test instance (#288), unshelved:
   - first, list every port listening on a non-local address (`ss -tulnp`),
     including whether RStudio Server runs. Anything member-facing beyond 22,
     5901 and 49528 stops the rollout and amends this note;
   - switch the port in one call, which replaces the whole list:
     `openstack port set --no-security-group --security-group mc-instance <port>`
     (no reboot);
   - from outside: the web desktop opens through the portal's link; SSH and
     TurboVNC connect; `http://<ip>:49528` direct from runner-1 and from the Mac
     times out; a test listener on another port (e.g. 49530) is unreachable;
     MyDrive and `/software` stay mounted;
   - shelve and unshelve once: the port keeps `mc-instance`.
5. Set `MORPHOCLOUD_SECURITY_GROUP=mc-instance` as a repository variable on
   Instances (and confirm `gh variable list --org MorphoCloud` does not hold
   it); vendorize. From here `create-instance`, `bake-image.sh` and
   `check-image.sh` all use `mc-instance`. Create one instance through the
   portal (as `amm554`) and repeat step 4's outside checks.
6. Switch every other `instance-*` port, shelved ones included, with the same
   one-call command. The list is read when the step runs, not fixed here
   (2026-11-25: only #288). Running it twice changes nothing.
7. Item 2, for each runner and the join VM, in two calls with no gap:
   `openstack port set --security-group mc-runner <port>` (adds; both groups
   attached), check SSH, then
   `openstack port set --no-security-group --security-group mc-runner <port>`
   (only the new group). The open SSH session survives (security groups are
   stateful). Check SSH from the Mac, runner-1 → runner-2/join (`mcjournal`),
   and the join site.
8. Item 3: remove the `mc-jump` rule from `mc-portal` (k8s session told first).
9. Install the hourly proxy-address check on runner-1; run it once by hand, and
   once with a deliberately wrong address in the rule to see it correct the rule
   and send the email.
10. The docs FAQ entry on SSH port forwarding; the chatbot's knowledge rebuilt.

**Rollback:**

- An instance or server:
  `openstack port set --no-security-group --security-group exosphere <port>`.
  Exosphere may have rewritten that group's rules in the meantime; its defaults
  still open every port these servers need, but look at its rules before relying
  on it.
- New instances: delete the repository variable on Instances.
- Item 3: re-add `tcp 22 from 149.165.149.15/32` to `mc-portal`.
- The new groups can stay.

## Follow-ups, not in this change

- A host firewall in the golden image (`ufw`, with Docker's published ports
  handled through the `DOCKER-USER` chain), as a second layer on instances.
- `mc-portal`'s group under `network-security-groups.sh`, so it gets the same
  drift check (its group is the only layer there; `ufw` is off).

## Out of scope, reported only (2026-11-25)

- `mc-k8s-portal`: the Kubernetes API (6443), the kubelet (10250), 5000,
  5901–5904 and 8472/udp are open to any source. An open kubelet port is the
  most serious exposure found on BIO180006.
- `morphoDepot-server`: SSH password login is on and root may log in with a key;
  Guacamole on 49528 is published by Docker, which bypasses `ufw`.
- The project's `default` group allows all incoming traffic. No port uses it,
  but a server created without a group would get it.
