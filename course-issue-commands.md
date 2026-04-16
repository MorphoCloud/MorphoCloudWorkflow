### Course Repository Configuration

The following repository variables can be set under **Settings → Secrets and
variables → Actions → Variables** to customize course behavior.

| Variable                        | Default | Description                                                                                                                                                                                                                                                                                                                |
| ------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `INSTANCE_SHELVING_TIMEOUT_HRS` | `4`     | Number of hours a student instance may run before being automatically shelved. Increase this value (e.g. to `8`) if students need longer uninterrupted sessions. The per-instance popup reminder (shown ~30 min before shelving) allows individual students to extend their session by 4 hours regardless of this setting. |

---

### Supported Issue Commands

_The commands described below may be added as issue comments. Only one command
may be entered per comment._

| Command            | Description                                        | Who                  |
| ------------------ | -------------------------------------------------- | -------------------- |
| `/shelve`          | Shelve the instance.                               | Issue creator, Admin |
| `/unshelve`        | Unshelve the instance.                             | Issue creator, Admin |
| `/email`           | Send email to _Issue creator_ with connection URL. | Issue creator, Admin |
| `/create`          | Create the instance and associated volume.         | Issue creator, Admin |
| `/delete_instance` | Delete the instance.                               | Issue creator, Admin |
| `/delete_volume`   | Delete the volume.                                 | Issue creator, Admin |
| `/delete_all`      | Delete the instance and volume.                    | Issue creator, Admin |
