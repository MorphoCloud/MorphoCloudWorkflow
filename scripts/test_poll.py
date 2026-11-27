"""poll.py (the create job's two waits, from one signed-in process) against a
fake Nova that plays back a list of answers.
Run: python -m pytest scripts/test_poll.py"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "poll",
    Path(__file__).resolve().parent.parent / ".github" / "actions" / "poll" / "poll.py",
)
poll = importlib.util.module_from_spec(spec)
sys.modules["poll"] = poll
spec.loader.exec_module(poll)

COMPLETE = '{"status":"complete","epoch":1}'
RUNNING = '{"status":"running","epoch":1}'
BOOT = "[    0.000000] Linux version 6.8\ncloud-init: started\n"
SHORT_WAIT = 9  # server-status timeout used below: looks at 0, 3, 6 and 9 s
SHORT_WAIT_LOOKS = 4
CONSOLE_WAIT = 10


class Clock:
    """Time that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeCompute:
    """One server; `statuses` and `consoles` are what each look returns, the
    last one repeating. An exception in a list is raised on that look."""

    def __init__(self, statuses=(), consoles=(), exists=True):
        self.statuses = list(statuses)
        self.consoles = list(consoles)
        self.exists = exists
        self.finds = 0
        self.looks = 0

    def find_server(self, name, ignore_missing=True):  # noqa: ARG002
        self.finds += 1
        return SimpleNamespace(id="sid-1") if self.exists else None

    def _next(self, answers):
        self.looks += 1
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_server(self, sid):
        assert sid == "sid-1"
        return SimpleNamespace(status=self._next(self.statuses))

    def get_server_console_output(self, sid):
        assert sid == "sid-1"
        return {"output": self._next(self.consoles)}


def conn_of(compute):
    return SimpleNamespace(compute=compute)


# --- server-status ---------------------------------------------------------


def test_status_returns_as_soon_as_wanted():
    clock = Clock()
    compute = FakeCompute(statuses=["BUILD", "BUILD", "ACTIVE"])
    status = poll.wait_status(
        conn_of(compute), "instance-1", ("ACTIVE", "ERROR"), 480, 3, clock.sleep, clock
    )
    assert status == "ACTIVE"
    assert clock.sleeps == [3, 3]
    assert compute.finds == 1  # the name is resolved once, not on every look


def test_status_error_is_a_stable_state_too():
    clock = Clock()
    compute = FakeCompute(statuses=["BUILD", "ERROR"])
    assert (
        poll.wait_status(
            conn_of(compute), "i", ("ACTIVE", "ERROR"), 480, 3, clock.sleep, clock
        )
        == "ERROR"
    )


def test_status_timeout_returns_the_last_status_read():
    clock = Clock()
    compute = FakeCompute(statuses=["BUILD"])
    status = poll.wait_status(
        conn_of(compute), "i", ("ACTIVE", "ERROR"), SHORT_WAIT, 3, clock.sleep, clock
    )
    assert status == "BUILD"
    assert clock.now == SHORT_WAIT  # by the clock, not by counting sleeps
    assert compute.looks == SHORT_WAIT_LOOKS


def test_status_missing_server_is_an_error():
    with pytest.raises(LookupError):
        poll.wait_status(conn_of(FakeCompute(exists=False)), "gone", ("ACTIVE",), 9, 3)


def test_status_api_failure_is_not_swallowed():
    clock = Clock()
    compute = FakeCompute(statuses=["BUILD", RuntimeError("HTTP 503")])
    with pytest.raises(RuntimeError):
        poll.wait_status(conn_of(compute), "i", ("ACTIVE",), 480, 3, clock.sleep, clock)


# --- console status line ---------------------------------------------------


def test_last_status_line_takes_the_last_one():
    console = BOOT + RUNNING + "\nTASK [x]\n" + COMPLETE + "\nlogin: \n"
    assert poll.last_status_line(console) == COMPLETE


def test_last_status_line_none():
    assert poll.last_status_line(BOOT) == ""
    assert poll.status_of("") == "pending"


def test_last_status_line_must_start_the_line():
    assert poll.last_status_line('echo {"status":"complete"}\n') == ""


def test_half_written_line_is_not_a_status_yet():
    # The console read while the script was still writing its line: the
    # earlier whole line stands, and the next look reads the new one whole.
    console = BOOT + RUNNING + '\n{"status":"comple'
    assert poll.last_status_line(console) == RUNNING
    assert poll.status_of(poll.last_status_line(console)) == "running"


def test_status_line_without_a_status_is_pending():
    assert poll.status_of('{"status":"","epoch":1}') == "pending"


# --- console-status --------------------------------------------------------


