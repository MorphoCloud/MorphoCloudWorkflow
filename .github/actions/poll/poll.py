"""Wait on a Jetstream2 server from one process, not one `openstack` command per look.

A create job is almost all waiting, and the waiting was the job's CPU cost:
each `openstack ...` command starts Python, loads the client and signs in
again, about one second of CPU per call. The build wait called it every 3 s
and the setup wait every 5 s, which measured 19% and 12% of the runner's one
core (runner-1, 2026-11-26); the same two reads from one process that stays
signed in measured 0.6%. With several runner agents on one host, that cost is
what limits how many creates can run side by side.

Usage (from a workflow step, with the runner's venv active and OS_CLOUD set):

    python poll.py server-status --server NAME --until ACTIVE,ERROR \
        [--timeout SECONDS] [--interval SECONDS]
    python poll.py console-status --server NAME --log-file PATH \
        [--timeout SECONDS] [--interval SECONDS]

`server-status` waits until the server's status is one of --until, or the
time runs out. It prints `final_status=...` (the last status read) for
$GITHUB_OUTPUT; the caller decides what a status that is not the wanted one
means.

`console-status` waits until the instance's own setup script reports
`complete` or `error` on the console (a line starting `{"status":"`), or the
time runs out. It writes the last console log read to --log-file and prints
the last status line (nothing when there was none); the caller reads the
status, the reason and the failed extensions from that line.

Progress goes to stderr, one line per look, as the shell loops printed it.

Exit status: 0 the wait ended (reached, or timed out: the output says which),
1 Jetstream2 could not be asked or the server does not exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

STATUS_LINE_PREFIX = '{"status":"'
DONE_STATUSES = ("complete", "error")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)  # noqa: T201


def server_id(conn, name: str) -> str:
    """The server's id, looked up once: every later read is one request."""
    server = conn.compute.find_server(name, ignore_missing=True)
    if server is None:
        msg = f"no server named {name}"
        raise LookupError(msg)
    return server.id


def wait_status(  # noqa: PLR0913 — the knobs are what the callers vary
    conn,
    name: str,
    until: tuple,
    timeout: float,
    interval: float,
    sleep=time.sleep,
    clock=time.monotonic,
) -> str:
    """The server's status once it is one of `until`, else the last one read
    when `timeout` seconds have passed."""
    sid = server_id(conn, name)
    start = clock()
    while True:
        status = conn.compute.get_server(sid).status
        waited = int(clock() - start)
        log(f"Current status: {status} (waited {waited}s)")
        if status in until:
            log(f"Instance reached stable state: {status}")
            return status
        if clock() - start >= timeout:
            return status
        sleep(interval)


def last_status_line(console: str) -> str:
    """The last line of the console that is the setup script's status object,
    or "" when there is none. A line cut short (the console was read while it
    was being written) is not a status yet: the next look reads it whole."""
    for line in reversed(console.splitlines()):
        if not line.startswith(STATUS_LINE_PREFIX):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return line
    return ""


def status_of(line: str) -> str:
    if not line:
        return "pending"
    return str(json.loads(line).get("status") or "pending")


def wait_console(  # noqa: PLR0913 — the knobs are what the callers vary
    conn,
    name: str,
    timeout: float,
    interval: float,
    sleep=time.sleep,
    clock=time.monotonic,
) -> tuple[str, str]:
    """(last status line, console log) once the setup script reported
    complete or error, else what was last read when the time ran out."""
    sid = server_id(conn, name)
    start = clock()
    while True:
        console = conn.compute.get_server_console_output(sid).get("output") or ""
        line = last_status_line(console)
        status = status_of(line)
        waited = int(clock() - start)
        if status in DONE_STATUSES:
            log(f"setup status [{status}] (waited {waited}s).")
            return line, console
        if clock() - start >= timeout:
            log(f"setup status [{status}] (waited {waited}s). Out of time.")
            return line, console
        log(f"setup status [{status}] (waited {waited}s). Waiting for completion...")
        sleep(interval)


def connect():
    import openstack  # noqa: PLC0415 — the runner's venv; tests pass a fake

    # As fip.py: a named cloud (clouds.yaml), or an application credential in
    # the OS_* environment variables.
    if os.environ.get("OS_CLOUD"):
        return openstack.connect(cloud=os.environ["OS_CLOUD"])
    return openstack.connect()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("server-status")
    s.add_argument("--server", required=True)
    s.add_argument("--until", required=True)
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("--interval", type=float, default=3)
    c = sub.add_parser("console-status")
    c.add_argument("--server", required=True)
    c.add_argument("--log-file", required=True)
    c.add_argument("--timeout", type=float, default=1200)
    c.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    if not (os.environ.get("OS_CLOUD") or os.environ.get("OS_AUTH_URL")):
        log("neither OS_CLOUD nor OS_AUTH_URL is set")
        return 1
    try:
        conn = connect()
        if args.cmd == "server-status":
            until = tuple(u for u in args.until.split(",") if u)
            status = wait_status(conn, args.server, until, args.timeout, args.interval)
            print(f"final_status={status}")  # noqa: T201
        else:
            line, console = wait_console(conn, args.server, args.timeout, args.interval)
            Path(args.log_file).write_text(console, encoding="utf-8")
            if line:
                print(line)  # noqa: T201
    except Exception as e:
        log(f"::error ::{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
