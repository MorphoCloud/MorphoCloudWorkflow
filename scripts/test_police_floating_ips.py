"""The plan of police-floating-ips.py (decision 24), with no OpenStack.
Run: python -m pytest scripts/test_police_floating_ips.py"""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "police", Path(__file__).resolve().parent / "police-floating-ips.py"
)
police = importlib.util.module_from_spec(spec)
sys.modules["police"] = police
spec.loader.exec_module(police)

NOW = 1_800_000_000.0
NOV_4_2026 = 1793750400.0  # 2026-11-04T00:00:00Z
QUIET = {
    "held": 5,
    "running": 1,
    "spares": 4,
    "target": 5,
    "hold": 5,
    "infra": 0,
    "unknown": 0,
}


def fip(ip, port=None, fixed=None, fid=None):
    return {
        "ID": fid or f"id-{ip}",
        "Floating IP Address": ip,
        "Port": port,
        "Fixed IP Address": fixed,
    }


def srv(status="ACTIVE", instance=True, name=None, task=""):
    return {
        "status": status,
        "task": task,
        "instance": instance,
        "name": name
        or ("morpho-cloud_instance-7" if instance else "morphocloud-instances-runner"),
    }


def run_plan(fips, by_fixed=None, hold=5, ages=None):
    # ages: address -> seconds since its binding changed. An address not
    # listed counts as changed just now, so it is never idle and never a
    # release candidate; a test that expects a release must list it.
    ages = ages or {}
    return police.plan(
        fips,
        by_fixed or {},
        hold,
        NOW,
        lambda f: NOW - ages.get(f["Floating IP Address"], 0),
    )


def kinds(actions):
    """(kind, address) for a change, (kind, key) for an alert, (kind,) for a mint."""
    out = []
    for a in actions:
        if a[0] in ("unset", "delete"):
            out.append((a[0], a[2]))
        elif a[0] == "alert":
            out.append((a[0], a[1]))
        else:
            out.append((a[0],))
    return out


def test_addresses_are_read_from_every_shape_the_client_renders():
    assert police.addresses({"net": ["10.0.0.5", "149.165.1.1"]}) == [
        "10.0.0.5",
        "149.165.1.1",
    ]
    assert police.addresses(
        {"net": [{"addr": "10.0.0.5", "OS-EXT-IPS:type": "fixed"}]}
    ) == ["10.0.0.5"]
    assert police.addresses("net=10.0.0.5, 149.165.1.1; net2=10.0.1.9") == [
        "10.0.0.5",
        "149.165.1.1",
        "10.0.1.9",
    ]
    assert police.addresses(None) == []


def test_a_missing_timestamp_is_never_idle():
    assert police.epoch(None) >= police.time.time() - 1
    assert police.epoch("garbage") >= police.time.time() - 1
    assert police.epoch("2026-11-04T00:00:00Z") == NOV_4_2026


def test_instance_names_are_told_from_infrastructure():
    assert police.INSTANCE_RE.search("instance-254")  # production names them so
    assert police.INSTANCE_RE.search("morpho-cloud_instance-254")
    assert police.INSTANCE_RE.search("morpho-cloud-test_instance-503")
    assert not police.INSTANCE_RE.search("morphocloud-instances-runner-2")
    assert not police.INSTANCE_RE.search("mc-data-portal")
    assert not police.INSTANCE_RE.search("md-models")


def test_exactly_the_hold_when_quiet_does_nothing():
    fips = [fip("149.165.1.1", "p1", "10.0.0.1")] + [
        fip(f"149.165.2.{i}") for i in range(1, 5)
    ]
    actions, counts = run_plan(
        fips, {"10.0.0.1": srv()}, ages={f"149.165.2.{i}": 9999 for i in range(1, 5)}
    )
    assert actions == []
    assert counts == QUIET