def test_console_returns_on_complete_with_the_log():
    clock = Clock()
    done = BOOT + COMPLETE + "\n"
    compute = FakeCompute(consoles=[BOOT, BOOT + RUNNING + "\n", done])
    line, console = poll.wait_console(
        conn_of(compute), "i", 1200, 5, clock.sleep, clock
    )
    assert line == COMPLETE
    assert console == done
    assert clock.sleeps == [5, 5]
    assert compute.finds == 1


def test_console_returns_on_error_with_the_reason_line():
    clock = Clock()
    err = '{"status":"error","reason":"no outbound network"}'
    compute = FakeCompute(consoles=[BOOT, BOOT + err + "\n"])
    line, _ = poll.wait_console(conn_of(compute), "i", 1200, 5, clock.sleep, clock)
    assert json.loads(line)["reason"] == "no outbound network"


def test_console_timeout_returns_what_was_last_read():
    clock = Clock()
    last = BOOT + RUNNING + "\nTASK [Install extensions]\n"
    compute = FakeCompute(consoles=[BOOT, last])
    line, console = poll.wait_console(
        conn_of(compute), "i", CONSOLE_WAIT, 5, clock.sleep, clock
    )
    assert line == RUNNING  # not complete, not error: the caller reports a timeout
    assert console == last
    assert clock.now == CONSOLE_WAIT


def test_console_timeout_with_no_status_line_at_all():
    clock = Clock()
    compute = FakeCompute(consoles=[BOOT])
    line, console = poll.wait_console(conn_of(compute), "i", 5, 5, clock.sleep, clock)
    assert line == ""
    assert console == BOOT


def test_console_empty_output_is_pending():
    clock = Clock()
    compute = FakeCompute(consoles=[None, BOOT + COMPLETE])
    line, _ = poll.wait_console(conn_of(compute), "i", 1200, 5, clock.sleep, clock)
    assert line == COMPLETE


# --- command line ----------------------------------------------------------


def run_main(monkeypatch, capsys, compute, argv):
    monkeypatch.setenv("OS_CLOUD", "test")
    monkeypatch.setattr(poll, "connect", lambda: conn_of(compute))
    monkeypatch.setattr(poll.time, "sleep", lambda _s: None)
    code = poll.main(argv)
    return code, capsys.readouterr()


def test_main_server_status_prints_the_output_line(monkeypatch, capsys):
    compute = FakeCompute(statuses=["BUILD", "ACTIVE"])
    code, io = run_main(
        monkeypatch,
        capsys,
        compute,
        ["server-status", "--server", "i", "--until", "ACTIVE,ERROR"],
    )
    assert code == 0
    assert io.out == "final_status=ACTIVE\n"  # stdout is exactly $GITHUB_OUTPUT's line
    assert "Current status: BUILD" in io.err


def test_main_console_status_writes_the_log_and_prints_the_line(
    monkeypatch, capsys, tmp_path
):
    log_file = tmp_path / "console.log"
    compute = FakeCompute(consoles=[BOOT + COMPLETE + "\n"])
    code, io = run_main(
        monkeypatch,
        capsys,
        compute,
        ["console-status", "--server", "i", "--log-file", str(log_file)],
    )
    assert code == 0
    assert io.out == COMPLETE + "\n"
    assert log_file.read_text() == BOOT + COMPLETE + "\n"


def test_main_console_status_prints_nothing_without_a_status_line(
    monkeypatch, capsys, tmp_path
):
    compute = FakeCompute(consoles=[BOOT])
    code, io = run_main(
        monkeypatch,
        capsys,
        compute,
        [
            "console-status",
            "--server",
            "i",
            "--timeout",
            "0",
            "--log-file",
            str(tmp_path / "c"),
        ],
    )
    assert code == 0
    assert io.out == ""


def test_main_api_failure_exits_1(monkeypatch, capsys, tmp_path):
    compute = FakeCompute(consoles=[RuntimeError("HTTP 503")])
    code, io = run_main(
        monkeypatch,
        capsys,
        compute,
        ["console-status", "--server", "i", "--log-file", str(tmp_path / "c")],
    )
    assert code == 1
    assert "::error ::" in io.err
    assert io.out == ""


def test_main_missing_server_exits_1(monkeypatch, capsys):
    code, io = run_main(
        monkeypatch,
        capsys,
        FakeCompute(exists=False),
        ["server-status", "--server", "gone", "--until", "ACTIVE"],
    )
    assert code == 1
    assert "no server named gone" in io.err


def test_main_needs_a_cloud(monkeypatch, capsys):
    monkeypatch.delenv("OS_CLOUD", raising=False)
    monkeypatch.delenv("OS_AUTH_URL", raising=False)
    assert poll.main(["server-status", "--server", "i", "--until", "ACTIVE"]) == 1
    assert "neither OS_CLOUD nor OS_AUTH_URL" in capsys.readouterr().err


