"""The storage-term sweep and the long-shelved scrub
(.github/actions/expire-shares/run.py and scrub.py; STORAGE_REDESIGN_PLAN.md
decisions 7 and 21), with the runner, GitHub and mail faked.
Run: python -m pytest scripts/test_storage_term_sweep.py"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent / ".github" / "actions" / "expire-shares"
sys.path.insert(0, str(HERE))
common = importlib.import_module("common")
run = importlib.import_module("run")
scrub = importlib.import_module("scrub")

PLACEHOLDERS = " ".join(
    "{{" + p + "}}"
    for p in (
        "kept_until",
        "delete_on",
        "days_left",
        "note_line",
        "link",
        "reason",
        "instance_name",
    )
)
KINDS = (
    "reminder",
    "locked",
    "deleted",
    "approved",
    "declined",
    "admin_reminder",
    "shelved_deleted",
)
TPL = {
    f"{kind}_{part}": f"{kind}_{part} {PLACEHOLDERS}"
    for kind in KINDS
    for part in ("subject", "body")
}
TPL["contact_email"] = "portal@morphocloud.org"
ISSUE, TEST_ISSUE, OTHER_ISSUE = 278, 9, 5
NO_REQUESTS = {"waiting": [], "stale": [], "decided": []}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Records every runner call, mail and delete dispatch."""
    calls = {"runner": [], "mail": [], "delete": []}
    answers = {}

    def runner(*args, **_kw):
        calls["runner"].append(args)
        return answers.get(args[0], {})

    def send(to, subject, body):
        calls["mail"].append((to, subject, body))

    monkeypatch.setattr(common, "runner", runner)
    monkeypatch.setattr(common, "send", send)
    monkeypatch.setattr(common, "email_for", lambda login: f"{login}@example.org")
    monkeypatch.setattr(
        common, "login_for", lambda gid, recorded: recorded or f"u{gid}"
    )
    monkeypatch.setattr(common, "dispatch_delete", calls["delete"].append)
    for k, v in {
        "TERM_DAYS": "120",
        "WARN_DAYS": "14,7,1",
        "GRACE_DAYS": "15",
        "PORTAL_URL": "https://portal.morphocloud.org",
        "LOOKUP_URL": "https://join.example/lookup",
        "LOOKUP_API_KEY": "k",
        "ADMIN_EMAILS": "admins@example.org",
        "MAIL_USERNAME": "u",
        "MAIL_PASSWORD": "p",
        "INSTANCE_PREFIX": "",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
    }.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "tpl.json").write_text(json.dumps(TPL))
    monkeypatch.setenv("TEMPLATE_FILE", str(tmp_path / "tpl.json"))
    calls["tmp"], calls["answers"] = tmp_path, answers
    return calls


def share(verdict, **more):
    a = {
        "github_id": 11,
        "login": "alice",
        "name": "mc-user-11",
        "verdict": verdict,
        "end_date": "2027-03-23",
        "days_to_end": 7,
        "delete_on": None,
        "locked_at": None,
        "mounted_by": None,
    }
    a.update(more)
    return a


def sweep(world, plan, requests=None):
    tmp = world["tmp"]
    (tmp / "plan.json").write_text(json.dumps(plan))
    (tmp / "req.json").write_text(json.dumps(requests or NO_REQUESTS))
    os.environ["PLAN_FILE"] = str(tmp / "plan.json")
    os.environ["REQUESTS_FILE"] = str(tmp / "req.json")
    return run.main()


def failures(world) -> str:
    out = world["tmp"] / "out"
    return out.read_text() if out.exists() else ""


# --- the daily sweep -------------------------------------------------------------


def test_reminder_mails_then_marks(world):
    assert sweep(world, [share("remind:7")]) == 0
    to, subject, _body = world["mail"][0]
    assert to == "alice@example.org"
    assert subject.startswith("reminder_subject 23 Mar 2027")
    assert ("mark", "11", "--reminded", "7", "--term-days", "120") in world["runner"]


def test_keep_hold_and_locked_do_nothing(world):
    assert sweep(world, [share("keep"), share("hold"), share("locked")]) == 0
    assert world["mail"] == []
    assert world["runner"] == []
    assert world["delete"] == []


def test_past_the_end_the_instance_is_deleted_first(world):
    assert sweep(world, [share("delete-instance", mounted_by="instance-278")]) == 0
    assert world["delete"] == [ISSUE]
    assert world["mail"] == []
    assert not any(c[0] == "lock" for c in world["runner"])


def test_an_unknown_holder_is_a_failure_not_a_guess(world):
    plan = [share("delete-instance", mounted_by="legacy-instance-9")]
    assert sweep(world, plan) == 1
    assert world["delete"] == []


def test_lock_then_tell_the_member_the_deletion_date(world):
    world["answers"]["lock"] = {"delete_on": "2027-04-08"}
    assert sweep(world, [share("lock", days_to_end=-1)]) == 0
    assert world["runner"][0][:2] == ("lock", "11")
    assert "--grace-days" in world["runner"][0]
    assert world["mail"][0][1].startswith("locked_subject 23 Mar 2027 8 Apr 2027")