def test_below_the_hold_mints_one_per_run():
    """The maintainer's snapshot of 2026-11-04: one instance running, four
    infrastructure servers, one spare — the pool must fill to 5 = 1 + 4."""
    fips = [
        fip("149.165.170.35", "p254", "10.0.0.254"),
        fip("149.165.152.240", "pa", "10.0.0.10"),
        fip("149.165.154.131", "pb", "10.0.0.11"),
        fip("149.165.152.104", "pc", "10.0.0.12"),
        fip("149.165.151.191", "pd", "10.0.0.13"),
        fip("149.165.9.9"),
    ]
    by_fixed = {
        "10.0.0.254": srv(name="morpho-cloud_instance-254"),
        "10.0.0.10": srv(instance=False, name="morphoDepot-server"),
        "10.0.0.11": srv(instance=False, name="morphocloud-instances-runner-2"),
        "10.0.0.12": srv(instance=False, name="morphocloud-instances-runner"),
        "10.0.0.13": srv(instance=False, name="morphocloud-org-server"),
    }
    actions, counts = run_plan(fips, by_fixed)
    assert actions == [("mint",)]
    assert counts == {
        "held": 2,
        "running": 1,
        "spares": 1,
        "target": 5,
        "hold": 5,
        "infra": 4,
        "unknown": 0,
    }


def test_all_in_use_mints_one_ahead():
    fips = [fip(f"149.165.1.{i}", f"p{i}", f"10.0.0.{i}") for i in range(1, 7)]
    actions, counts = run_plan(fips, {f"10.0.0.{i}": srv() for i in range(1, 7)})
    assert actions == [("mint",)]
    assert counts == {
        "held": 6,
        "running": 6,
        "spares": 0,
        "target": 7,
        "hold": 5,
        "infra": 0,
        "unknown": 0,
    }


def test_over_the_hold_releases_idle_spares_oldest_first_down_to_running_plus_one():
    fips = [
        fip(f"149.165.1.{i}", f"p{i}", f"10.0.0.{i}") for i in range(1, 4)
    ]  # 3 running
    fips += [
        fip("149.165.2.1"),
        fip("149.165.2.2"),
        fip("149.165.2.3"),
        fip("149.165.2.4"),
    ]  # 7 held
    ages = {
        "149.165.2.1": 4000,
        "149.165.2.2": 100,
        "149.165.2.3": 9000,
        "149.165.2.4": 700,
    }
    actions, counts = run_plan(
        fips, {f"10.0.0.{i}": srv() for i in range(1, 4)}, ages=ages
    )
    # target max(5, 4) = 5: release two, the idle ones oldest first; the
    # one freed 100 s ago is left alone (an unshelve may be about to bind it).
    assert kinds(actions) == [("delete", "149.165.2.3"), ("delete", "149.165.2.1")]
    assert counts == {
        "held": 7,
        "running": 3,
        "spares": 4,
        "target": 5,
        "hold": 5,
        "infra": 0,
        "unknown": 0,
    }


def test_two_spares_with_the_same_age_release_without_crashing():
    # Test, 2026-11-05: equal timestamps made sorted() compare the address
    # dicts and raise; eleven ticks died silently.
    fips = [fip("149.165.1.1"), fip("149.165.1.2"), fip("149.165.1.3")]
    actions, counts = run_plan(
        fips, hold=1, ages={"149.165.1.1": 900, "149.165.1.2": 900, "149.165.1.3": 900}
    )
    assert [a[0] for a in actions] == ["delete", "delete"]
    assert counts["held"] == len(fips)


def test_shelved_instance_loses_its_address_and_it_counts_as_spare():
    fips = [fip("149.165.1.1", "p1", "10.0.0.1"), fip("149.165.1.2", "p2", "10.0.0.2")]
    actions, counts = run_plan(
        fips,
        {"10.0.0.1": srv(), "10.0.0.2": srv("SHELVED_OFFLOADED")},
        ages={"149.165.1.2": 900},  # bound long ago: a leftover, not an unshelve
    )
    assert kinds(actions) == [("unset", "149.165.1.2"), ("mint",)]
    assert counts["running"] == 1
    assert counts["spares"] == 1
    # Decision 35: the release names the instance, so the member can be told.
    unset = next(a for a in actions if a[0] == "unset")
    assert unset[4] == "morpho-cloud_instance-7"


def test_an_in_flight_task_blocks_the_release_even_of_an_old_binding():
    # Both guards are needed, not either: a hung unshelve (Instances #214 sat
    # in task_state spawning for 26 minutes) outlives the ten-minute window.
    fips = [fip("149.165.1.1", "p1", "10.0.0.1")]
    actions, counts = run_plan(
        fips,
        {"10.0.0.1": srv("SHELVED_OFFLOADED", task="spawning")},
        hold=1,
        ages={"149.165.1.1": 1560},
    )
    assert kinds(actions) == [("mint",)]
    assert counts["running"] == 1


