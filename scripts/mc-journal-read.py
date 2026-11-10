#!/usr/bin/env python3
"""Read a host's system journal for the incident harvester, and nothing else.

INCIDENT_STORE_PHASE1_DESIGN.md, "Coverage amendment", class I. Installed on
every MorphoCloud server VM by install-journal-reader.sh as the forced command
of the read-only `mcjournal` account (`restrict,command=...` in its
authorized_keys); on the acquire runner the harvester runs it locally. The
request comes in SSH_ORIGINAL_COMMAND, one form only:

    journal <cursor | -> <unit-glob[,unit-glob...]>

Output: one line per entry that matters, oldest first,

    <UTC ISO time> <unit or identifier>[<pid>] p<priority>: <message>

then a last line `##CURSOR <cursor>`: the position after the last entry READ
(shipped or not), which the harvester saves and sends back next time, so every
entry is read exactly once. `-` is first sight: start seven days back.

An entry ships when it is priority warning or worse, comes from the kernel
(storage and network messages are often `info`), or its unit matches one of
the globs (our own services ship in full). Everything else is read and
skipped here, on the host, so the noisy desktop and session units never cross
the network. At most CAP bytes are written per call; the cursor then points at
the last entry written and the next call continues from there.

The second and only other form reads a log file one of our services writes
outside the journal (a cron job's `>> /var/log/...`):

    file <name> <offset>

`name` must be a line `<name> <absolute path>` in the root-owned allowlist
/etc/morphocloud/mc-journal-files (written by the installer); nothing else is
readable. Output: the bytes from `offset` up to the last whole line (at most
CAP), then `##OFFSET <new offset>`. A file shorter than `offset` (rotated or
rewritten) is read from 0.
"""

from __future__ import annotations

import fnmatch
import grp
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CAP = 4_000_000
# Seconds of reading per call: a first sight of a busy host (the portal keeps
# ~1.8 GB of journal) stops here and the next call continues from the cursor,
# so no call outlives the harvester's JOURNAL_TIMEOUT (180 s).
BUDGET = 100
FIRST_SIGHT = "7 days ago"
WARNING = 4
CURSOR_RE = re.compile(r"^[A-Za-z0-9=;_-]{1,512}$")
UNITS_RE = re.compile(r"^[A-Za-z0-9@._*:,-]{1,1024}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALLOW_FIELDS = 2  # an allowlist line: name, absolute path
ALLOWLIST = "/etc/morphocloud/mc-journal-files"


def deny(msg: str) -> int:
    print(f"mc-journal-read: refused: {msg}", file=sys.stderr)  # noqa: T201 — the caller reads stderr
    return 2


def parse_request(raw: str) -> tuple[str, list[str]] | None:
    parts = raw.split()
    if len(parts) != 3 or parts[0] != "journal":  # noqa: PLR2004 — verb, cursor, units
        return None
    cursor, units = parts[1], parts[2]
    if cursor != "-" and not CURSOR_RE.match(cursor):
        return None
    if not UNITS_RE.match(units):
        return None
    return cursor, [u for u in units.split(",") if u]


def text_of(value) -> str:
    """MESSAGE is a string, or a list of byte values for binary payloads."""
    if isinstance(value, list):
        return bytes(v & 0xFF for v in value if isinstance(v, int)).decode(
            "utf-8", "replace"
        )
    return str(value if value is not None else "")


def wanted(entry: dict, units: list[str]) -> bool:
    try:
        priority = int(entry.get("PRIORITY", 6))
    except ValueError:
        priority = 6
    if priority <= WARNING or entry.get("_TRANSPORT") == "kernel":
        return True
    unit = entry.get("_SYSTEMD_UNIT") or entry.get("UNIT") or ""
    return any(fnmatch.fnmatchcase(unit, g) for g in units)


def line_of(entry: dict) -> str:
    us = int(entry.get("__REALTIME_TIMESTAMP") or 0)
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(us / 1_000_000))
    who = (
        entry.get("_SYSTEMD_UNIT")
        or entry.get("SYSLOG_IDENTIFIER")
        or entry.get("_COMM")
        or ("kernel" if entry.get("_TRANSPORT") == "kernel" else "?")
    )
    pid = entry.get("_PID")
    msg = text_of(entry.get("MESSAGE")).replace("\n", " ⏎ ")
    return f"{when} {who}{f'[{pid}]' if pid else ''} p{entry.get('PRIORITY', '?')}: {msg}\n"


