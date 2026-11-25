"""The parts of network-security.py that decide (NETWORK_SECURITY_PLAN.md)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "network_security", SCRIPTS / "network-security.py"
)
ns = importlib.util.module_from_spec(spec)
sys.modules["network_security"] = ns
spec.loader.exec_module(ns)

PROXY = "149.165.159.111"


def row(*fields):
    rid, direction, ether, proto, ports, ip_range = fields
    return {
        "ID": rid,
        "Direction": direction,
        "Ethertype": ether,
        "IP Protocol": proto,
        "Port Range": ports,
        "IP Range": ip_range,
        "Remote Security Group": None,
    }


def test_parse_rule_reads_openstack_rows():
    assert ns.parse_rule(row("a", "ingress", "IPv4", "tcp", "22:22", "0.0.0.0/0")) == (
        "a",
        ns.tcp_in(22),
    )
    # Default egress rules come back with no protocol, no ports, no range.
    assert ns.parse_rule(row("b", "egress", "IPv4", None, "", None))[1] == ns.EGRESS4
    assert ns.parse_rule(row("c", "egress", "IPv6", None, "", None))[1].remote == "::/0"
    assert ns.parse_rule(
        row("d", "ingress", "IPv4", "udp", "60000:61000", "0.0.0.0/0")
    )[1] == ns.Rule("ingress", "IPv4", "udp", 60000, 61000, "0.0.0.0/0")


def test_new_group_keeps_ipv4_egress_and_drops_ipv6_egress():
    # A group OpenStack just created holds the two default egress rules.
    current = [
        ns.parse_rule(row("e4", "egress", "IPv4", None, "", None)),
        ns.parse_rule(row("e6", "egress", "IPv6", None, "", None)),
    ]
    want = ns.GROUPS["mc-instance"][1] + ns.proxy_rules({PROXY})
    add, delete = ns.plan_group(current, want)
    assert ns.EGRESS4 not in add  # kept, never removed and re-added
    assert [rid for rid, _ in delete] == ["e6"]
    assert ns.tcp_in(49528, f"{PROXY}/32") in add
    assert ns.tcp_in(22) in add
    assert ns.tcp_in(5901) in add


def test_plan_group_removes_rules_added_by_hand_and_duplicates():
    want = ns.GROUPS["mc-join"][1]
    current = [("1", r) for r in want] + [
        ("x", ns.tcp_in(8888)),
        ("dup", ns.tcp_in(22)),
    ]
    add, delete = ns.plan_group(current, want)
    assert add == []
    assert sorted(rid for rid, _ in delete) == ["dup", "x"]


def test_instance_group_never_opens_more_than_the_plan():
    rules = ns.GROUPS["mc-instance"][1] + ns.proxy_rules({PROXY})
    ingress = {
        (r.protocol, r.port_min, r.remote) for r in rules if r.direction == "ingress"
    }
    assert ingress == {
        ("tcp", 22, "0.0.0.0/0"),
        ("tcp", 5901, "0.0.0.0/0"),
        ("icmp", None, "0.0.0.0/0"),
        ("tcp", 49528, f"{PROXY}/32"),
    }


@pytest.mark.parametrize(
    ("addresses", "ok"),
    [
        ({PROXY}, True),
        ({PROXY, "149.165.159.112"}, True),
        (set(), False),  # no answer
        ({"203.0.113.5"}, False),  # outside Jetstream2
        ({PROXY, "10.0.0.1"}, False),  # one outside spoils the answer
        ({f"149.165.1.{i}" for i in range(5)}, False),  # more than 4
    ],
)
def test_validate(addresses, ok):
    assert (ns.validate(addresses) is None) is ok


def test_follow_adds_a_new_address_at_once():
    add, remove, state = ns.follow({PROXY, "149.165.1.2"}, {PROXY}, ns.State())
    assert add == {"149.165.1.2"}
    assert remove == set()
    assert state.failures == 0


def test_follow_removes_a_missing_address_only_after_three_runs():
    state = ns.State()
    current = {PROXY, "149.165.1.2"}
    for run in (1, 2):
        add, remove, state = ns.follow({PROXY}, current, state)
        assert remove == set(), run
        assert state.missing == {"149.165.1.2": run}
    add, remove, state = ns.follow({PROXY}, current, state)
    assert remove == {"149.165.1.2"}
    assert state.missing == {}


def test_follow_forgets_the_count_when_the_address_comes_back():
    current = {PROXY, "149.165.1.2"}
    _, _, state = ns.follow({PROXY}, current, ns.State())
    _, _, state = ns.follow(current, current, state)  # rotating answer: back again
    assert state.missing == {}
    _, remove, state = ns.follow({PROXY}, current, state)
    assert remove == set()
    assert state.missing == {"149.165.1.2": 1}


def test_follow_with_no_rule_adds_one_and_reports_no_problem():
    add, remove, state = ns.follow({PROXY}, set(), ns.State(failures=2))
    assert add == {PROXY}
    assert remove == set()
    assert state.failures == 0  # a good run resets the problem counter


def test_problem_alert_after_three_runs_then_daily():
    day = ns.REALERT_SECONDS
    assert not ns.problem_alert_due(ns.State(failures=1), 10 * day)
    assert not ns.problem_alert_due(ns.State(failures=2), 10 * day)
    assert ns.problem_alert_due(ns.State(failures=3), 10 * day)
    # Alerted an hour ago: quiet until a day has passed.
    assert not ns.problem_alert_due(
        ns.State(failures=4, last_alert=10 * day - 3600), 10 * day
    )
    assert ns.problem_alert_due(ns.State(failures=30, last_alert=9 * day), 10 * day)


def test_state_file_missing_or_corrupt_counts_as_empty(tmp_path):
    assert ns.State.load(tmp_path / "nope.json") == ns.State()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ns.State.load(bad) == ns.State()
    bad.write_text('["a list"]')
    assert ns.State.load(bad) == ns.State()
    good = tmp_path / "good.json"
    ns.State({"149.165.1.2": 2}, 1, 5.0).save(good)
    assert ns.State.load(good) == ns.State({"149.165.1.2": 2}, 1, 5.0)


def test_proxy_check_does_nothing_before_the_group_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "HOME", tmp_path)
    monkeypatch.setattr(ns, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(ns, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ns, "group_id", lambda *_: None)
    monkeypatch.setattr(ns, "resolve", lambda *_: pytest.fail("must not resolve"))
    assert ns.cmd_proxy_check() == 0
    assert not (tmp_path / "state.json").exists()


def test_proxy_check_counts_a_failed_call_as_a_problem_run(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(ns, "HOME", tmp_path)
    monkeypatch.setattr(ns, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(ns, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(
        ns, "group_id", lambda *_: (_ for _ in ()).throw(RuntimeError("api down"))
    )
    sent = []
    monkeypatch.setattr(ns, "email", lambda s, *_: sent.append(s) or True)
    for _ in range(ns.FAIL_RUNS):
        assert ns.cmd_proxy_check() == 0
    assert ns.State.load(tmp_path / "state.json").failures == ns.FAIL_RUNS
    assert len(sent) == 1  # the third run alerts, the first two do not
    assert "nothing changed" in capsys.readouterr().out


def test_proxy_check_updates_rules_and_emails(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "HOME", tmp_path)
    monkeypatch.setattr(ns, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(ns, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ns, "group_id", lambda *_: "gid")
    monkeypatch.setattr(ns, "resolve", lambda *_: {"149.165.1.9"})
    monkeypatch.setattr(
        ns,
        "group_rules",
        lambda *_: [("old", ns.tcp_in(49528, f"{PROXY}/32")), ("s", ns.tcp_in(22))],
    )
    created, deleted, sent = [], [], []
    monkeypatch.setattr(ns, "create_rule", lambda _gid, r: created.append(r))
    monkeypatch.setattr(ns, "delete_rule", lambda rid: deleted.append(rid))
    monkeypatch.setattr(ns, "email", lambda s, *_: sent.append(s) or True)
    for run in range(1, ns.MISSING_RUNS + 1):
        ns.cmd_proxy_check()
        # The new address is added on the first run (the fake keeps returning
        # the old rule list, so it is "added" each run in this test).
        assert ns.tcp_in(49528, "149.165.1.9/32") in created
        assert deleted == ([] if run < ns.MISSING_RUNS else ["old"])
    assert len(sent) == ns.MISSING_RUNS


def test_a_rule_from_another_group_is_not_an_anywhere_rule():
    r = row("g", "ingress", "IPv4", "tcp", "22:22", None)
    r["Remote Security Group"] = "abc123"
    _, rule = ns.parse_rule(r)
    assert rule != ns.tcp_in(22)
    add, delete = ns.plan_group([("g", rule)], [ns.tcp_in(22)])
    assert add == [ns.tcp_in(22)]
    assert [rid for rid, _ in delete] == ["g"]


def test_proxy_rules_now_separates_strays():
    rules = [
        ("p", ns.tcp_in(49528, f"{PROXY}/32")),
        ("dup", ns.tcp_in(49528, f"{PROXY}/32")),
        ("wide", ns.tcp_in(49528, "0.0.0.0/0")),
        ("v6", ns.Rule("ingress", "IPv6", "tcp", 49528, 49528, "::/0")),
        ("ssh", ns.tcp_in(22)),
    ]
    mine, strays = ns.proxy_rules_now(rules)
    assert mine == {PROXY: "p"}
    assert sorted(rid for rid, _ in strays) == ["dup", "v6", "wide"]


def _fake_world(monkeypatch, tmp_path, rules, delete_fails=False):
    monkeypatch.setattr(ns, "HOME", tmp_path)
    monkeypatch.setattr(ns, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(ns, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ns, "group_id", lambda *_: "gid")
    monkeypatch.setattr(ns, "resolve", lambda *_: {PROXY})
    monkeypatch.setattr(ns, "group_rules", lambda *_: rules)
    log = {"created": [], "deleted": [], "sent": []}

    def delete(rid):
        if delete_fails:
            msg = "api down"
            raise RuntimeError(msg)
        log["deleted"].append(rid)

    monkeypatch.setattr(ns, "create_rule", lambda _gid, r: log["created"].append(r))
    monkeypatch.setattr(ns, "delete_rule", delete)
    monkeypatch.setattr(ns, "email", lambda s, b: log["sent"].append((s, b)) or True)
    return log


def test_proxy_check_removes_a_stray_rule_and_emails(tmp_path, monkeypatch):
    log = _fake_world(
        monkeypatch,
        tmp_path,
        [("p", ns.tcp_in(49528, f"{PROXY}/32")), ("w", ns.tcp_in(49528))],
    )
    ns.cmd_proxy_check()
    assert log["deleted"] == ["w"]
    assert len(log["sent"]) == 1
    assert "not the proxy's" in log["sent"][0][1]


def test_a_change_is_emailed_even_when_a_later_call_fails(tmp_path, monkeypatch):
    # The new address is added, then removing the stray fails: the addition
    # must still be reported, and the run counts as a problem run.
    log = _fake_world(
        monkeypatch, tmp_path, [("w", ns.tcp_in(49528))], delete_fails=True
    )
    ns.cmd_proxy_check()
    assert log["created"] == [ns.tcp_in(49528, f"{PROXY}/32")]
    assert len(log["sent"]) == 1
    assert f"added {PROXY}" in log["sent"][0][1]
    assert "api down" in log["sent"][0][1]
    assert ns.State.load(tmp_path / "state.json").failures == 1


def test_a_good_run_ends_the_problem_episode():
    _, _, state = ns.follow({PROXY}, {PROXY}, ns.State(failures=5, last_alert=123.0))
    assert state.failures == 0
    assert state.last_alert == 0.0


def test_proxy_rules_now_catches_wide_and_portless_rules_but_not_icmp():
    rules = [
        ("p", ns.tcp_in(49528, f"{PROXY}/32")),
        ("range", ns.Rule("ingress", "IPv4", "tcp", 49000, 50000, "0.0.0.0/0")),
        ("alltcp", ns.Rule("ingress", "IPv4", "tcp", None, None, "0.0.0.0/0")),
        ("any", ns.Rule("ingress", "IPv4", None, None, None, "0.0.0.0/0")),
        ("icmp", ns.ICMP_IN),
        ("vnc", ns.tcp_in(5901)),
        ("out", ns.EGRESS4),
    ]
    mine, strays = ns.proxy_rules_now(rules)
    assert mine == {PROXY: "p"}
    assert sorted(rid for rid, _ in strays) == ["alltcp", "any", "range"]


def test_a_rule_call_that_fails_every_run_reaches_the_alert(tmp_path, monkeypatch):
    # The proxy needs a rule; creating it fails every hour (quota, permission).
    log = _fake_world(monkeypatch, tmp_path, [])

    def create_fails(*_):
        msg = "quota exceeded"
        raise RuntimeError(msg)

    monkeypatch.setattr(ns, "create_rule", create_fails)
    for _ in range(ns.FAIL_RUNS):
        ns.cmd_proxy_check()
    assert ns.State.load(tmp_path / "state.json").failures == ns.FAIL_RUNS
    assert len(log["sent"]) == 1
    assert "quota exceeded" in log["sent"][0][1]


def test_parse_rule_on_a_real_openstack_row():
    # Captured from `openstack security group rule list mc-portal -f json`
    # (BIO180006, 2026-11-25): the remote-group keys are present and null.
    real = {
        "ID": "02d4fdd1-f585-401e-b4e0-46110fed2d7e",
        "IP Protocol": "tcp",
        "Ethertype": "IPv4",
        "IP Range": "149.165.149.15/32",
        "Port Range": "22:22",
        "Direction": "ingress",
        "Remote Security Group": None,
        "Remote Address Group": None,
    }
    assert ns.parse_rule(real)[1] == ns.tcp_in(22, "149.165.149.15/32")
    real["Remote Security Group"] = "8f0c"
    real["IP Range"] = None
    assert ns.parse_rule(real)[1].remote == "group:8f0c"
