# Storage Redesign — Per-User Shares and Golden Images

Design spec agreed on 2026-09-25. Status: **testing**, on the Test-Instances
allocation. Nothing here is in production. Tracking issue:
[#306](https://github.com/MorphoCloud/MorphoCloudWorkflow/issues/306).

## Goals

1. A user's files outlive any single instance: they survive a failed unshelve, a
   delete and recreate, and a change of flavor, with no copying.
2. Users can upload and download files without a running instance.
3. Each user sees and changes only their own files.
4. Free the per-instance data volume from the ~100 volume + instance quota.
5. Faster, more predictable instance creation.

## Decisions

These are settled. Revisit only with new facts, not preference.

| #   | Decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Each user gets their own 100 GB Manila CephFS share. It replaces the per-instance MyData Cinder volume.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 2   | Shares are keyed on the **GitHub numeric user ID**, not the login. Logins can be renamed or reclaimed; the ID never changes.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 3   | The home directory and all settings stay on the instance's root disk. Only the user-facing folders live on the share.                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| 4   | A data portal on its own VM provides browser upload and download to each user's share, with GitHub sign-in (members of MorphoCloudUsers only). It works with or without an instance.                                                                                                                                                                                                                                                                                                                                                                     |
| 5   | Two golden images, both with the NVIDIA driver: a vGPU (GRID) image for g3.large, and a regular-driver image for every other flavor, including CPU-only.                                                                                                                                                                                                                                                                                                                                                                                                 |
| 6   | Slicer and the standard extension set are baked into the images. Extensions a user adds are lost when the instance is recreated. Accepted.                                                                                                                                                                                                                                                                                                                                                                                                               |
| 7   | Storage term: a share is kept for 120 days from its creation. Its member can ask for more time on the portal; an admin approves (120 more days each time, no limit) or declines. Reminders 14, 7 and 1 day before the end date; then the share is locked for 15 days, then deleted. Instances have no expiry of their own: they exist while the storage is kept (see "Storage term"). Revised 2026-11-24 by the maintainer (was: deleted after 180 days without use, renewed by the member with no review; before that, no expiry).                      |
| 8   | No backups. Same as MyData volumes today.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 9   | No migration path from MyData during testing. Designed only if this is adopted.                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 10  | The current per-instance upload page ("Data drop") stays in production until this design is adopted.                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 11  | Workshop instances get no per-attendee share or volume. They use one centralized workshop share.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 12  | R libraries live on the root disk. MorphoCloud no longer installs them.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 13  | Slicer's DICOM database moves to local disk. DICOM is not a common use case.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 14  | Shares are created on demand, **only from the data portal** ("Create my storage"). `/create` without a share refuses and points to the portal; it never creates a share. No automatic provisioning of every member. Revised 2026-09-26 (was: `/create` creates it).                                                                                                                                                                                                                                                                                      |
| 15  | The request issue's lifecycle covers the instance only. Volume commands and `volume:*` labels are removed; the share has its own lifecycle.                                                                                                                                                                                                                                                                                                                                                                                                              |
| 16  | Reset empties the share (share and key are kept). The only automatic deletion is the storage-term sweep (decision 7), after its reminders and 15 days locked; the member's instance is deleted when the share locks. Revised 2026-10-14 (was: nothing ever deletes a share automatically) and 2026-11-24 (was: never while an instance holds the share).                                                                                                                                                                                                 |
| 17  | **One instance per user.** Two instances would mount the same share at once. Enforced with the existing per-user limit, `MORPHOCLOUD_MAX_INSTANCES_PER_USER=1` (set on Test-Instances for the prototype). Admins are exempt from that limit, as today.                                                                                                                                                                                                                                                                                                   |
| 18  | **OS updates arrive through the image.** `/create` from a baked image runs neither `apt-get update` nor `apt-get upgrade` (amended 2026-11-16: setup installs nothing on a baked image); the bake keeps both. A package ansible installs must be in the image. Both images are rebuilt monthly whether or not Slicer changed (issue #430). Added 2026-11-16.                                                                                                                                                                                             |
| 19  | **Background security updates between rebakes.** `unattended-upgrades` is turned on when an instance's setup completes. Security fixes only: the Ubuntu security pocket is the only allowed source (no `noble-updates`, no feature updates). After each boot the lists are refreshed at 30-40 minutes and the upgrade runs at 45-55 minutes; daily at 06:00/18:00 (refresh) and 06:30 UTC (upgrade), at idle priority. Kernel and NVIDIA packages are excluded; they change only with a rebake. Added 2026-11-16; sources and timing amended 2026-11-17. |
| 20  | **The desktop does not prompt members to upgrade.** Software Updater does not open on its own, and no new Ubuntu release is offered. Security fixes still install in the background (decision 19). Software Updater stays in the app menu for a member who wants to run it. Added 2026-11-23.                                                                                                                                                                                                                                                            |
| 21  | **Long-shelved instances are deleted.** Once a week, an instance shelved for more than 28 days is deleted and its request closed; the member's files stay on the storage, and a new instance can be created from the portal at any time (see "Long-shelved instances"). Decided 2026-11-24 by the maintainer.                                                                                                                                                                                                                                            |

## Why per-user shares, not one big share with folders

One large share with a folder per user was the first idea. It fails the
isolation goal:

- **Manila grants access to a whole share, never a folder inside it.** A share's
  access rules are cephx keys covering the entire share. The access-rule API has
  no path option.
- **Users have sudo on their instance.** Whatever key an instance holds, the
  user can read it and mount the share's top level. Mounting only the user's
  folder is a default, not a boundary.
- **Folder-restricted keys exist in Ceph but only Ceph administrators can create
  them**, meaning Jetstream2 staff, per user. Data from all folders would still
  share one storage namespace.
- **The worst case is deletion, not snooping.** The share type has no snapshot
  support. With one shared read-write key, a single malicious user or
  compromised instance could delete everyone's files with no way to restore
  them. Instances have public IPs, accept password SSH, and run arbitrary user
  code.

With one share per user, a compromised instance can damage only its own user's
data, and Manila enforces the 100 GB limit as the share size with no special
permissions.

## Why the home directory is split

- **Not the whole home on the share.** Jetstream2 does not permit heavy lock use
  on shares and can evict the client, which freezes the mount until a remount or
  reboot. A home directory is lock-heavy: the GNOME session, Firefox profile
  databases, Slicer settings, SQLite files. A frozen home freezes the desktop.
- **Not the whole home on a larger root disk.** Root disks are fixed per flavor
  (60 GB; 20 GB on the small m3 sizes). A larger root needs boot-from-volume,
  which is still a volume. The root disk also dies in exactly the cases users
  most need their files: our recovery from a failed shelve or unshelve is to
  delete and recreate the instance.

## Components

### 1. Share per user

- **Size:** 100 GB. **Protocol:** CephFS, share type `cephfsnativetype`.
- **Name:** derived from the GitHub numeric user ID, for example `mc-user-<id>`.
  The display login is informational only.
- **Access:** one read-write cephx access rule per share. The instance for that
  user gets that key; the central service holds all keys.
- **Export path format** (from an existing share):
  `<mon1>:6789,...:/volumes/_nogroup/<uuid>/<uuid>`.
- **Bookkeeping lives on the share itself**, as Manila share metadata: the
  GitHub login at creation, when the welcome email was sent, and when the share
  was last mounted by an instance (updated at every create and unshelve). No new
  database or sheet column. The last-mounted time is what a future expiry rule
  will be measured against.

**Provisioning: on demand, two entry points.**

1. **The portal's "Create my storage" button.** The portal records the request.
   The portal holds no OpenStack or GitHub credentials, so the runner host polls
   it every minute over SSH, through an account whose forced command allows only
   `claim`, `install <id>` and `fail <id>`. The runner creates the share with
   its own credentials and hands the portal the details to mount.
2. **`/create` without a share refuses** with a link to the portal (revised
   2026-09-26), so every share is created by the portal and the portal always
   knows about it.

The portal's creation path uses the share-creation logic (`ensure`), which
**fails closed** (`/create` only looks the share up, see the Phase 2 prototype):

- every OpenStack error stops the run; a failed lookup is never read as "no
  share exists";
- more than one share matching a GitHub ID (by name or by the `mc_github_id`
  property) stops the run;
- an existing share is reused as is, whatever its state; a share in an error
  state is left for an admin;
- after creating, the lookup runs again and must find exactly one share;
- the code has no delete call; a test enforces it.

The portal side refuses to remap a user to a different share and refuses to
mount over a non-empty directory. Every refusal or failure emails the admins,
and the user sees "could not be created" with a retry button; a retry reuses the
share if it exists.

The numeric ID comes from the GitHub sign-in on the portal, and from
`github.event.issue.user.id` in `/create` and other comment-triggered commands
(the issue creator, who owns the instance, not the commenter). No extra API
call, no dependence on the current login.

The welcome email is unchanged: storage is created only when a user asks.

**Keys.** Manila stores each share's access key; there is no separate key store.
At create and unshelve the runner reads the key from Manila and writes it to a
root-only file on the instance, which the mount uses after reboots. The user can
read it with sudo; it grants only their own share. **Rotation** for a
compromised key: deny the share's access rule, grant a new one, and redeliver
the key with the next create or unshelve. The data is untouched. A mount that is
already active may keep working on the old key until it is remounted, so an
emergency rotation also reboots or shelves the affected instance (test 13 checks
whether denying the rule already cuts active mounts).

### 2. Instance side

**Mount.** At creation, the runner (already SSHing in during setup) installs the
user's key in a root-only file and adds a mount entry so the share mounts at
boot at a fixed path, `/media/share/MyDrive`. Exosphere already ships a CephFS
mount script (`mount_ceph.py`) used for the R library share.

**Layout on the share**, created on first mount if missing:

```
/media/share/MyDrive/
├── Desktop/
├── Documents/
└── Downloads/
```

No Uploads folder: the portal's file browser can upload into any folder on the
share (decided 2026-09-26). An existing Uploads folder is left alone; nothing
deletes user files.

**Links in the home directory** (home stays on the root disk):

```
/home/exouser/Desktop   -> /media/share/MyDrive/Desktop
/home/exouser/Documents -> /media/share/MyDrive/Documents
/home/exouser/Downloads -> /media/share/MyDrive/Downloads
```

GNOME, the file manager, Firefox, file dialogs and Slicer use the standard XDG
user folders, which resolve through these links.

**Requirements:**

- The share must be mounted **before** the desktop session starts (systemd
  ordering before `vncserver@1`). Otherwise GNOME finds a broken link and
  silently resets the folder to a local one.
- **A failed mount must block the desktop, not degrade it.** `vncserver@1`
  requires the mount unit, so a failed mount means no desktop session and a
  visible error, never an empty local Desktop where the user would save work
  that disappears with the instance. A small failure handler replaces the
  generic error with a plain message: the user's storage could not be reached,
  try again shortly, and contact us if it persists.
- Turn off the automatic XDG folder reset (`xdg-user-dirs` update) so a late
  mount can never replace the links.
- Setup copies the desktop launchers (Slicer, ExtendInstanceSession) into the
  share's Desktop **after** mounting, refreshing them on each new instance.
  Today ansible writes them into `~/Desktop` at first boot, before any share
  exists.
- Setup sets Slicer's DICOM database folder to a local path, for example
  `~/.local/share/SlicerDICOMDatabase`.
- Remove today's home relocation onto MyData and the `R_LIBS_USER` /
  `R_LIBS_SITE` setup.

**What stays on the root disk:** Slicer, app settings, caches, R libraries, the
DICOM database, and anything lock-heavy.

### 3. Data portal

Code:
[MorphoCloud/morphocloud-data-portal](https://github.com/MorphoCloud/morphocloud-data-portal).

- **Host:** its own always-on Jetstream2 VM, separate from the join VM, because
  it holds a key to every user's share. Nothing outside Jetstream2 can write to
  CephFS directly: the Manila API manages shares but never file contents.
- **Pieces:** Caddy (HTTPS) in front of a small web app (GitHub sign-in, team
  check, create and reset) and copyparty from the MorphoCloud/copyparty fork
  (the file browser at `/files`). Caddy asks the web app before every `/files`
  request; only then does it tell copyparty who the user is. The web app passes
  the numeric GitHub ID itself, so no third-party proxy decides identity.
- **Per-user folders:** copyparty's templated volumes, inside a parent volume
  nobody can access:

  ```
  [/${u}]
    /mnt/shares/${u}
    accs:
      rwmd: ${u}
  ```

- **Mounts:** one systemd mount unit per share at `/mnt/shares/<id>`. The bare
  mountpoint is immutable, so nothing can be written to local disk while a share
  is unmounted. A root worker checks mounts every minute, remounts if needed,
  and alerts.
- **Files are owned by uid/gid 1001** (exouser), as on instances, so files
  uploaded here stay editable on an instance.
- **copyparty's upload index** stays on local disk, never on the share.
- **Reset** empties the share after proving the path is that user's CephFS
  mount; it never crosses filesystems or follows symlinks, and recreates the
  three folders. It requires typing "reset my data". File access is blocked
  while it runs. Warning users away from resetting with an instance running is
  text only until instances mount shares (see "Phase 2 prototype" below).
- **Result:** a stable address that does not change on unshelve, unlike the
  per-instance Data drop link.

**Test deployment:** `mc-data-portal` on BIO240357_IU,
`https://mc-data-portal.bio240357.projects.jetstream-cloud.org` (Jetstream2's
automatic DNS name with a real certificate). The pickup runs from the
Test-Instances runner host.

### 4. Golden images

- **Two images**, built from one recipe that differs only in the driver:
  - **vGPU image:** baked on a g3.large, GRID driver. Used for g3.large.
  - **Regular-driver image:** baked on a passthrough flavor (g3.xl or g4.xl).
    Used for every other flavor, including CPU-only m3 and r3.
- **Contents:** everything ansible installs today, plus Slicer and the standard
  extensions with their Python dependencies.
- **Selection:** `create-instance` picks the image from the flavor. This is a
  fifth place a flavor is listed. The other four, which must stay in sync, are
  the flavor dropdowns in `ISSUE_TEMPLATE/01-individual-instance-request.yml`
  and `03-workshop-request.yml`, the `flavor:*` labels in `labels.yml`, and
  `app/js2_availability.py` in morphocloud-intake. The implementation adds this
  list to MAINTENANCE.md.

  | Flavor                  | GPU mode    | Image          |
  | ----------------------- | ----------- | -------------- |
  | g3.large                | vGPU (GRID) | vGPU image     |
  | g3.xl, g4.xl            | passthrough | regular-driver |
  | m3.\*, r3.\* (CPU-only) | none        | regular-driver |

  The two image names and the list of vGPU flavors live in repository variables,
  so a rebuilt image goes live by updating a variable, with no code change or
  vendorize. `create-instance` replaces its hard-coded `Featured-Ubuntu24` with
  this lookup.

- **Slicer launcher decides at launch time**, not bake time: use `vglrun` only
  when `nvidia-smi` sees a GPU, otherwise start Slicer directly with software
  rendering. Today ansible makes this decision once and bakes it into the
  launcher, which would be wrong on a CPU flavor booted from a GPU image.
- **Slicer upgrade procedure** becomes: rebuild both images.
- **OS updates (decisions 18 and 19, 2026-11-16).** Until now every `/create`
  ran cloud-init's full `apt-get upgrade` on the baked image. On Test-Instances
  from 2026-11-09 to 2026-11-16, with images from 2026-09-26, that stage took
  6.7 to 11.8 minutes on the creates that worked, because it installed a new
  kernel and rebuilt the NVIDIA driver module (on #568 the driver moved from 590
  to 595). 7 of 15 creates never left it within the 1200 s poll (#537, #541,
  #543, #544, #545, #547, #560). Now:
  - `/create` turns `package_upgrade` off when it boots a baked image
    (`.github/actions/create-instance`). The stock-image fallback, course
    instances and the bake keep the committed `package_upgrade: true`.
  - Amended the same day: `/create` also turns `package_update` off for a baked
    image. On #566 (vGPU image, created 2026-11-16 before the change) apt's
    history from first boot shows no install at all, only the upgrade: the setup
    script's `apt-get install python3-venv` and every ansible `apt` task
    (`state: present`) find the package already there, and the docker role's
    `apt_repository` (`update_cache: true`, which refreshes only when the task
    adds the repository) finds the repository already in the image (checked at
    exosphere ee60c28). The refresh cost 40 s and is the step that hung on #545.
    The background updates (decision 19) refresh the lists after setup, so a
    member's own `apt install` later gets current lists. Rule that follows: a
    package ansible installs must be in the image; a new one reaches instances
    through a rebake, and until then fails at create on stale lists.
  - Setup turns on `unattended-upgrades` after it completes (`cloud-config`, end
    of `90-exosphere-ansible-setup.sh`): `APT::Periodic` on, kernel (`linux-`)
    and NVIDIA (`nvidia-`, `libnvidia-`) packages excluded, no automatic reboot,
    no catch-up run, `Nice=10` and idle I/O. Amended 2026-11-17 after #574:
    - Sources: our config clears Ubuntu's default list and allows only
      `${distro_id}:${distro_codename}-security`. The default also listed the
      release pocket (never changes) and two Ubuntu Pro sources (inactive
      without Pro); `noble-updates` was never allowed. A security update is the
      same package version with the patch, never a feature update.
    - Timing: Ubuntu's timers add a random delay of up to 12 hours (refresh) and
      1 hour (upgrade), and it applies to the after-boot run too. On #574 the
      upgrade was due 77 minutes after boot and the refresh 5 hours after, so
      the upgrade would have used old lists. Now: refresh 30-40 minutes after
      boot, upgrade 45-55 minutes after boot; daily refresh 06:00 and 18:00 UTC,
      upgrade 06:30 UTC, each within 10 minutes. apt runs each step at most once
      per calendar day (it compares the date of a stamp file). Both current
      images have `APT::Periodic` off (checked on #566 and #568; ansible does
      not set it), so nothing runs during setup.
  - The bake stops the apt timers, waits for a run in progress to finish, and
    sets `APT::Periodic` back to off before the snapshot. Amended 2026-11-17: it
    also deletes apt's stamp files (`/var/lib/apt/periodic/*-stamp`). In
    `morphocloud-share-vgpu-20261116` they carried the build's date, so #574
    (created the same day) skipped its first upgrade at 04:27 UTC. The build's
    apt, dpkg and unattended-upgrades logs move to `/var/log/morphocloud-bake/`
    so they are not mistaken for the instance's own history.
  - Updating at shelve time was considered and rejected: the shelve's guest
    shutdown can cut off dpkg mid-install, every shelve (including auto-shelve)
    would take minutes longer, and a hung mirror would block the shelve.
- **No upgrade prompts on the desktop (decision 20, 2026-11-23).** Read from
  Ubuntu 24.04's `update-notifier` source (noble-updates); not yet seen on an
  instance, so test item 17 starts by checking the current image.
  - What prompts today: `update-notifier` starts 60 seconds after the desktop
    session starts (`/etc/xdg/autostart/update-notifier.desktop`). Each time
    apt's package lists change, it counts the pending updates and opens Software
    Updater if the last time Software Updater opened is more than
    `regular-auto-launch-interval` days ago (7). A new instance has no last
    time, so Software Updater opens after the first list refresh (30-40 minutes
    after boot, decision 19) and again every 7 days. Software Updater offers
    every pending update from `noble-updates` and `noble-security`, including
    the `linux-` and `nvidia-` packages decision 19 holds back. A member who
    clicks "Install Now" installs them, and after the next boot (an unshelve)
    the GPU is gone if the driver module was not rebuilt for the new kernel.
  - Already off, nothing to change: the new-release offer ("Ubuntu 26.04 is
    available"). The exosphere Slicer role sets `Prompt=never` in
    `/etc/update-manager/release-upgrades` (exosphere dec0592e). Both desktop
    triggers (`update-notifier` itself and the user unit
    `update-notifier-release.path`) run `check-new-release-gtk`, which reads
    that setting, and so does the SSH login message. A member who sees the offer
    anyway means `Prompt=never` is not taking effect: a bug to report.
  - Change: the exosphere Slicer role, when `gui_enabled`, writes
    `/home/exouser/.config/autostart/update-notifier.desktop` (owner exouser)
    with `Hidden=true`. In the autostart standard, a file in the user's
    autostart folder replaces the system file of the same name, and
    `Hidden=true` means it is not started. The role already puts
    `update-desktop-settings.desktop` in that folder, and it runs at every login
    (exosphere #13), so the folder is read in our VNC desktop session.
  - Not changed: the background security updates (root systemd timers, not part
    of the desktop session); Software Updater in the app menu; crash-report
    pop-ups (they come from the separate user unit
    `update-notifier-crash.path`); the SSH login message "N updates can be
    applied immediately" and its Ubuntu Pro lines (left out by the maintainer,
    2026-11-23).
  - To turn the check back on for one instance, delete that file. It is not
    listed in Startup Applications, because Ubuntu's entry has `NoDisplay=true`.
  - Rejected: removing the `update-notifier` package (`ubuntu-desktop-minimal`
    depends on it, so apt removes the meta-package too and marks the whole
    desktop for `apt autoremove`); editing Ubuntu's file in `/etc/xdg/autostart`
    (the package owns it); the setting `regular-auto-launch-interval` (0 or less
    means open every time; any value below about 20,800 days still opens at once
    on a new instance, whose last time is 0; above about 24,850 days the
    program's day-to-seconds multiplication overflows).
- **Slicer's runtime libraries (2026-11-23).** Slicer now keeps its own list of
  the Ubuntu packages it needs
  (`Utilities/Scripts/SlicerInstallers/deps/apt.txt`, added by Slicer#9294,
  merged 2026-11-03). One package on it is missing from ours
  (`ansible/roles/slicer/vars/Ubuntu.yml`): `libsm6`. It is added to our list.
  Switching from our vendored `slicer-download.sh` to Slicer's `install.sh` is a
  separate change, after the migration; Slicer stays in `/opt/slicer`, owned by
  exouser (maintainer, 2026-11-23).
- **Rollout of decision 20 and `libsm6`.** One exosphere PR (both changes), then
  one MWF PR that moves `exosphere_sha` in `cloud-config`. Order matters because
  of decision 18: a create from a baked image runs no `apt-get update`, so a
  package that ansible installs and the image lacks fails at create. The current
  images most likely have `libsm6` already (the desktop needs it), but that is
  not checked. So: bake both images from the MWF PR's `cloud-config`, point the
  Test image variables at them, run test item 17, and only then vendorize the
  new `cloud-config` to Test-Instances. Production gets both together at the
  migration.

**Build plan (prototype, Test-Instances, share mode only).** Started 2026-09-26.

- Exosphere, branch `share-prototype`: with `storage_mode=share`, Slicer
  installs to `/opt/slicer` (not `/media/volume/MyData`, which only exists as a
  volume mount point in volume mode), the launcher checks `nvidia-smi` at
  launch, and Slicer's default save folder is `~/Documents` (on the share).
  Volume mode is unchanged.
- `cloud-config` passes `storage_mode` and `slicer_install_dir` to ansible;
  `/create` fills them in.
- `/create` in share mode picks the image from repository variables:
  `MORPHOCLOUD_IMAGE_VGPU` for the flavors in `MORPHOCLOUD_VGPU_FLAVORS`
  (`g3.large`), `MORPHOCLOUD_IMAGE_REGULAR` for all others. An unset image falls
  back to `Featured-Ubuntu24`, as today.
- `scripts/bake-image.sh <flavor> <image name>`, run by an admin on the runner:
  boots the stock image with the same cloud-config, waits for setup to finish,
  cleans the instance (pitfalls below), shuts it down, snapshots it as a raw
  image, and deletes the build instance. Each new instance still runs ansible at
  first boot, which skips the Slicer download because it is already there.
- Each bake is a real instance for about 20 minutes plus a 60 GB image on the
  Test-Instances allocation. Bakes run only after approval.

Known bake pitfalls, from the first test image (2026-08-08):

1. **Disable `vncserver@1` during sysprep.** Enabled in an image, it starts
   before `docker0` exists, exhausts its restart limit, and aborts the playbook.
2. **Remove the Guacamole containers.** They carry the build instance's
   passphrase and restart with it before the playbook re-templates.
3. **Clear per-instance state:** exouser password, `.vnc/passwd`, VNC logs and
   pids, SSH host keys, machine-id, `authorized_keys`, and run
   `cloud-init clean --logs --seed`. Without the cloud-init reset, setup is
   skipped entirely.
4. **Scan extensions for Python dependencies beyond requirements files.** Some
   extensions call `pip_install("...")` at run time (Photogrammetry's
   ClusterPhotos).
5. **Keep `disk_format=raw`.** Jetstream2's image store is Ceph-backed, so raw
   boots by copy-on-write clone.

### 5. Workshops

- **Chosen:** no per-attendee share or volume. Workshop instances use one
  centralized workshop share. They are temporary by nature.
- The workshop share is a new dedicated share, not MorphoCloudCephShare (which
  holds the R libraries and is retired at adoption, since decision 12 ends the
  preinstalled R libraries). Attendees share read and write access. The
  organizer instructions state that workshop data must not be sensitive.
- **Fallback, if attendees need separate space:** one share sized 50 GB times
  the number of attendees, with folder N mapped to instance N. Assigning
  instances to attendees is the organizer's job. In this layout attendees could
  see each other's folders, which is acceptable for a workshop.

### 6. Lifecycle

- **Share lifecycle** drives the instance lifecycle: see "Storage term" below
  (decision 7, revised 2026-11-24).
- **`/migrate` (#305) becomes trivial:** the new instance mounts the same share.
  No detach or attach.
- **Instance expiry** no longer exists in share mode: an instance is deleted
  when its owner's storage locks, and deleting an instance never deletes files.
- **Issue commands and labels:**
  - The request issue tracks the instance only. In share mode a successful
    delete closes it (see "Delete in share mode" in the Phase 2 prototype).
  - `/delete_volume` is removed. `/delete_all` becomes "delete the instance".
  - The `volume:*` labels and the volume steps in the end-of-life sweep are
    removed.
  - `/renew` is turned off in share mode. More time is asked for the storage, on
    the portal ("Storage term" below), and an admin decides.
- **Setup steps removed:** the volume create and attach, the `exoVolumes`
  instance property, the `MyData-tmp` rename and Slicer copy, the home
  relocation with its VNC password and `.ssh` copies, the `.cache` redirect onto
  the volume, and the `.Renviron` step. The VNC password and `.ssh` stay on the
  root disk and are generated fresh on every create, as on any stock instance.
- **Workflow files with volume logic:**
  - Removed (done 2026-10-14): `delete-volume.yml`,
    `delete-volume-from-workflow.yml`, `automatic-volume-deleting.yml` and the
    `delete-volume` action; `/delete_volume` is no longer a command and
    `/delete_all` deletes the instance only. The crontab entry
    (`scripts/instances-runner.crontab`, `runner-cron-dispatcher.md`) went with
    them; the production runner's live crontab changes at the migration.
  - Rewritten: `delete-instance-and-volume.yml` (becomes instance-only),
    `close-expired-issues.yml` (volume steps and labels), `labels.yml` (volume
    labels).
  - Audited for volume references: `control-instance.yml`,
    `create-instance.yml`, `create-instance-from-workflow.yml`,
    `guard-issue-close.yml`, `report-dropped-command.yml`,
    `request-initial-comments.yml`, `send-renewal-email.yml`,
    `update-renew-label.yml`, `validate-command-instance.yml`,
    `workshop-backfill.yml`, `test-workshop-deletion.yml`.
  - Course workflows (`create-course-instance.yml`,
    `validate-command-course.yml`) are out of scope.
- Exosphere's UI no longer lists a MyData volume; shares are not volumes. User
  documentation should say where files now live.

### Storage term (decision 7, revised 2026-11-24)

Replaces the design of 2026-10-14, where a share was kept while it was used,
deleted after 180 days without use, and renewed by its member with **Keep my
storage** and no review. The aim now is tracking: the admins see who still uses
MorphoCloud, nobody is renewed without asking, and the share count stays within
the allocation (10,000 GB, about 88 member shares of 100 GB; see "Quota").

**Term.** A share is kept for 120 days from the day it was created: its end
date. The 44 shares the migration created on 2026-11-23 end on 2027-03-23. The
end date is the Manila property `mc_kept_until` (UTC date), written only by the
runner; a share without it counts from its creation time.

**Instances follow the storage.** In share mode an instance has no expiry of its
own: the request-issue ladder (`expiration:*` labels, renewal notices, `/renew`)
is turned off. An instance can be created only while its owner's storage is
kept, and it is deleted when the storage locks. The 4-hour session and automatic
shelving do not change.

**Reminders.** An email to the member 14 days, 7 days and 1 day before the end
date. The sweep runs daily at noon UTC, so "1 day" is its last run before the
end date. Each reminder gives the end date and links to the portal, where **Ask
to keep my storage** asks for more time.

**Asking for more time.** The member writes why (required, a short paragraph)
and sends it. The portal records the request and emails the admins (`ALERT_TO`)
with:

- who is asking, and why;
- the start date, the end date and the extensions so far;
- the last instance use, the last file change and the space used;
- one link to the request on the portal's admin page.

The link only opens the page. Nothing happens until an admin signed in with
GitHub, a member of `morphocloud-admins`, presses **Approve** or **Decline**
there. Mail scanners open the links in an email (Proofpoint did that to the
course-intake Approve links), so a link that acted by itself would approve every
request. No new token or GitHub app is needed: the portal's own sign-in and the
runner's existing SSH path carry the decision.

- **Approve** adds 120 days to the current end date, not to the day of approval.
  The next runner pickup writes it on the share, and the member gets an email
  with the new date.
- **Decline** keeps the end date. The member gets an email with the admin's
  note, if there is one.
- There is no limit on the number of extensions for now.
- **When a member can ask:** the button appears with the first reminder, 14 days
  before the end date, and stays while the share is locked, until it is deleted.
  Asking earlier would let a member stack extensions far ahead.
- **One request at a time.** While one is waiting, the button says so instead of
  offering another. After a Decline the member can ask again.
- **A waiting request postpones the lock.** If the end date passes while a
  request is waiting, the share stays kept, and its instance stays, until an
  admin decides. After a Decline the share locks at the next sweep, and the 15
  locked days start then.
- **Unlocking order.** After an approval of a locked share, the pickup first
  creates the access rule again (`ensure`), then writes the new end date. If
  `ensure` fails, the share stays locked with its old end date, the admins get
  an alert, and the next pickup tries again.

**After the end date, the share is locked for 15 days.** The share is kept
through its whole end date; the first sweep after that day (noon UTC the next
day) locks it, unless a request is waiting. The sweep deletes the member's
instance (with the clean shutdown every share-mode delete does) and closes its
request issue. The portal releases its mount, and the share's access rule is
revoked, so nothing can reach the files. The portal shows "Your storage is
locked since <date> and will be deleted on <date>" with **Ask to keep my
storage**. An approval while the share is locked unlocks it: the next pickup
creates the access rule again (`ensure`), the portal mounts the share again, and
the end date becomes the old end date plus 120 days.

**After 15 days locked, the share is deleted** by `expire`, as before (only the
sweep calls it), and the member gets a deletion email. Right before deleting,
the sweep asks the portal again whether a request is waiting or was just
approved; if so, it does not delete, so an approval on the last day cannot lose
to the delete. They can create a new, empty storage from the portal.

**Admin page** on the portal, for `morphocloud-admins` only:

- every share, with its owner, start and end dates, extensions (and who approved
  each), state (kept, locked or deleted), last instance use, last file change,
  space used, and whether an instance is running;
- the pending requests, with Approve and Decline.

Live usage views come later.

**Record.** The portal's audit table keeps every request, every decision (who,
when and the note), every lock and every deletion. The end date and the number
of extensions are also kept on the share as Manila properties (`mc_kept_until`,
`mc_extensions`), so the record survives a rebuild of the portal. If the two
ever disagree, the share's Manila properties win.

**Reused from the 2026-10-14 design:**

- the daily sweep (`expire-shares.yml`) and its read-only `sweep` verdicts;
- the reminder emails (PortalContent `storage-expiry.json`);
- the portal releasing its mount before a delete (`share-expiring`,
  `share-expired`);
- `expire` as the only delete.

The activity stamps (`mc_last_activity`, `mc_mounted_by`) stay, but only for the
admin page and the admin email; they no longer move the end date. **Keep my
storage**, which renewed with no review, is removed.

**Settings** (repository variables; unset means no sweep):

| Variable                         | Production |
| -------------------------------- | ---------- |
| `MORPHOCLOUD_SHARE_TERM_DAYS`    | `120`      |
| `MORPHOCLOUD_SHARE_WARNING_DAYS` | `14,7,1`   |
| `MORPHOCLOUD_SHARE_GRACE_DAYS`   | `15`       |

### Long-shelved instances (decision 21, 2026-11-24)

An instance must not stay shelved for ever: it holds a place in the allocation's
instance quota and an image-sized disk on Jetstream2, and nothing in it needs
keeping, because the member's files are on the storage. Once a week, the runner
checks every instance of the project:

- **Which:** an instance whose status is `SHELVED_OFFLOADED` and whose latest
  shelve, from Nova's action history (`openstack server event list`), started
  more than 28 days ago. Every shelve counts, by the member or automatic, and an
  unshelve starts the count again. If the history cannot be read, the instance
  is left for the next week; an unknown age never deletes.
- **What happens:** the instance is deleted the way every share-mode delete is,
  and its request issue is closed with a note. Nothing on the storage is
  touched. The member gets an email: the instance was removed after 4 weeks
  shelved, their files are safe in their storage, and they can create a new
  instance from the portal.
- **When:** weekly, dispatched by the runner crontab (the runner is the clock:
  INSTANCE_DASHBOARD_PLAN.md decision 21), not by GitHub's schedule.
- **Setting:** `MORPHOCLOUD_SHELVED_DELETE_DAYS` (production `28`); unset means
  no scrub.

## Quota

| Allocation                    | Share space | Used     | Share count | Manila per-share limit |
| ----------------------------- | ----------- | -------- | ----------- | ---------------------- |
| BIO180006_IU (production)     | 10,000 GB   | 5,500 GB | 125         | none                   |
| BIO240357_IU (Test-Instances) | 1,000 GB    | 100 GB   | 50          | none                   |

"Manila per-share limit" is the allocation's ceiling on how large any one share
may be created, and there is none: a share can be as large as the remaining
allocation total. Each user share is still capped at 100 GB, because that is the
size it is created with, and CephFS enforces a share's size as a hard limit.

Production as of 2026-11-24: 50 shares and 5,500 GB used, of which 44 are the
member shares the migration created (4,400 GB) and 6 are other shares (1,100
GB). Space binds, never the share count: the 4,500 GB left is 45 more member
shares, so 89 member shares in all (8,900 GB / 100 GB), well under 125. The
quota is soft and can be raised on request; the storage term (decision 7) is
there to show the turnover of members, not to hold a hard line. Testing fits the
Test-Instances allocation.

## Phase 2 prototype (instance side), Test-Instances only

Started 2026-09-25. The instance dashboard (INSTANCE_DASHBOARD_PLAN.md) waits
for this, because much of instance management depends on volumes versus shares.

**Switch.** Repository variable `MORPHOCLOUD_STORAGE_MODE=share`, set on
Test-Instances only. Unset (production, workshops, courses) keeps today's volume
path exactly.

**`/create` in share mode:**

1. No volume: the volume name, check, create and attach steps are skipped.
2. Before anything is created, `/create` looks up the owner's share
   (`mc_share_runner.py lookup <id>`, read-only, issue creator's numeric ID). No
   share: it refuses with a link to the portal. A failed lookup stops `/create`
   too; it is never read as "no share". After the instance is set up, the runner
   reads the share's details with `ensure --existing`, which never creates a
   share. The access key is masked in the logs.
3. Over SSH, as root on the instance:
   - key in `/etc/ceph/mc-user.secret` (root only);
   - a systemd mount unit for `/media/share/MyDrive`; the bare mountpoint is
     immutable;
   - the three folders on the share if missing, owned by exouser;
   - launchers copied into the share's Desktop;
   - each home folder replaced by a link to the share. A local folder that is
     not empty is moved aside to `~/<name>.local-<date>`, never deleted;
   - `vncserver@1` requires the mount (no desktop without the share);
   - `xdg-user-dirs` updates turned off.
4. Skipped: the MyData rename, Slicer copy (Slicer stays on the root disk where
   ansible installs it), home relocation, and `.Renviron`.

**Delete in share mode.** Every delete of an instance (`/delete_instance`,
`/delete_all`, expiry) first shuts it down cleanly and waits up to 3 minutes, so
everything written to the share is flushed. If it does not stop in time, it is
deleted anyway and the issue says that recent unflushed changes may be lost.
After a successful delete of a user's own request (`request-creator:user`), the
workflow **closes the request issue**: a request covers one instance's lifetime
(decision 15), and the next instance starts from a new request. Found by test 4
on 2026-09-26: a file saved about 10 seconds before `/delete_instance` came back
empty; a hard reset confirmed that unflushed writes are lost.

A request its owner closes by hand (GitHub still allows it) is deleted the same
way by `guard-issue-close.yml` (2026-10-14): shelving it, as volume mode does,
would leave an instance the portal no longer shows while the owner can create a
second one onto the same storage, against decision 17. The comment says the
files are in the storage and where to create the next instance.

**Instance lifetime on Test-Instances:** 90 days, and `/renew` adds 90 (180 in
total), set by the repository variable `MORPHOCLOUD_EXPIRATION_LADDER`
(`90d,180d`). Unset keeps today's 60 + 60. Since 2026-10-14 the prototype runs
`7d,14d`, and shares expire after 14 idle days (the 2026-10-14 design, since
replaced by "Storage term"; Test-Instances keeps that rule until the storage
term is built and tested there), so the whole lifecycle can be watched within
the test period.

**No Data drop in share mode:** the credentials email has no Data drop line and
new instances do not install it; the portal's file browser replaces it.
Production (volume mode) keeps Data drop for now (decision 10).

**No Guacamole file transfer in share mode:** the web desktop and web shell have
no upload/download panel (`enable-sftp` off); the portal's file browser replaces
it. Decided by the maintainer 2026-09-26. Production (volume mode) keeps it.

**Guacamole 1.6.0, desktop sized to the browser window:** the web desktop
resizes with the browser window, and text stays at the local OS size on Retina
and scaled displays (the Exosphere Guacamole extension sizes the desktop in CSS
pixels). Images come from Docker Hub (`guacamole/guacd:1.6.0`,
`guacamole/guacamole:1.6.0`). Clipboard syncs natively in Chrome/Edge; Firefox
and Safari still need the side panel. Decided by the maintainer 2026-09-27, for
the prototype only; production keeps 1.5.5 until it moves to the prototype.

**Desktop launchers start without "Allow Launching":** setup marks the launchers
on the share trusted for exouser (`gio set … metadata::trusted true`) before the
first desktop session. The login-time autostart that also does this can run
after GNOME has drawn the icons, which then show as untrusted (seen on #452).

**No Guacamole side panel in share mode:** the Ctrl+Alt+Shift menu (and the
touch swipe that opens it) is switched off, and files dropped onto the web
desktop are ignored instead of starting an upload that cannot finish. A second
Guacamole extension, installed only when `storage_mode=share`, keeps the menu
closed. Cost: in Firefox and Safari there is no way to paste into or copy out of
the web desktop (Chrome and Edge sync the clipboard natively); TurboVNC is the
alternative there. Decided by the maintainer 2026-09-27. Production (volume
mode) keeps the panel.

**Test-Instances settings for the prototype** (repository variables; unset means
today's behaviour):

| Variable                             | Value                                                           |
| ------------------------------------ | --------------------------------------------------------------- |
| `MORPHOCLOUD_STORAGE_MODE`           | `share`                                                         |
| `MORPHOCLOUD_IMAGE_VGPU`             | `morphocloud-share-vgpu-20260926`                               |
| `MORPHOCLOUD_IMAGE_REGULAR`          | `morphocloud-share-regular-20260926`                            |
| `MORPHOCLOUD_VGPU_FLAVORS`           | `g3.large`                                                      |
| `MORPHOCLOUD_EXPIRATION_LADDER`      | `90d,180d`                                                      |
| `MORPHOCLOUD_MAX_INSTANCES_PER_USER` | `1`                                                             |
| `MORPHOCLOUD_DATA_PORTAL_URL`        | `https://mc-data-portal.bio240357.projects.jetstream-cloud.org` |
| `MORPHOCLOUD_PORTAL_ONLY`            | `true` (INSTANCE_DASHBOARD_PLAN.md decision 18)                 |
| `MORPHOCLOUD_PORTAL_APP_SLUG`        | `morphocloud-portal-test`                                       |

The data portal's own settings (`/etc/mc-data-portal/portal.env` on its VM),
beyond those `deploy/provision.sh` writes: `MC_UNAVAILABLE_FLAVORS=g4.xl,r3.*`
(instance types greyed out during the evaluation). The Help card needs none; its
defaults are in CHATBOT_PLAN.md in the data portal repo.

The runner host also needs `~/mc-data/` from `runner/install-runner.sh` in the
data portal repo (share pickup, `lookup`/`ensure`, `portal_target`).

**Known gap:** if Ceph is unreachable at boot, the mount is not retried when it
comes back; the desktop stays down until the next reboot or unshelve (test 8).

**Not in the prototype** (later, if adopted): the failure message shown in place
of the desktop (the desktop simply does not start), Slicer's DICOM folder, share
metadata bookkeeping, removing volume commands, workshops. Golden images are in
the prototype (section 4, "Build plan").

## Test plan (Test-Instances, BIO240357_IU)

Run before building anything permanent.

1. Create one share and mount it on a test instance with the links above.
   **Passed 2026-09-26** (Test-Instances#439).
2. Normal desktop session: files on the Desktop, a Firefox download, Slicer
   saving a scene to the Desktop. Watch for lock-related errors or eviction.
3. Reboot and unshelve: the share mounts before the session and the links hold.
   **Passed 2026-09-26** (mount one second before the desktop).
4. Delete the instance and create a new one: the same Desktop reappears. The
   Desktop came back, but a file saved seconds before the delete was empty:
   fixed by the clean shutdown ("Delete in share mode"); retest pending.
5. Load a large microCT volume from the share into Slicer; compare with root
   disk load time.
6. Boot the regular-driver image on an m3 flavor; confirm Slicer starts with
   software rendering.
7. Stand up the central service against the test share; confirm a second GitHub
   user cannot see or reach the first user's files. **Passed 2026-09-26**
   (including URL-encoded and `../` paths).
8. Break the mount (wrong key, then Ceph unreachable): the desktop session must
   not start, and no local Desktop folder may appear. **Passed 2026-09-26** (no
   retry once Ceph is back: known gap).
9. Upload through the central service while the instance writes to the same
   share; check both files arrive intact. **Passed 2026-09-26** (400 MB each
   side, checksums match).
10. Fill the share past 100 GB: a clear error both on the instance and in the
    browser. **Passed 2026-09-26** ("Disk quota exceeded" on the instance and on
    the portal host; the browser's own message not checked).
11. Portal create: a share is created once; a repeated or retried request reuses
    it and never makes a second one. **Passed 2026-09-26** (with test IDs, since
    deleted).
12. Share reuse across request issues: create an instance from one issue, save a
    file to the Desktop, `/delete_all`, open a new issue, `/create`; the same
    Desktop reappears. Also `/create` for an account with no share yet: it
    refuses with a link to the portal and creates nothing. **The refusal passed
    2026-09-26** (as amm554, Test-Instances#443).
13. Rotate the key of a share mounted on a running instance: record whether the
    active mount survives the denied rule, and confirm the instance mounts with
    the new key after a reboot.
14. Confirm the name passed to copyparty is the numeric GitHub ID, not the
    login. **Passed 2026-09-26.**
15. Reset: wrong phrase and bad form token refused; a real reset empties only
    the user's own share; a reset while the share is unmounted is refused,
    alerts the admins, and the worker remounts the share with data intact.
    **Passed 2026-09-26.**
16. OS updates (decisions 18 and 19). As amm554 through the portal, `/create`
    one instance per image (a g3.medium and an m3.xl). Each run log says
    "skipping the first-boot package upgrade"; the time at `starting` is under 2
    minutes; setup is `complete`; `nvidia-smi` sees the GPU on the g3. On the
    instance: `APT::Periodic` is `1` in `20auto-upgrades`, the blacklist file is
    present, and `systemctl list-timers apt-daily*` shows the refresh 30-40 and
    the upgrade 45-55 minutes after boot. After an hour,
    `/var/log/unattended-upgrades/unattended-upgrades.log` shows a completed run
    and no `linux-`, `nvidia-` or `libnvidia-` package in
    `/var/log/apt/history.log` since boot. Shelve, unshelve: `nvidia-smi` still
    works. Then one bake (after approval): the image has `APT::Periodic` set to
    `0` and no files in `/var/lib/apt/periodic/`. An image test, not a
    permissions test, so it may run under any account (user, 2026-11-17). **vGPU
    image passed 2026-11-17** (`morphocloud-share-vgpu-20261116`, baked on
    g3.medium; #574, g3.medium): `starting` 38 s, `complete` 159 s, cloud-init's
    package stage 0.001 s, no apt install at create, GPU (GRID A100X-10C,
    580.159.03) before and after shelve/unshelve. Two findings, fixed by the
    2026-11-17 amendment: the upgrade at 04:27 UTC skipped because of the
    image's stamp files, and the timers' random delays. Still open: the regular
    image (g3.xl bake not yet placed), and the timer and stamp checks on the
    next image.
17. No upgrade prompts and `libsm6` (decision 20). First, on an instance from
    the current image if one is running: open the desktop, wait 2 minutes, and
    record whether `pgrep -u exouser -x update-notifier` finds it (it should).
    Then on the build instance of each bake, and on one instance created from
    each new image: `/home/exouser/.config/autostart/update-notifier.desktop`
    exists with `Hidden=true`; `dpkg -s libsm6` says installed;
    `/etc/update-manager/release-upgrades` says `Prompt=never`. On the created
    instance, open the desktop through Guacamole and keep it open past the first
    list refresh (about 40 minutes after boot):
    `pgrep -u exouser -x update-notifier` and `pgrep -f update-manager` find
    nothing, no Software Updater window appeared, and test item 16's timer
    checks still pass (`systemctl list-timers apt-daily*`). Software Updater
    still opens from the app menu.

## Out of scope for now

- Migrating existing users from MyData.
- Backups of shares.
- Removing Data drop.
- Course repositories (separate allocations and pinned exosphere branches).
