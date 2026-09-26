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

| #   | Decision                                                                                                                                                                                                                  |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Each user gets their own 100 GB Manila CephFS share. It replaces the per-instance MyData Cinder volume.                                                                                                                   |
| 2   | Shares are keyed on the **GitHub numeric user ID**, not the login. Logins can be renamed or reclaimed; the ID never changes.                                                                                              |
| 3   | The home directory and all settings stay on the instance's root disk. Only the user-facing folders live on the share.                                                                                                     |
| 4   | A data portal on its own VM provides browser upload and download to each user's share, with GitHub sign-in (members of MorphoCloudUsers only). It works with or without an instance.                                      |
| 5   | Two golden images, both with the NVIDIA driver: a vGPU (GRID) image for g3.large, and a regular-driver image for every other flavor, including CPU-only.                                                                  |
| 6   | Slicer and the standard extension set are baked into the images. Extensions a user adds are lost when the instance is recreated. Accepted.                                                                                |
| 7   | Share lifecycle target is 6 months, renewable. **Start with no expiry** and measure how much accumulates first.                                                                                                           |
| 8   | No backups. Same as MyData volumes today.                                                                                                                                                                                 |
| 9   | No migration path from MyData during testing. Designed only if this is adopted.                                                                                                                                           |
| 10  | The current per-instance upload page ("Data drop") stays in production until this design is adopted.                                                                                                                      |
| 11  | Workshop instances get no per-attendee share or volume. They use one centralized workshop share.                                                                                                                          |
| 12  | R libraries live on the root disk. MorphoCloud no longer installs them.                                                                                                                                                   |
| 13  | Slicer's DICOM database moves to local disk. DICOM is not a common use case.                                                                                                                                              |
| 14  | Shares are created on demand: a user presses "Create my storage" on the portal, or runs `/create` without one, which creates it automatically. No automatic provisioning of every member.                                 |
| 16  | Reset empties the share (share and key are kept). Nothing ever deletes or recreates a user's share automatically.                                                                                                         |
| 15  | The request issue's lifecycle covers the instance only. Volume commands and `volume:*` labels are removed; the share has its own lifecycle.                                                                               |
| 17  | **One instance per user.** Two instances would mount the same share at once. Enforced with the existing per-user limit (`MORPHOCLOUD_MAX_INSTANCES_PER_USER=1`) when shares are adopted; the data portal offers only one. |

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
2. **`/create` without a share** (decision A) creates it the same way, inline,
   so `/create` never fails over storage. Not built yet (phase 2).

Both call the same share-creation logic (`ensure`), which **fails closed**:

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
├── Downloads/
└── Uploads/
```

**Links in the home directory** (home stays on the root disk):

```
/home/exouser/Desktop   -> /media/share/MyDrive/Desktop
/home/exouser/Documents -> /media/share/MyDrive/Documents
/home/exouser/Downloads -> /media/share/MyDrive/Downloads
/home/exouser/Uploads   -> /media/share/MyDrive/Uploads
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
  four folders. It requires typing "reset my data". File access is blocked while
  it runs. Warning users away from resetting with an instance running is text
  only until instances mount shares (phase 2).
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

- **Share lifecycle** is separate from instance lifecycle. Target 6 months,
  renewable, but no expiry at first. Measure usage before choosing the rule.
