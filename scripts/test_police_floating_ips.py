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


def srv(status="ACTIVE", instance=True, name=None):
    return {
        "status": status,
        "instance": instance,
        "name": name
        or ("morpho-cloud_instance-7" if instance else "morphocloud-instances-runner"),
    }


def run_plan(fips, by_fixed=None, hold=5, ages=None):
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


def test_shelved_instance_loses_its_address_and_it_counts_as_spare():
    fips = [fip("149.165.1.1", "p1", "10.0.0.1"), fip("149.165.1.2", "p2", "10.0.0.2")]
    actions, counts = run_plan(
        fips, {"10.0.0.1": srv(), "10.0.0.2": srv("SHELVED_OFFLOADED")}
    )
    assert kinds(actions) == [("unset", "149.165.1.2"), ("mint",)]
    assert counts["running"] == 1
    assert counts["spares"] == 1


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
