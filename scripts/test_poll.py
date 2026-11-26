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
