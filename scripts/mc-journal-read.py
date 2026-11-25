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

    file <name> <offset | ->

`name` must be a line `<name> <absolute path>` in the root-owned allowlist
/etc/morphocloud/mc-journal-files (written by the installer); nothing else is
readable. Output: the bytes from `offset` up to the last whole line (at most
CAP), then `##OFFSET <new offset>`. A file shorter than `offset` (rotated or
rewritten) is read from 0. `-` is first sight: nothing is returned but the
file's current size, so reading starts at its end.

The third form reads the GitHub Actions runner's own logs (`_diag`) on a
runner host (INCIDENT_STORE_PLAN.md, "Reconcile with production after the
migration"):

    diag

with a JSON object on standard input,
{"prime": bool, "offsets": {file: n}, "forget_before": epoch seconds}.
The directory is the `diag-dir` line of the same allowlist; without that line
the request is refused. Only regular files (never a link) changed in the last
DIAG_WINDOW are looked at, and only lines matching DIAG_MATCH, the runner's
own ERR level, are returned, at most CAP bytes of them per call. For each file:
`==FILE <name> <new offset>` and then its matching lines. A file starts at its
end (the last whole line), shipping nothing, when the call is `prime` (the
directory's history is not evidence) or when the caller has no offset for it
and its name (`..._YYYYMMDD-HHMMSS...`, the runner's own) says it was created
before `forget_before`: the caller forgot that offset, so its earlier lines
were already shipped. A file without a date in its name and no offset is read
from 0 (new). Once the CAP or the time budget is reached, every remaining
file the caller knows is named with the caller's own offset and nothing else
is read, so the caller keeps it. Last line: `##DIAG ok`.
"""

from __future__ import annotations

import calendar
import fnmatch
import grp
import json
import os
import re
import stat
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
# The `diag` form. The pattern is the harvester's `diag_match`
# (incident-sources.yml; a test keeps the two equal): the runner's own ERR
# lines, nothing else from its logs leaves the host.
DIAG_DIR = "diag-dir"  # the allowlist name of the runner's _diag directory
DIAG_MATCH = re.compile(r"^\[\S+ \S+ ERR ")
DIAG_WINDOW = 48 * 3600  # files changed in the last two days
DIAG_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")  # never . or ..
DIAG_NAME_DATE_RE = re.compile(r"(\d{8})-(\d{6})")  # Worker_20261125-002110-utc.log
DIAG_TAIL = 65_536  # bytes read back from a file's end to find its last whole line
DIAG_REQUEST_MAX = 2_000_000  # bytes of JSON on standard input
DIAG_READ_MAX = 64_000_000  # bytes read from one file per call


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
    path = allowed_files(allowlist).get(name) if name != DIAG_DIR else None
    if path is None or not (offset_text.isdigit() or offset_text == "-"):
        return deny(f"file '{name}' is not in {allowlist}")
    try:
        if offset_text == "-":
            # first sight: start at the file's current end, as the _diag
            # reader does; a service log's months of history are not evidence
            # of anything current (the welcome-email log held 13 MB).
            out.write(f"##OFFSET {Path(path).stat().st_size}\n")
            return 0
        offset = int(offset_text)
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


def diag_request(raw: str) -> tuple[bool, dict[str, int], float] | None:  # noqa: PLR0911 — one return per malformation
    """(prime, offsets, forget_before) from the caller's JSON, or None when it
    is malformed."""
    try:
        req = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(req, dict) or not isinstance(req.get("prime"), bool):
        return None
    offsets = req.get("offsets") or {}
    forget = req.get("forget_before", 0)
    if not isinstance(offsets, dict) or isinstance(forget, bool):
        return None
    if not isinstance(forget, (int, float)) or forget < 0:
        return None
    clean: dict[str, int] = {}
    for name, off in offsets.items():
        if not (isinstance(name, str) and DIAG_NAME_RE.match(name)):
            return None
        if not isinstance(off, int) or isinstance(off, bool) or off < 0:
            return None
        clean[name] = off
    return req["prime"], clean, float(forget)


def created_by_name(name: str) -> float | None:
    """The creation time the runner writes into a _diag file name, or None."""
    m = DIAG_NAME_DATE_RE.search(name)
    if not m:
        return None
    try:
        return float(
            calendar.timegm(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
        )
    except ValueError:
        return None


def open_regular(path: Path):
    """The file opened for reading, never through a link, or None when it is
    not a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    fh = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
        fh.close()
        return None
    return fh


def line_end(path: Path) -> int | None:
    """The offset just after the file's last whole line: where a first sight
    starts, so the next read never begins in the middle of a line. A tail of
    DIAG_TAIL bytes without a newline counts as one line: its end."""
    fh = open_regular(path)
    if fh is None:
        return None
    with fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(max(0, size - DIAG_TAIL))
        tail = fh.read()
    nl = tail.rfind(b"\n")
    if nl < 0:
        return size if len(tail) == DIAG_TAIL else size - len(tail)
    return size - len(tail) + nl + 1


def diag_hits(
    path: Path, offset: int, budget: int
) -> tuple[int, list[str], bool] | None:
    """(new offset, matching lines, budget reached) of one regular file read
    from `offset`, never through a link, with at most `budget` bytes of lines;
    None when it cannot be read as one. When the budget is reached, the new
    offset is just before the first line left out, which comes next call. A
    single matching line longer than the whole budget is cut to it rather
    than stopping the file forever, and so is a run of DIAG_READ_MAX bytes
    without a newline (skipped)."""
    fh = open_regular(path)
    if fh is None:
        return None
    with fh:
        size = os.fstat(fh.fileno()).st_size
        if size < offset:  # rewritten shorter: from the start
            offset = 0
        fh.seek(offset)
        chunk = fh.read(min(size - offset, DIAG_READ_MAX))
    cut = chunk.rfind(b"\n") + 1  # a partial last line waits for the next call
    if cut == 0:
        # no newline in a full read: one line longer than DIAG_READ_MAX, skipped
        return offset + (len(chunk) if len(chunk) == DIAG_READ_MAX else 0), [], False
    hits: list[str] = []
    used = pos = 0
    for raw in chunk[:cut].split(b"\n")[:-1]:
        line = raw.decode("utf-8", "replace") + "\n"
        if DIAG_MATCH.search(line):
            if used + len(line) > budget:
                if hits:
                    return offset + pos, hits, True
                line = line[: max(budget - 1, 0)] + "\n"  # never stuck on one line
                hits.append(line)
                return offset + pos + len(raw) + 1, hits, True
            hits.append(line)
            used += len(line)
        pos += len(raw) + 1
    return offset + pos, hits, False


def read_diag(stdin, out, allowlist: str = ALLOWLIST, now: float | None = None) -> int:
    directory = allowed_files(allowlist).get(DIAG_DIR)
    if directory is None:
        return deny(f"no '{DIAG_DIR}' line in {allowlist}")
    req = diag_request(stdin.read(DIAG_REQUEST_MAX + 1)[:DIAG_REQUEST_MAX])
    if req is None:
        return deny(
            'expected {"prime": true|false, "offsets": {file: n},'
            ' "forget_before": seconds} on stdin'
        )
    prime, offsets, forget_before = req
    now = time.time() if now is None else now
    deadline = time.monotonic() + BUDGET
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError as e:
        return deny(f"cannot list {DIAG_DIR}: {type(e).__name__}")
    written = 0
    deferred = False  # the CAP or the time budget is reached: name, never read
    for entry in entries:
        deferred = deferred or time.monotonic() > deadline
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (
            not stat.S_ISREG(st.st_mode)
            or not DIAG_NAME_RE.match(entry.name)
            or now - st.st_mtime > DIAG_WINDOW
        ):
            continue
        known = entry.name in offsets
        if deferred:
            if known:  # the caller keeps its offset and its "last seen"
                out.write(f"==FILE {entry.name} {offsets[entry.name]}\n")
            continue
        born = created_by_name(entry.name)
        if prime or (not known and born is not None and born < forget_before):
            end = line_end(Path(entry.path))
            if end is not None:
                out.write(f"==FILE {entry.name} {end}\n")
            continue
        got = diag_hits(Path(entry.path), offsets.get(entry.name, 0), CAP - written)
        if got is None:
            continue
        new, hits, full = got
        out.write(f"==FILE {entry.name} {new}\n")
        out.writelines(hits)
        written += sum(len(h) for h in hits)
        deferred = full or written >= CAP
    out.write("##DIAG ok\n")
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
    if parts == ["diag"]:
        return read_diag(sys.stdin, sys.stdout)
    req = parse_request(raw)
    if req is None:
        return deny(
            "expected 'journal <cursor|-> <unit-globs>', 'file <name> <offset>' or 'diag'"
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