def test_an_unshelve_in_flight_keeps_its_address():
    # Instances #214 (2026-11-04): production binds the address, then
    # unshelves; the boot hung with task_state "spawning" and the address
    # was released from under it. In flight = counted as running, untouched.
    fips = [fip("149.165.1.1", "p1", "10.0.0.1")]
    actions, counts = run_plan(
        fips, {"10.0.0.1": srv("SHELVED_OFFLOADED", task="spawning")}, hold=1
    )
    assert kinds(actions) == [("mint",)]  # 1 running + 1 ahead
    assert counts["running"] == 1
    assert counts["spares"] == 0


def test_an_address_just_bound_to_a_shelved_instance_is_left_for_ten_minutes():
    # Between "add floating ip" and "server unshelve" nothing is in flight;
    # the binding's age tells that window from a leftover.
    fips = [fip("149.165.1.1", "p1", "10.0.0.1")]
    fresh = {"149.165.1.1": 180}
    actions, counts = run_plan(
        fips, {"10.0.0.1": srv("SHELVED_OFFLOADED")}, hold=1, ages=fresh
    )
    assert kinds(actions) == [("mint",)]
    assert counts["running"] == 1
    old = {"149.165.1.1": 900}
    actions, counts = run_plan(
        fips, {"10.0.0.1": srv("SHELVED_OFFLOADED")}, hold=1, ages=old
    )
    assert kinds(actions) == [("unset", "149.165.1.1")]
    assert counts["spares"] == 1


def test_a_release_note_goes_through_admin_alert(monkeypatch, tmp_path):
    # Decision 35: the note is a dispatch of admin-alert.yml with the issue
    # number read from the instance's name.
    token = tmp_path / "pat"
    token.write_text("gho_test")
    monkeypatch.setattr(police, "TOKEN_FILE", token)
    monkeypatch.setattr(police, "REPO", "MorphoCloud/Instances")
    calls = []

    class Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        police.subprocess,
        "run",
        lambda args, **_kw: (calls.append(list(args)), Done())[1],
    )
    police.note_issue("morpho-cloud_instance-7", "released")
    assert len(calls) == 1
    assert calls[0][:4] == ["gh", "workflow", "run", "admin-alert.yml"]
    assert "issue=7" in calls[0]
    assert "note=released" in calls[0]
    # Not an instance name: nothing is dispatched.
    police.note_issue("notinstance-5", "released")
    assert len(calls) == 1


def test_infrastructure_is_never_touched_even_when_shelved_or_unknown():
    fips = [
        fip("149.165.1.1", "p1", "10.0.0.1"),  # a shelved runner: not ours
        fip("149.165.1.2", "p2", "10.0.0.2"),  # a port no listed server owns
    ]
    actions, counts = run_plan(
        fips, {"10.0.0.1": srv("SHELVED_OFFLOADED", instance=False)}, hold=0
    )
    assert actions == [("mint",)]  # nothing of ours is held at all: 0 < 1
    assert counts == {
        "held": 0,
        "running": 0,
        "spares": 0,
        "target": 1,
        "hold": 0,
        "infra": 1,
        "unknown": 1,
    }


def test_non_routable_addresses_are_deleted_or_reported():
    fips = [
        fip("172.17.20.187"),
        fip("172.17.21.23", "p2", "10.0.0.2"),
        fip("172.17.21.24", "p3", "10.0.0.3"),
        fip("172.17.21.25", "p4", "10.0.0.4"),
        fip("149.165.1.1"),
    ]
    actions, counts = run_plan(
        fips,
        {
            "10.0.0.2": srv("SHELVED_OFFLOADED"),
            "10.0.0.3": srv(),
            "10.0.0.4": srv(instance=False),  # a runner on a dead address: reported too
        },
        hold=1,
        ages={"172.17.21.23": 900},
    )
    assert kinds(actions) == [
        ("delete", "172.17.20.187"),
        ("unset", "172.17.21.23"),
        ("delete", "172.17.21.23"),
        ("alert", "nonroutable:172.17.21.24"),
        ("alert", "nonroutable:172.17.21.25"),
    ]
    assert counts == {
        "held": 1,
        "running": 0,
        "spares": 1,
        "target": 1,
        "hold": 1,
        "infra": 1,  # the runner on the dead address
        "unknown": 0,
    }


