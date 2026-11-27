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
    python poll.py task-settled --server NAME [--stop-on TASK_STATE] \
        [--timeout SECONDS] [--interval SECONDS]
    python poll.py server-watch --server NAME --until STATUS \
        [--no-host-error] [--unshelve-errors-known ID,ID] \
        [--timeout SECONDS] [--interval SECONDS]
    python poll.py server-gone --server NAME \
        [--timeout SECONDS] [--interval SECONDS]

`server-status` waits until the server's status is one of --until, or the
time runs out. It prints `final_status=...` (the last status read) for
$GITHUB_OUTPUT; the caller decides what a status that is not the wanted one
means.

`server-status --while ACTIVE` is the other way round: it waits while the
status is one of --while. With --tolerate-errors a read that fails counts as
status UNKNOWN and the wait goes on.

`task-settled` waits until the server has no action in flight (task_state is
empty), or its task_state is --stop-on, or the time runs out. It prints
`task_state=...` (`null` when empty) and `timed_out=true|false`.

`server-watch` is the wait after a shelve, unshelve or start. It ends when the
status is --until; with --no-host-error, when the status is ERROR and the
server has no host and no address (Jetstream2 placed it nowhere); with
--unshelve-errors-known, when an unshelve action that is not in the given list
ended in Error (looked for every 30 s, and only while no action is in flight:
an Error found during an attempt cannot be that attempt's); or when the time
runs out. It prints `status=`, `task_state=`, `reason=` (reached,
no_host_error, unshelve_error, timeout) and `failed_req=`; the caller decides
what to do and calls again for another attempt.

`server-gone` waits until no server has the name, or the time runs out. A
listing that fails counts as "still listed". It prints `gone=true|false` and
`waited=SECONDS`.

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
# How often server-watch reads the server's actions for a failed unshelve.
ACTION_CHECK_SECONDS = 30


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
    while_in: tuple = (),
    tolerate_errors: bool = False,
) -> str:
    """The server's status once it is one of `until` (or, with `while_in`, no
    longer one of those), else the last one read when `timeout` seconds have
    passed. With `tolerate_errors` a read that fails is status UNKNOWN and the
    wait goes on; without, it raises."""
    sid = None
    start = clock()
    while True:
        try:
            if sid is None:
                sid = server_id(conn, name)
            status = conn.compute.get_server(sid).status
        except Exception as e:
            if not tolerate_errors:
                raise
            log(f"::warning ::could not read the status: {type(e).__name__}: {e}")
            status = "UNKNOWN"
        waited = int(clock() - start)
        log(f"Current status: {status} (waited {waited}s)")
        done = status in until
        if while_in and status != "UNKNOWN":
            done = done or status not in while_in
        if done:
            log(f"Instance reached stable state: {status}")
            return status
        if clock() - start >= timeout:
            return status
        sleep(interval)


def wait_task_settled(  # noqa: PLR0913 — the knobs are what the callers vary
    conn,
    name: str,
    stop_on: str,
    timeout: float,
    interval: float,
    sleep=time.sleep,
    clock=time.monotonic,
) -> tuple[str, bool]:
    """(task_state, timed out): task_state is "null" once no action is in
    flight, `stop_on` when that is what the server is doing, else the action
    still in flight when `timeout` seconds have passed."""
    sid = server_id(conn, name)
    start = clock()
    while True:
        task_state = conn.compute.get_server(sid).task_state or "null"
        waited = int(clock() - start)
        log(f"task_state [{task_state}] waited [{waited}s]")
        if task_state == "null" or (stop_on and task_state == stop_on):
            return task_state, False
        if clock() - start >= timeout:
            return task_state, True
        sleep(interval)


def new_unshelve_error(conn, sid: str, known: frozenset) -> str:
    """The request id of an unshelve action that ended in Error and is not in
    `known`, newest first as Nova lists them; "" when there is none or the
    actions could not be read (the next look asks again)."""
    try:
        actions = list(conn.compute.server_actions(sid))
    except Exception as e:
        log(f"::warning ::could not read the server's actions: {type(e).__name__}: {e}")
        return ""
    for action in actions:
        if action.action != "unshelve" or action.message != "Error":
            continue
        if action.request_id and action.request_id not in known:
            return action.request_id
    return ""


