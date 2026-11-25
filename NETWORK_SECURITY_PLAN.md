# Network exposure on production (BIO180006) — design note

Status: **proposed, 2026-11-25.** Nothing here is applied until the maintainer
approves this note. Scope agreed with the maintainer on 2026-11-25: items 1–3
below. The Exosphere web-desktop proxy stays (members who need smooth 3D use
TurboVNC). The k8s proof of concept (`mc-k8s-*`, `mc-jump`) and the MorphoDepot
server are out of scope here; their findings are listed at the end.

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

| Direction | Protocol | Port  | From               | Why                                   |
| --------- | -------- | ----- | ------------------ | ------------------------------------- |
| in        | tcp      | 22    | anywhere           | member SSH; runners                   |
| in        | tcp      | 5901  | anywhere           | TurboVNC (TLS only on the server)     |
| in        | tcp      | 49528 | 149.165.159.111/32 | web desktop through Exosphere's proxy |
| in        | icmp     | —     | anywhere           | ping, for diagnosis                   |
| out       | any      | —     | anywhere           | unchanged                             |

Dropped compared with `exosphere`: every other TCP port, Mosh (no member
document mentions it), and the IPv6 rules (the project network has no IPv6
subnet).

Code (one MWF PR, vendorized to Instances):

- `create-instance` takes the group from a new input fed by the repository
  variable `MORPHOCLOUD_SECURITY_GROUP`, **default `exosphere`**. Only
  `MorphoCloud/Instances` sets it (to `mc-instance`). Course and workshop
  repositories on other allocations keep `exosphere` and change nothing; the
  group need not exist there. Workshops and courses created from
  `MorphoCloud/Instances` itself get `mc-instance`, which serves them the same
  way (same ports).
- `bake-image.sh` and `check-image.sh` use `mc-instance` (they run only on
  BIO180006; the build VM needs only SSH from runner-1).
- `scripts/network-security-groups.sh`: creates or updates `mc-instance` (and
  the groups of item 2) from the tables in this note, idempotently, and prints
  what it changed. It is the only writer of these groups.

**The proxy's address can change without notice.** If it does, every web desktop
stops working while SSH and TurboVNC keep working. To catch it:

- A daily check on runner-1 (a new crontab line with its `# dispatch:` marker,
  rendered by `check-workflow-clock.py`) resolves `proxy-js2-iu.exosphere.app`
  and compares it with the 49528 rule of `mc-instance`. A difference sends the
  admin email (`admin-alert.yml`) with the one command that fixes the rule. It
  never changes the rule itself.
- The fix is to re-run `network-security-groups.sh` with the new address.

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
| groups `mc-instance`, `mc-runner`, `mc-join` | `network-security-groups.sh` only (maintainer)                                                                       | new                                                                                                      |
| group `mc-portal`                            | maintainer (by hand so far)                                                                                          | one rule removed; `network-security-groups.sh` does not manage it                                        |
| member instance ports                        | `create-instance` (at create); unshelve keeps the port and its groups; delete removes the port                       | new instances get `mc-instance`; existing ones are switched once (below)                                 |
| Exosphere's proxy                            | Exosphere's operators; we cannot change it                                                                           | it alone may reach 49528; the daily check watches its address                                            |
| runner-1 → runner-2, join, portal SSH        | the harvester (`mcjournal`), pickup and usage (`mcrunner`), the maintainer                                           | unchanged: 22 stays open on runner-2 and join; the portal rules for the runners stay                     |
| the reconcile probe (port 22)                | portal pickup, every minute                                                                                          | unchanged                                                                                                |
| k8s session (`mc-jump`)                      | the k8s proof of concept                                                                                             | loses SSH to `mc-portal`; it is told before the rule is removed                                          |
| course repositories (other allocations)      | their own runners, vendorized `create-instance`                                                                      | none: the variable is unset there, so `exosphere` as before                                              |

## Rollout, in order (each production step after the maintainer's OK)

1. This note merged.
2. The MWF PR (variable, scripts, daily check, tests), Claude review, merged.
   Merging changes nothing yet: the variable is unset, so `create-instance`
   still uses `exosphere`.
3. `network-security-groups.sh` on BIO180006: creates the three groups. Nothing
   uses them yet.
4. Live check on one test instance (#288): attach `mc-instance`, detach
   `exosphere` (port update, no reboot). Then, from outside: the web desktop
   opens through the portal's link; SSH and TurboVNC connect;
   `http://<ip>:49528` direct from runner-1 and from the Mac times out; a test
   listener on another port (e.g. 49530) is unreachable; MyDrive and `/software`
   stay mounted.
5. Set `MORPHOCLOUD_SECURITY_GROUP=mc-instance` on Instances; vendorize. Create
   one instance through the portal (as `amm554`) and repeat step 4's checks.
6. Switch every other `instance-*` port (today only #288 exists).
7. Item 2: for each runner and the join VM, add the new group first, then remove
   `exosphere` (no gap; the open SSH session survives). Check SSH from the Mac
   and runner-1 → runner-2/join (`mcjournal`), and the join site.
8. Item 3: remove the `mc-jump` rule from `mc-portal` (k8s session told first).
9. Install the daily proxy-address check on runner-1; run it once by hand.

**Rollback:** re-attach `exosphere` to a port (one command per server); unset
the variable. The groups can stay.

## Out of scope, reported only (2026-11-25)

- `mc-k8s-portal`: the Kubernetes API (6443), the kubelet (10250), 5000,
  5901–5904 and 8472/udp are open to any source. An open kubelet port is the
  most serious exposure found on BIO180006.
- `morphoDepot-server`: SSH password login is on and root may log in with a key;
  Guacamole on 49528 is published by Docker, which bypasses `ufw`.
- The project's `default` group allows all incoming traffic. No port uses it,
  but a server created without a group would get it.
