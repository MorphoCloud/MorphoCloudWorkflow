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


def fip(ip, port=None, fixed=None, fid=None):
    return {
        "ID": fid or f"id-{ip}",
        "Floating IP Address": ip,
        "Port": port,
        "Fixed IP Address": fixed,
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


NOV_4_2026 = 1793750400.0  # 2026-11-04T00:00:00Z


def test_a_missing_timestamp_is_never_idle():
    assert police.epoch(None) >= police.time.time() - 1
    assert police.epoch("garbage") >= police.time.time() - 1
    assert police.epoch("2026-11-04T00:00:00Z") == NOV_4_2026


def test_quiet_project_within_the_hold_does_nothing():
    fips = [
        fip("149.165.1.1", "p1", "10.0.0.1"),
        fip("149.165.1.2"),
        fip("149.165.1.3"),
    ]
    actions, counts = run_plan(fips, {"10.0.0.1": "ACTIVE"})
    assert actions == []
    assert counts == {"held": 3, "running": 1, "spares": 2, "target": 5, "hold": 5}


def test_no_spare_left_mints_one_ahead():
    fips = [fip(f"149.165.1.{i}", f"p{i}", f"10.0.0.{i}") for i in range(1, 7)]
    actions, counts = run_plan(fips, {f"10.0.0.{i}": "ACTIVE" for i in range(1, 7)})
    assert actions == [("mint",)]
    assert counts == {"held": 6, "running": 6, "spares": 0, "target": 7, "hold": 5}


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
        fips, {f"10.0.0.{i}": "ACTIVE" for i in range(1, 4)}, ages=ages
    )
    # target max(5, 4) = 5: release two, the idle ones oldest first; the
    # one freed 100 s ago is left alone (an unshelve may be about to bind it).
    assert kinds(actions) == [("delete", "149.165.2.3"), ("delete", "149.165.2.1")]
    assert counts == {"held": 7, "running": 3, "spares": 4, "target": 5, "hold": 5}


def test_the_hold_floor_keeps_spares_when_quiet():
    fips = [fip("149.165.1.1", "p1", "10.0.0.1")] + [
        fip(f"149.165.2.{i}") for i in range(1, 5)
    ]
    actions, counts = run_plan(
        fips, {"10.0.0.1": "ACTIVE"}, ages={f"149.165.2.{i}": 9999 for i in range(1, 5)}
    )
    assert actions == []  # exactly N: nothing to release
    assert counts == {"held": 5, "running": 1, "spares": 4, "target": 5, "hold": 5}


def test_shelved_server_loses_its_address_and_it_counts_as_spare():
    fips = [fip("149.165.1.1", "p1", "10.0.0.1"), fip("149.165.1.2", "p2", "10.0.0.2")]
    actions, counts = run_plan(
        fips, {"10.0.0.1": "ACTIVE", "10.0.0.2": "SHELVED_OFFLOADED"}
    )
    assert kinds(actions) == [("unset", "149.165.1.2")]
    assert counts == {"held": 2, "running": 1, "spares": 1, "target": 5, "hold": 5}


def test_non_routable_addresses_are_deleted_or_reported():
    fips = [
        fip("172.17.20.187"),
        fip("172.17.21.23", "p2", "10.0.0.2"),
        fip("172.17.21.24", "p3", "10.0.0.3"),
        fip("149.165.1.1"),
    ]
    actions, counts = run_plan(
        fips, {"10.0.0.2": "SHELVED_OFFLOADED", "10.0.0.3": "ACTIVE"}
    )
    assert kinds(actions) == [
        ("delete", "172.17.20.187"),
        ("unset", "172.17.21.23"),
        ("delete", "172.17.21.23"),
        ("alert", "nonroutable:172.17.21.24"),
    ]
    assert counts == {"held": 1, "running": 0, "spares": 1, "target": 5, "hold": 5}
