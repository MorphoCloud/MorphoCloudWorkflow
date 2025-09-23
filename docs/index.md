---
title: MorphoCloud: On-Demand Cloud for 3D Slicer & SlicerMorph
---

<p align="center">
  <img src="https://raw.githubusercontent.com/MorphoCloud/MorphoCloudInstances/main/MC_Logo.png" alt="MorphoCloud Logo" width="280">
</p>

# MorphoCloud: On-Demand Cloud for 3D Slicer & SlicerMorph

MorphoCloud provides **on-demand cloud instances** and **reusable GitHub
workflows** to support computational morphology, 3D morphometrics, and
biomedical imaging research.

It allows researchers and educators to launch powerful
[JetStream2](https://jetstream-cloud.org/) virtual machines — preloaded with
[3D Slicer](https://download.slicer.org) and
[SlicerMorph](https://SlicerMorph.org) — by simply opening and commenting on
GitHub issues.

## ✨ What You Can Do with MorphoCloud

- **Run Slicer in your browser**: GPU-enabled remote desktops with up to 40GB
  GPUs and 1TB RAM.
- **Request and manage instances with GitHub Issues**: no portals, no manual
  provisioning.
- **Automated lifecycle management**: Create, shelve, unshelve, renew, or delete
  instances via simple `/commands`.
- **Integrate workflows into your project**: Use
  [MorphoCloudWorkflow](https://github.com/MorphoCloud/MorphoCloudWorkflow) to
  vendorize and customize provisioning logic for your community.
- **Support research and teaching**: Ideal for occasional high-performance
  computing needs in morphology and imaging.

## 🖥️ Available Instance Types

| Flavor | RAM    | Cores | GPU         | Storage |
| ------ | ------ | ----- | ----------- | ------- |
| g3.l\* | 60GB   | 16    | A100 (20GB) | 100GB   |
| g3.xl  | 125GB  | 32    | A100 (40GB) | 100GB   |
| m3.x   | 250GB  | 64    | None        | 100GB   |
| r3.l   | 500GB  | 64    | None        | 100GB   |
| r3.xl  | 1000GB | 128   | None        | 100GB   |

\*`g3.l` is the default instance.<br>
[Check real-time JetStream2 resource availability →](https://docs.jetstream-cloud.org/overview/status/#availability-of-scarce-resources)

## 🔑 Commands

Once approved, you can control your instance from the issue itself:

| Command            | Description                 |
| ------------------ | --------------------------- |
| `/create`          | Provision a new instance    |
| `/shelve`          | Suspend (turn off) instance |
| `/unshelve`        | Resume shelved instance     |
| `/delete_instance` | Delete an instance          |
| `/delete_volume`   | Remove associated storage   |
| `/renew`           | Extend lifespan by 4 hours  |
| `/email`           | Resend access email         |

See the full
[command reference](https://github.com/MorphoCloud/MorphoCloudWorkflow/blob/main/issue-commands.md).

## 🚀 Quick Start

- **For Researchers / Educators**: 👉 Request an instance via
  [MorphoCloudInstances issue templates](https://github.com/MorphoCloud/MorphoCloudInstances/issues/new/choose).
  Approval typically takes <24h.

- **For Project Maintainers / Admins** 👉 Integrate workflows from
  [MorphoCloudWorkflow](https://github.com/MorphoCloud/MorphoCloudWorkflow) into
  your GitHub projects. See
  [Requirements](https://github.com/MorphoCloud/MorphoCloudWorkflow#requirements)
  and
  [Runner setup](https://github.com/MorphoCloud/MorphoCloudWorkflow#setting-up-a-morphocloud-github-runner).

## 🖼️ Desktop Environment

<p align="center">
  <img src="https://github.com/MorphoCloud/MorphoCloudInstances/blob/main/MCI_Desktop.png" width="650">
</p>

- **Side toolbar** (Ctrl/Cmd + Alt + Shift) for file transfers & clipboard.
- **Shortcuts** for Slicer, SlicerMorph, and MyData storage.
- **Right-click menu** to adjust resolution and display settings.
- **Extend session** with one click (+4 hours).

## 🙏 Funding & Acknowledgments

MorphoCloud services, including MorphoCloud On-Demand Instances, are supported
by funding from the National Science Foundation (DBI/2301405) and National
Institutes of Health (NICHD/HD104435). MorphoCloud runs on cyberinfrastructure
that is made available by current and previous funding from the National Science
Foundation (JetStream2: OAC/2005506, Exosphere: TI/2229642). Initial development
of SlicerMorph was previously supported by National Science Foundation
(DBI/1759883).

If you use any of the MorphoCloud services for your project, please acknowledge
our funders with this statement:

> “This study relied on cyberinfrastructure supported by grants from National
> Science Foundation (MorphoCloud: DBI/2301405; JetStream2: OAC/2005506;
> Exosphere: TI/2229642) and National Institutes of Health (MorphoCloud: NICHD
> HD104435).”