def read(start: list[str], units: list[str], cursor: str, out) -> tuple[str, int, int]:
    """Stream the journal from `start`; returns (last cursor read, entries
    read, journalctl's exit status or 0 when stopped at the cap)."""
    cmd = ["journalctl", "-q", "--no-pager", "-o", "json", *start]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    last, n, written, capped = cursor, 0, 0, False
    deadline = time.monotonic() + BUDGET
    assert proc.stdout is not None
    for raw in proc.stdout:
        if n and time.monotonic() > deadline:
            capped = True  # out of time: this entry comes next call
            break
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if wanted(entry, units):
            line = line_of(entry)
            if written + len(line) > CAP and written:
                capped = (
                    True  # `last` stays at the previous entry: this one comes next time
                )
                break
            out.write(line)
            written += len(line)
        last = entry.get("__CURSOR") or last
        n += 1
    proc.terminate()
    rc = proc.wait(timeout=30)
    return last, n, 0 if capped else rc


def allowed_files(path: str = ALLOWLIST) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if (
                    len(parts) == ALLOW_FIELDS
                    and NAME_RE.match(parts[0])
                    and parts[1].startswith("/")
                ):
                    files[parts[0]] = parts[1]
    except OSError:
        pass
    return files


def read_file(name: str, offset_text: str, out, allowlist: str = ALLOWLIST) -> int:
    path = allowed_files(allowlist).get(name)
    if path is None or not offset_text.isdigit():
        return deny(f"file '{name}' is not in {allowlist}")
    offset = int(offset_text)
    try:
        with Path(path).open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size < offset:
                offset = 0
            fh.seek(offset)
            chunk = fh.read(min(size - offset, CAP))
    except OSError as e:
        return deny(f"cannot read {name}: {type(e).__name__}")
    cut = chunk.rfind(b"\n") + 1  # a partial last line waits for the next call
    out.write(chunk[:cut].decode("utf-8", "replace"))
    out.write(f"##OFFSET {offset + cut}\n")
    return 0


def can_read_system_journal() -> bool:
    """Without root, systemd-journal or adm, journalctl shows only the
    caller's own entries and still exits 0: a reader in that state would look
    healthy and capture almost nothing, so it refuses instead."""
    if os.geteuid() == 0:
        return True
    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return bool(names & {"systemd-journal", "adm", "wheel"})


def main() -> int:
    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    parts = raw.split()
    if len(parts) == 3 and parts[0] == "file":  # noqa: PLR2004 — verb, name, offset
        return read_file(parts[1], parts[2], sys.stdout)
    req = parse_request(raw)
    if req is None:
        return deny(
            "expected 'journal <cursor|-> <unit-globs>' or 'file <name> <offset>'"
        )
    cursor, units = req
    if not can_read_system_journal():
        return deny(
            "this account cannot read the system journal (not in systemd-journal or adm)"
        )
    out = sys.stdout
    if cursor == "-":
        last, _, rc = read(["--since", FIRST_SIGHT], units, cursor, out)
    else:
        last, n, rc = read([f"--after-cursor={cursor}"], units, cursor, out)
        if rc and not n:
            # The cursor is gone (the journal was vacuumed past it): what it
            # pointed at no longer exists anywhere. Restart an hour back and
            # say so in the record rather than stall on it forever.
            out.write(
                f"# cursor no longer in the journal (journalctl exit {rc}); restarted one hour back\n"
            )
            last, _, rc = read(["--since", "1 hour ago"], units, cursor, out)
    if rc:
        return deny(f"journalctl exit {rc}")
    out.write(f"##CURSOR {last}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