# --- the waits of shelve, unshelve and delete (control-instance) ---


class ControlCompute:
    """One server whose looks are (status, task_state[, host, host_id,
    addresses]) tuples, the last repeating; `actions` is what each read of the
    action log returns, the last repeating; `listings` what each listing of
    the name returns. An exception in a list is raised on that look."""

    def __init__(self, looks=(), actions=(), listings=()):
        self.server_looks = list(looks)
        self.action_reads = list(actions)
        self.listings = list(listings)
        self.looks = 0
        self.action_looks = 0

    def find_server(self, name, ignore_missing=True):  # noqa: ARG002
        return SimpleNamespace(id="sid-1")

    @staticmethod
    def _take(answers):
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get_server(self, sid):  # noqa: ARG002
        self.looks += 1
        look = self._take(self.server_looks)
        status, task_state, *rest = look
        host, host_id, addresses = rest or ("h", "hid", {"net": [1]})
        return SimpleNamespace(
            status=status,
            task_state=task_state,
            compute_host=host,
            host_id=host_id,
            addresses=addresses,
        )

    def server_actions(self, sid):  # noqa: ARG002
        self.action_looks += 1
        return [
            SimpleNamespace(action=a, message=m, request_id=r)
            for a, m, r in self._take(self.action_reads)
        ]

    def servers(self, name):  # noqa: ARG002
        return [SimpleNamespace(name=n) for n in self._take(self.listings)]


def control(looks=(), actions=((),), listings=((),)):
    compute = ControlCompute(looks, actions, listings)
    return SimpleNamespace(compute=compute), compute, Clock()


def test_task_settled_at_once_when_nothing_is_in_flight():
    conn, compute, clock = control([("ACTIVE", None)])
    out = poll.wait_task_settled(conn, "i", "", 600, 20, clock.sleep, clock)
    assert out == ("null", False)
    assert compute.looks == 1
    assert clock.sleeps == []


def test_task_settled_waits_for_the_action_in_flight():
    conn, _, clock = control(
        [("ACTIVE", "shelving"), ("ACTIVE", "shelving"), ("SHELVED_OFFLOADED", None)]
    )
    out = poll.wait_task_settled(conn, "i", "", 600, 20, clock.sleep, clock)
    assert out == ("null", False)
    assert clock.sleeps == [20, 20]


def test_task_settled_stops_on_a_delete_already_in_flight():
    conn, _, clock = control([("ACTIVE", "deleting")])
    out = poll.wait_task_settled(conn, "i", "deleting", 600, 20, clock.sleep, clock)
    assert out == ("deleting", False)
    assert clock.sleeps == []


def test_task_settled_without_stop_on_waits_out_a_delete():
    conn, _, clock = control([("ACTIVE", "deleting")])
    out = poll.wait_task_settled(conn, "i", "", 60, 20, clock.sleep, clock)
    assert out == ("deleting", True)
    assert clock.now == 60  # noqa: PLR2004


def test_watch_ends_when_the_status_is_reached():
    conn, compute, clock = control(
        [
            ("ACTIVE", "shelving"),
            ("SHELVED", "shelving_offloading"),
            ("SHELVED_OFFLOADED", None),
        ]
    )
    out = poll.watch_server(
        conn, "i", "SHELVED_OFFLOADED", 900, 5, sleep=clock.sleep, clock=clock
    )
    assert out == {
        "status": "SHELVED_OFFLOADED",
        "task_state": "null",
        "reason": "reached",
        "failed_req": "",
    }
    assert compute.action_looks == 0  # a shelve never reads the action log


def test_watch_times_out_with_the_last_status():
    conn, _, clock = control([("ACTIVE", "shelving")])
    out = poll.watch_server(
        conn, "i", "SHELVED_OFFLOADED", 20, 5, sleep=clock.sleep, clock=clock
    )
    assert out["reason"] == "timeout"
    assert out["status"] == "ACTIVE"
    assert clock.now == 20  # noqa: PLR2004


def test_watch_error_without_a_host_is_a_capacity_error_only_when_asked():
    looks = [("ERROR", None, None, "", {})]
    conn, _, clock = control(looks)
    out = poll.watch_server(
        conn, "i", "ACTIVE", 300, 5, no_host_error=True, sleep=clock.sleep, clock=clock
    )
    assert out["reason"] == "no_host_error"
    assert clock.sleeps == []
    conn, _, clock = control(looks)
    out = poll.watch_server(conn, "i", "ACTIVE", 10, 5, sleep=clock.sleep, clock=clock)
    assert out["reason"] == "timeout"


def test_watch_error_on_a_host_keeps_waiting():
    conn, _, clock = control([("ERROR", None, "host-1", "hid", {})])
    out = poll.watch_server(
        conn, "i", "ACTIVE", 10, 5, no_host_error=True, sleep=clock.sleep, clock=clock
    )
    assert out["reason"] == "timeout"