def watch_server(  # noqa: PLR0913 — the knobs are what the callers vary
    conn,
    name: str,
    until: str,
    timeout: float,
    interval: float,
    no_host_error: bool = False,
    known_errors: frozenset | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict:
    """Wait after a shelve, unshelve or start; see the module text. Returns
    status, task_state, reason and failed_req."""
    sid = server_id(conn, name)
    start = clock()
    next_action_check = ACTION_CHECK_SECONDS
    status, task_state = "PENDING", "null"

    def result(reason: str, failed_req: str = "") -> dict:
        return {
            "status": status,
            "task_state": task_state,
            "reason": reason,
            "failed_req": failed_req,
        }

    while True:
        server = conn.compute.get_server(sid)
        status = server.status or "PENDING"
        task_state = server.task_state or "null"
        waited = clock() - start
        log(f"status [{status}] task [{task_state}] (waited {int(waited)}s).")
        if (
            no_host_error
            and status == "ERROR"
            and not server.compute_host
            and not server.host_id
            and not server.addresses
        ):
            log("ERROR with no host and no address: Jetstream2 placed it nowhere.")
            return result("no_host_error")
        if status == until:
            return result("reached")
        if known_errors is not None and waited >= next_action_check:
            next_action_check += ACTION_CHECK_SECONDS
            if task_state == "null":
                failed_req = new_unshelve_error(conn, sid, known_errors)
                if failed_req:
                    return result("unshelve_error", failed_req)
        if waited >= timeout:
            return result("timeout")
        sleep(interval)


def wait_gone(  # noqa: PLR0913 — the knobs are what the callers vary
    conn,
    name: str,
    timeout: float,
    interval: float,
    sleep=time.sleep,
    clock=time.monotonic,
) -> tuple[bool, int]:
    """(gone, seconds waited): gone once no server has exactly this name. A
    listing that fails counts as still listed; the timeout bounds it."""
    start = clock()
    while True:
        try:
            # Nova matches `name` as a pattern; keep the exact name only.
            listed = any(s.name == name for s in conn.compute.servers(name=f"^{name}$"))
        except Exception as e:
            log(f"::warning ::could not list servers: {type(e).__name__}: {e}")
            listed = True
        waited = int(clock() - start)
        if not listed:
            return True, waited
        if clock() - start >= timeout:
            return False, waited
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("server-status")
    s.add_argument("--server", required=True)
    s.add_argument("--until", default="")
    s.add_argument("--while", dest="while_in", default="")
    s.add_argument("--tolerate-errors", action="store_true")
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("--interval", type=float, default=3)
    t = sub.add_parser("task-settled")
    t.add_argument("--server", required=True)
    t.add_argument("--stop-on", default="")
    t.add_argument("--timeout", type=float, default=600)
    t.add_argument("--interval", type=float, default=20)
    w = sub.add_parser("server-watch")
    w.add_argument("--server", required=True)
    w.add_argument("--until", required=True)
    w.add_argument("--no-host-error", action="store_true")
    w.add_argument("--unshelve-errors-known", default=None)
    w.add_argument("--timeout", type=float, default=600)
    w.add_argument("--interval", type=float, default=5)
    g = sub.add_parser("server-gone")
    g.add_argument("--server", required=True)
    g.add_argument("--timeout", type=float, default=180)
    g.add_argument("--interval", type=float, default=5)
    c = sub.add_parser("console-status")
    c.add_argument("--server", required=True)
    c.add_argument("--log-file", required=True)
    c.add_argument("--timeout", type=float, default=1200)
    c.add_argument("--interval", type=float, default=5)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "server-status" and not (args.until or args.while_in):
        parser.error("server-status needs --until or --while")
    if not (os.environ.get("OS_CLOUD") or os.environ.get("OS_AUTH_URL")):
        log("neither OS_CLOUD nor OS_AUTH_URL is set")
        return 1
    try:
        conn = connect()
        if args.cmd == "server-status":
            until = tuple(u for u in args.until.split(",") if u)
            while_in = tuple(u for u in args.while_in.split(",") if u)
            status = wait_status(
                conn,
                args.server,
                until,
                args.timeout,
                args.interval,
                while_in=while_in,
                tolerate_errors=args.tolerate_errors,
            )
            print(f"final_status={status}")  # noqa: T201
        elif args.cmd == "task-settled":
            task_state, timed_out = wait_task_settled(
                conn, args.server, args.stop_on, args.timeout, args.interval
            )
            print(f"task_state={task_state}")  # noqa: T201
            print(f"timed_out={'true' if timed_out else 'false'}")  # noqa: T201
        elif args.cmd == "server-watch":
            known = None
            if args.unshelve_errors_known is not None:
                known = frozenset(k for k in args.unshelve_errors_known.split(",") if k)
            out = watch_server(
                conn,
                args.server,
                args.until,
                args.timeout,
                args.interval,
                no_host_error=args.no_host_error,
                known_errors=known,
            )
            for key, value in out.items():
                print(f"{key}={value}")  # noqa: T201
        elif args.cmd == "server-gone":
            gone, waited = wait_gone(conn, args.server, args.timeout, args.interval)
            print(f"gone={'true' if gone else 'false'}")  # noqa: T201
            print(f"waited={waited}")  # noqa: T201
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