def test_delete_mails_the_member_and_fails_loudly_if_unrecorded(world):
    world["answers"]["expire"] = {"portal_recorded": True}
    assert sweep(world, [share("delete")]) == 0
    assert world["mail"][0][1].startswith("deleted_subject")
    world["answers"]["expire"] = {"portal_recorded": False}
    assert sweep(world, [share("delete")]) == 1


def test_one_failure_does_not_stop_the_rest(world, monkeypatch):
    def broken(_login):
        msg = "lookup down"
        raise RuntimeError(msg)

    monkeypatch.setattr(common, "email_for", broken)
    second = share("delete-instance", mounted_by="instance-5", github_id=12)
    assert sweep(world, [share("remind:14"), second]) == 1
    assert world["delete"] == [OTHER_ISSUE]
    assert "lookup down" in failures(world)


def test_admins_are_reminded_and_members_hear_the_decisions(world):
    stale = {
        "request_id": "4",
        "github_id": 11,
        "login": "alice",
        "reason": "Need it for a paper.",
        "created_at": "2027-03-10T10:00:00Z",
    }
    approved = {
        "request_id": "5",
        "github_id": 12,
        "login": "bob",
        "decision": "approved",
        "note": "",
        "kept_until": "2027-07-21",
    }
    declined = {
        "request_id": "6",
        "github_id": 13,
        "login": "carol",
        "decision": "declined",
        "note": "Course is over.",
        "kept_until": "2027-03-23",
    }
    requests = {"waiting": [11], "stale": [stale], "decided": [approved, declined]}
    assert sweep(world, [], requests) == 0
    to_admin, to_bob, to_carol = world["mail"]
    assert to_admin[0] == "admins@example.org"
    assert "https://portal.morphocloud.org/admin/requests/4" in to_admin[2]
    assert "Need it for a paper." in to_admin[2]
    assert to_bob[0] == "bob@example.org"
    assert to_bob[1].startswith("approved_subject 21 Jul 2027")
    assert to_carol[0] == "carol@example.org"
    assert "Course is over." in to_carol[2]
    for rid, who in (("4", "admin"), ("5", "member"), ("6", "member")):
        assert ("portal-mailed", rid, who) in world["runner"]


def test_an_error_verdict_is_reported(world):
    assert sweep(world, [share("error", error="unreadable end date")]) == 1


# --- the weekly scrub ----------------------------------------------------------------


def test_scrub_deletes_long_shelved_member_instances_only(world, monkeypatch):
    world["answers"]["stale-shelved"] = [
        {
            "name": "instance-278",
            "issue": 278,
            "shelved_at": "2027-01-01T00:00:00Z",
            "days": 30,
        },
        {
            "name": "instance-279",
            "issue": 279,
            "shelved_at": None,
            "days": None,
            "error": "no shelve",
        },
        {
            "name": "instance-280",
            "issue": 280,
            "shelved_at": "2027-01-01T00:00:00Z",
            "days": 30,
        },
    ]
    world["answers"]["info"] = {"end_date": "2027-03-23", "locked": False}
    labels = {278: ["request-creator:user"], 280: ["request-creator:workshop"]}
    monkeypatch.setattr(scrub, "issue_facts", lambda n: ("alice", labels[n], "OPEN"))
    monkeypatch.setattr(scrub, "github_id", lambda _login: 11)
    monkeypatch.setenv("SHELVED_DAYS", "28")
    assert scrub.main() == 1  # 279: its age is unknown, reported
    assert world["delete"] == [ISSUE]  # not 279 (unknown age), not 280 (not a member's)
    to, _subject, body = world["mail"][0]
    assert to == "alice@example.org"
    assert "instance-278" in body
    assert "23 Mar 2027" in body
    assert "unknown" in failures(world)


def test_issue_number_from_an_instance_name(monkeypatch):
    monkeypatch.setenv("INSTANCE_PREFIX", "")
    assert common.issue_of("instance-278") == ISSUE
    assert common.issue_of("legacy-instance-278") is None
    monkeypatch.setenv("INSTANCE_PREFIX", "morpho-cloud-test")
    assert common.issue_of("morpho-cloud-test_instance-9") == TEST_ISSUE
    assert common.issue_of("instance-9") is None


def test_no_admin_list_stops_only_the_admin_reminders(world, monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS")
    stale = {
        "request_id": "4",
        "github_id": 11,
        "login": "alice",
        "reason": "Need it for a paper.",
        "created_at": "2027-03-10T10:00:00Z",
    }
    requests = {"waiting": [11], "stale": [stale], "decided": []}
    assert sweep(world, [share("remind:7")], requests) == 1
    assert len(world["mail"]) == 1  # the member's reminder still went out
    assert "ADMIN_EMAILS is not set" in failures(world)
    assert sweep(world, [share("remind:7")]) == 0  # nothing to remind: no failure