def test_the_drought_email_says_what_is_in_hand():
    zero = police.drought_body("Dry.", {"spares": 0, "running": 3})
    assert "In hand now: 0 spare addresses beside 3 running instances." in zero
    assert "The next create or unshelve fails for want of an address" in zero
    one = police.drought_body("Dry.", {"spares": 1, "running": 1})
    assert "1 spare address beside 1 running instance." in one
    assert "takes the spare; the one after that fails" in one
    two = police.drought_body("Dry.", {"spares": 2, "running": 3})
    assert "The next 2 creates or unshelves take the spares" in two
    for body in (zero, one, two):
        assert "a shelve returns its address to the spares" in body
        assert "You get another email when Jetstream2 has addresses again." in body
        assert "from now on" not in body


class FakeJS2:
    """Jetstream2 handing out `answers` in turn; records deletes and emails."""

    def __init__(self, monkeypatch, answers, send_ok=True):
        self.answers = list(answers)
        self.deleted, self.sent = [], []
        monkeypatch.setattr(police, "os_json", self.create)
        monkeypatch.setattr(police, "os_run", lambda *a: self.deleted.append(a[-1]))
        monkeypatch.setattr(
            police, "send_alert", lambda s, b: (self.sent.append((s, b)), send_ok)[1]
        )

    def create(self, *_args):
        ip = self.answers.pop(0)
        return {"floating_ip_address": ip, "id": f"id-{ip}"}


def test_a_non_routable_mint_alerts_once_with_the_counts(monkeypatch):
    js2 = FakeJS2(monkeypatch, ["172.17.21.165", "172.17.21.166"])
    state = {}
    police.mint(state, {"spares": 0, "running": 1})
    police.mint(state, {"spares": 0, "running": 1})
    assert js2.deleted == ["id-172.17.21.165", "id-172.17.21.166"]
    assert len(js2.sent) == 1
    assert js2.sent[0][0].endswith("(non-routable address)")
    assert "0 spare addresses beside 1 running instance" in js2.sent[0][1]
    assert "mint-failed" in state


def test_a_good_mint_after_a_drought_sends_the_all_clear(monkeypatch):
    js2 = FakeJS2(monkeypatch, ["149.165.1.9"])
    state = {"mint-failed": NOV_4_2026}
    police.mint(state, {"spares": 0, "running": 1})
    assert js2.deleted == []  # kept: it is the spare that was due
    assert [s for s, _ in js2.sent] == ["Jetstream2 has public addresses again"]
    assert "first seen 2026-11-04 00:00 UTC" in js2.sent[0][1]
    assert state == {}


def test_the_probe_runs_hourly_only_during_a_drought_and_returns_what_it_gets(
    monkeypatch,
):
    js2 = FakeJS2(monkeypatch, ["172.17.21.1", "149.165.1.9"])
    police.probe({}, NOW)  # no drought: nothing asked
    assert js2.answers == ["172.17.21.1", "149.165.1.9"]
    state = {"mint-failed": NOV_4_2026}
    police.probe(state, NOW)  # still dry
    assert js2.deleted == ["id-172.17.21.1"]
    assert js2.sent == []
    police.probe(state, NOW + 600)  # within the hour: not asked again
    assert js2.answers == ["149.165.1.9"]
    police.probe(state, NOW + 3600)  # addresses again: handed back, all clear
    assert js2.deleted == ["id-172.17.21.1", "id-149.165.1.9"]
    assert [s for s, _ in js2.sent] == ["Jetstream2 has public addresses again"]
    assert state == {}


def test_an_all_clear_that_cannot_be_sent_keeps_the_drought_open(monkeypatch):
    FakeJS2(monkeypatch, ["149.165.1.9"], send_ok=False)
    state = {"mint-failed": NOV_4_2026, "mint-probe-at": NOW - 7200}
    police.probe(state, NOW)
    assert state == {"mint-failed": NOV_4_2026, "mint-probe-at": NOW}
