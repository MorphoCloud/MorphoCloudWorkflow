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

| #   | Decision                                                                                                                                                                                                       |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Each user gets their own 100 GB Manila CephFS share. It replaces the per-instance MyData Cinder volume.                                                                                                        |
| 2   | Shares are keyed on the **GitHub numeric user ID**, not the login. Logins can be renamed or reclaimed; the ID never changes.                                                                                   |
| 3   | The home directory and all settings stay on the instance's root disk. Only the user-facing folders live on the share.                                                                                          |
| 4   | A small always-on service provides browser upload and download to each user's share, with GitHub sign-in. It works with or without an instance.                                                                |
| 5   | Two golden images, both with the NVIDIA driver: a vGPU (GRID) image for g3.large, and a regular-driver image for every other flavor, including CPU-only.                                                       |
| 6   | Slicer and the standard extension set are baked into the images. Extensions a user adds are lost when the instance is recreated. Accepted.                                                                     |
| 7   | Share lifecycle target is 6 months, renewable. **Start with no expiry** and measure how much accumulates first.                                                                                                |
| 8   | No backups. Same as MyData volumes today.                                                                                                                                                                      |
| 9   | No migration path from MyData during testing. Designed only if this is adopted.                                                                                                                                |
| 10  | The current per-instance upload page ("Data drop") stays in production until this design is adopted.                                                                                                           |
| 11  | Workshop instances get no per-attendee share or volume. They use one centralized workshop share.                                                                                                               |
| 12  | R libraries live on the root disk. MorphoCloud no longer installs them.                                                                                                                                        |
| 13  | Slicer's DICOM database moves to local disk. DICOM is not a common use case.                                                                                                                                   |
| 14  | A share is provisioned for every member of the MorphoCloudUsers team. Joining the team is signing up. A scheduled reconciliation creates missing shares and sends the welcome email with storage instructions. |
| 15  | The request issue's lifecycle covers the instance only. Volume commands and `volume:*` labels are removed; the share has its own lifecycle.                                                                    |

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
  GitHub login at creation and when the welcome email was sent. No new database
  or sheet column.

**Provisioning: reconcile the MorphoCloudUsers team.** Membership in the team is
the signup record, and GitHub's team-members API returns each member's numeric
ID. So provisioning needs nothing from the intake app:

1. A `reconcile-user-shares` workflow runs on the control runner, which already
   holds the OpenStack credentials, every few minutes (scheduled, or from the
   runner's crontab as the end-of-life sweep is) and on manual dispatch.
2. It lists the team's members and the existing `mc-user-*` shares.
3. For each member without a share: create the 100 GB share and its access rule,
   wait until available, look up the member's email through the existing
   `/lookup` endpoint, and send the welcome email with storage instructions and
   the upload service address. Record the send in the share metadata.
4. It is idempotent: an existing share is never recreated, and a recorded
   welcome is never resent. A failure on one member does not stop the others.
5. Any failure emails the admins. A member left without a share is retried on
   the next run.
6. Members who have left the team are reported, not deleted (no expiry yet).

At adoption, the join app's welcome sweep is retired so users get one welcome
email, sent only once their share exists. Existing members are backfilled by the
first run: 73 team members today, so about 7.3 TB provisioned at once.

During testing, the workflow runs against the Test-Instances allocation and only
for an allowlist of test accounts. The production join app and its welcome email
are unchanged.

**Keys.** Manila stores each share's access key; there is no separate key store.
At create and unshelve the runner reads the key from Manila and writes it to a
root-only file on the instance, which the mount uses after reboots. The user can
read it with sudo; it grants only their own share. **Rotation** for a
compromised key: deny the share's access rule, grant a new one, and redeliver
the key with the next create or unshelve. The data is untouched.

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
  that disappears with the instance.
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

### 3. Central upload and download service

- **Host:** an always-on Jetstream2 VM (the join VM or a dedicated one). Nothing
  outside Jetstream2 can write to CephFS directly: the Manila API manages shares
  but never file contents, and CephFS is served only on Jetstream2's internal
  network.
- **Software:** copyparty from the MorphoCloud/copyparty fork, the same build as
  Data drop.
- **Sign-in:** GitHub through an identity-provider proxy (oauth2-proxy) that
  passes the verified user to copyparty's header authentication.
- **Per-user folders:** copyparty's templated volumes, one per signed-in user,
  pointing at that user's mounted share:

  ```
  [/u/${u}]
    /mnt/shares/${u}
    accs:
      rwmd: ${u}
  ```

  The username passed by the proxy must be the numeric GitHub ID, to match
  decision 2.

- **Sensitivity:** this VM holds every user's key. Harden it like the join
  server.
- **Result:** a stable address that does not change on unshelve, unlike the
  per-instance Data drop link.

### 4. Golden images

- **Two images**, built from one recipe that differs only in the driver:
  - **vGPU image:** baked on a g3.large, GRID driver. Used for g3.large.
  - **Regular-driver image:** baked on a passthrough flavor (g3.xl or g4.xl).
    Used for every other flavor, including CPU-only m3 and r3.
- **Contents:** everything ansible installs today, plus Slicer and the standard
  extensions with their Python dependencies.
- **Selection:** `create-instance` picks the image from the flavor. This is a
  fifth place a flavor is listed (see the flavor sync note in MAINTENANCE.md).

  | Flavor                  | GPU mode    | Image          |
  | ----------------------- | ----------- | -------------- |
  | g3.large                | vGPU (GRID) | vGPU image     |
  | g3.xl, g4.xl            | passthrough | regular-driver |
  | m3.\*, r3.\* (CPU-only) | none        | regular-driver |

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
  holds the R libraries). Attendees share read and write access. The organizer
  instructions state that workshop data must not be sensitive.
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
- **Setup steps removed:** the volume create and attach, the `MyData-tmp` rename
  and Slicer copy, the home relocation with its VNC password and `.ssh` copies,
  the `.cache` redirect onto the volume, and the `.Renviron` step.
- Exosphere's UI no longer lists a MyData volume; shares are not volumes. User
  documentation should say where files now live.

## Quota

| Allocation                    | Share space | Used   | Share count | Per-share cap |
| ----------------------------- | ----------- | ------ | ----------- | ------------- |
| BIO180006_IU (production)     | 2,000 GB    | 900 GB | 50          | none          |
| BIO240357_IU (Test-Instances) | 1,000 GB    | 100 GB | 50          | none          |

Ceph and Manila impose no per-share size limit; a share can use whatever the
allocation total allows. A production rollout needs a higher share count and
total. That increase has been requested. Testing fits the Test-Instances
allocation.

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
11. Run the reconciliation for an allowlisted test account: share created,
    welcome sent once, and a second run changes nothing.

## Out of scope for now

- Migrating existing users from MyData.
- Backups of shares.
- Expiry enforcement on shares.
- Removing Data drop.
- Course repositories (separate allocations and pinned exosphere branches).