- **`/migrate` (#305) becomes trivial:** the new instance mounts the same share.
  No detach or attach.
- **Instance expiry** no longer deletes user files; deleting the share becomes
  its own decision.
- **Issue commands and labels:**
  - The request issue tracks the instance only.
  - `/delete_volume` is removed. `/delete_all` becomes "delete the instance".
  - The `volume:*` labels and the volume steps in the end-of-life sweep are
    removed.
  - `/renew` extends the instance, as today. Share renewal is designed with the
    share expiry rule, later.
- **Setup steps removed:** the volume create and attach, the `exoVolumes`
  instance property, the `MyData-tmp` rename and Slicer copy, the home
  relocation with its VNC password and `.ssh` copies, the `.cache` redirect onto
  the volume, and the `.Renviron` step. The VNC password and `.ssh` stay on the
  root disk and are generated fresh on every create, as on any stock instance.
- **Workflow files with volume logic:**
  - Removed: `delete-volume.yml`, `delete-volume-from-workflow.yml`,
    `automatic-volume-deleting.yml`. The last one is live today: the runner
    crontab (`scripts/instances-runner.crontab`, documented in
    `runner-cron-dispatcher.md`) dispatches it daily, so its crontab entry and
    that documentation are removed with it.
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

## Quota

| Allocation                    | Share space | Used   | Share count | Manila per-share limit |
| ----------------------------- | ----------- | ------ | ----------- | ---------------------- |
| BIO180006_IU (production)     | 2,000 GB    | 900 GB | 50          | none                   |
| BIO240357_IU (Test-Instances) | 1,000 GB    | 100 GB | 50          | none                   |

"Manila per-share limit" is the allocation's ceiling on how large any one share
may be created, and there is none: a share can be as large as the remaining
allocation total. Each user share is still capped at 100 GB, because that is the
size it is created with, and CephFS enforces a share's size as a hard limit.

A production rollout needs a higher share count and total. With on-demand
creation, usage grows with the members who ask for storage rather than all at
once, but 73 team members at 100 GB each is still 7,300 GB against today's 50
shares and roughly 1,100 GB free. The increase has been requested; **do not open
the portal in production until it lands**, or requests past the quota fail with
an admin alert instead of a share. Size the request for growth. Testing fits the
Test-Instances allocation.

## Phase 2 prototype (instance side), Test-Instances only

Started 2026-09-25. The instance dashboard (INSTANCE_DASHBOARD_PLAN.md) waits
for this, because much of instance management depends on volumes versus shares.

**Switch.** Repository variable `MORPHOCLOUD_STORAGE_MODE=share`, set on
Test-Instances only. Unset (production, workshops, courses) keeps today's volume
path exactly.

**`/create` in share mode:**

1. No volume: the volume name, check, create and attach steps are skipped.
2. After the instance is set up, the runner calls the same fail-closed `ensure`
   as the portal (`mc_share_runner.py ensure <id> <login>`, issue creator's
   numeric ID), under the same lock as the portal pickup so the two never create
   at once. The access key is masked in the logs.
3. Over SSH, as root on the instance:
   - key in `/etc/ceph/mc-user.secret` (root only);
   - a systemd mount unit for `/media/share/MyDrive`; the bare mountpoint is
     immutable;
   - the four folders on the share if missing, owned by exouser;
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
deleted anyway and the issue says that changes from the last few seconds may be
lost. Found by test 4 on 2026-09-26: a file saved about 10 seconds before
`/delete_instance` came back empty; a hard reset confirmed that unflushed writes
are lost.

**Known gap:** if Ceph is unreachable at boot, the mount is not retried when it
comes back; the desktop stays down until the next reboot or unshelve (test 8).

**Not in the prototype** (later, if adopted): golden images (tests 5 and 6 use
today's image), the failure message shown in place of the desktop (the desktop
simply does not start), Slicer's DICOM folder, share metadata bookkeeping,
removing volume commands, workshops. A share created by `/create` is not yet
known to the portal; the user's "Create my storage" then reuses it.

## Test plan (Test-Instances, BIO240357_IU)

Run before building anything permanent.

1. Create one share and mount it on a test instance with the links above.
2. Normal desktop session: files on the Desktop, a Firefox download, Slicer
   saving a scene to the Desktop. Watch for lock-related errors or eviction.
3. Reboot and unshelve: the share mounts before the session and the links hold.
4. Delete the instance and create a new one: the same Desktop reappears.
5. Load a large microCT volume from the share into Slicer; compare with root
   disk load time.
6. Boot the regular-driver image on an m3 flavor; confirm Slicer starts with
   software rendering.
7. Stand up the central service against the test share; confirm a second GitHub
   user cannot see or reach the first user's files.
8. Break the mount (wrong key, then Ceph unreachable): the desktop session must
   not start, and no local Desktop folder may appear.
9. Upload through the central service while the instance writes to the same
   share; check both files arrive intact.
10. Fill the share past 100 GB: a clear error both on the instance and in the
    browser.
11. Portal create: a share is created once; a repeated or retried request reuses
    it and never makes a second one. **Passed 2026-09-26** (with test IDs, since
    deleted).
12. Share reuse across request issues: create an instance from one issue, save a
    file to the Desktop, `/delete_all`, open a new issue, `/create`; the same
    Desktop reappears. Also `/create` for an account with no share yet: the
    share is created inline.
13. Rotate the key of a share mounted on a running instance: record whether the
    active mount survives the denied rule, and confirm the instance mounts with
    the new key after a reboot.
14. Confirm the name passed to copyparty is the numeric GitHub ID, not the
    login. **Passed 2026-09-26.**
15. Reset: wrong phrase and bad form token refused; a real reset empties only
    the user's own share; a reset while the share is unmounted is refused,
    alerts the admins, and the worker remounts the share with data intact.
    **Passed 2026-09-26.**

Test 7 (isolation between two users, including URL-encoded and `../` paths) also
**passed 2026-09-26**.

## Out of scope for now

- Migrating existing users from MyData.
- Backups of shares.
- Expiry enforcement on shares.
- Removing Data drop.
- Course repositories (separate allocations and pinned exosphere branches).