def test_watch_finds_a_new_failed_unshelve_at_30_seconds():
    acts = [
        [
            ("unshelve", "Error", "req-new"),
            ("unshelve", "Error", "req-old"),
            ("shelve", None, "req-s"),
        ]
    ]
    conn, compute, clock = control([("SHELVED_OFFLOADED", None)], acts)
    out = poll.watch_server(
        conn,
        "i",
        "ACTIVE",
        300,
        5,
        known_errors=frozenset({"req-old"}),
        sleep=clock.sleep,
        clock=clock,
    )
    assert out["reason"] == "unshelve_error"
    assert out["failed_req"] == "req-new"
    assert clock.now == 30  # noqa: PLR2004
    assert compute.action_looks == 1


def test_watch_ignores_failures_already_known():
    acts = [[("unshelve", "Error", "req-old"), ("shelve", "Error", "req-x")]]
    conn, compute, clock = control([("SHELVED_OFFLOADED", None)], acts)
    out = poll.watch_server(
        conn,
        "i",
        "ACTIVE",
        90,
        5,
        known_errors=frozenset({"req-old"}),
        sleep=clock.sleep,
        clock=clock,
    )
    assert out["reason"] == "timeout"
    assert compute.action_looks == 3  # noqa: PLR2004 — at 30, 60 and 90 s


def test_watch_does_not_read_the_action_log_while_an_attempt_is_in_flight():
    """Instances #127: attempt 1's Error is in the log while attempt 2 is
    spawning; reading it then declared attempt 2 dead 8 s before it came up."""
    acts = [[("unshelve", "Error", "req-1")]]
    looks = [("SHELVED_OFFLOADED", "spawning")] * 8 + [("ACTIVE", None)]
    conn, compute, clock = control(looks, acts)
    out = poll.watch_server(
        conn,
        "i",
        "ACTIVE",
        300,
        5,
        known_errors=frozenset(),
        sleep=clock.sleep,
        clock=clock,
    )
    assert out["reason"] == "reached"
    assert compute.action_looks == 0


def test_watch_survives_an_unreadable_action_log():
    acts = [RuntimeError("503"), [("unshelve", "Error", "req-new")]]
    conn, _, clock = control([("SHELVED_OFFLOADED", None)], acts)
    out = poll.watch_server(
        conn,
        "i",
        "ACTIVE",
        300,
        5,
        known_errors=frozenset(),
        sleep=clock.sleep,
        clock=clock,
    )
    assert out["failed_req"] == "req-new"
    assert clock.now == 60  # noqa: PLR2004


def test_gone_once_the_name_is_no_longer_listed():
    conn, _, clock = control(listings=[["instance-7"], ["instance-7"], []])
    assert poll.wait_gone(conn, "instance-7", 180, 5, clock.sleep, clock) == (True, 10)


def test_gone_keeps_only_the_exact_name():
    conn, _, clock = control(listings=[["instance-70", "instance-7-old"]])
    assert poll.wait_gone(conn, "instance-7", 180, 5, clock.sleep, clock) == (True, 0)


def test_gone_counts_a_failed_listing_as_still_listed():
    conn, _, clock = control(listings=[RuntimeError("503")])
    assert poll.wait_gone(conn, "instance-7", 15, 5, clock.sleep, clock) == (False, 15)


def test_status_while_active_ends_on_shutoff_or_any_other_status():
    conn, _, clock = control([("ACTIVE", "powering-off"), ("SHUTOFF", None)])
    out = poll.wait_status(
        conn, "i", ("SHUTOFF",), 180, 10, clock.sleep, clock, while_in=("ACTIVE",)
    )
    assert out == "SHUTOFF"
    conn, _, clock = control([("ACTIVE", None), ("ERROR", None)])
    out = poll.wait_status(
        conn, "i", ("SHUTOFF",), 180, 10, clock.sleep, clock, while_in=("ACTIVE",)
    )
    assert out == "ERROR"


def test_status_tolerating_errors_waits_through_a_failed_read():
    conn, _, clock = control([RuntimeError("503"), ("ACTIVE", None), ("SHUTOFF", None)])
    out = poll.wait_status(
        conn,
        "i",
        ("SHUTOFF",),
        180,
        10,
        clock.sleep,
        clock,
        while_in=("ACTIVE",),
        tolerate_errors=True,
    )
    assert out == "SHUTOFF"
    assert clock.sleeps == [10, 10]


def test_status_without_tolerance_still_raises():
    conn, _, clock = control([RuntimeError("503")])
    with pytest.raises(RuntimeError):
        poll.wait_status(conn, "i", ("ACTIVE",), 30, 3, clock.sleep, clock)
