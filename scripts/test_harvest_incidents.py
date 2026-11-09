"""The incident harvester (INCIDENT_STORE_PHASE1_DESIGN.md, PR A), no network.
Run: python -m pytest scripts/test_harvest_incidents.py"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent


def load(name: str, module: str):
    spec = importlib.util.spec_from_file_location(module, SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module] = mod
    spec.loader.exec_module(mod)
    return mod


hv = load("harvest-incidents.py", "harvest")
clock = load("check-workflow-clock.py", "clock")
SED = ROOT / ".github" / "actions" / "record-facts" / "redact.sed"


@pytest.fixture(scope="module")
def cfg():
    return hv.load_config(ROOT)


# --- T11: redaction ----------------------------------------------------------------

FIXTURE = """\
token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and github_pat_11AAAAAAA0bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
-----END OPENSSH PRIVATE KEY-----
OS_PASSWORD=hunter2secret
  password: clouds-yaml-secret
"password": "json-secret"
exoPw:Correct-Horse-Battery
vncpasswd=abc123 VNC_PASSWORD: zzz
"access_key": "AQBhZmFjZXM4Zm9vYmFyYmF6Cg=="
alice.smith@example.edu wrote to Bob.Jones@uw.edu
"""


def test_redaction_fixture_comes_out_clean():
    key = b"k" * 32
    red = hv.Redactor(SED, key)
    out, n = red.text(FIXTURE)
    for secret in (
        "ghp_ABCDEFGHIJ",
        "github_pat_11AAAAAAA",
        "b3BlbnNzaC1rZXkt",
        "hunter2secret",
        "clouds-yaml-secret",
        "json-secret",
        "Correct-Horse-Battery",
        "abc123",
        "zzz",
        "AQBhZmFjZXM4",
        "alice.smith@example.edu",
        "Bob.Jones@uw.edu",
    ):
        assert secret not in out, out
    assert "BEGIN OPENSSH PRIVATE KEY" not in out
    assert out.count("[email:") == 2  # noqa: PLR2004 — two addresses in the fixture
    assert n >= 10  # noqa: PLR2004 — one replacement per secret, at least
    # the HMAC is stable across runs and case-insensitive on the address
    again, _ = red.text("Alice.Smith@example.edu")
    assert again in out


def test_redaction_without_key_drops_addresses():
    red = hv.Redactor(SED, None)
    out, _ = red.text("mail bob@example.org now")
    assert out == "mail [email] now"
    assert red.email_hash == "unavailable"


# --- T12: the read-only wrapper -----------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["server", "delete", "x"],
        ["server", "set", "x"],
        ["floating", "ip", "create", "public"],
        ["server", "unshelve", "x"],
    ],
)
def test_wrapper_refuses_writes(args):
    r = subprocess.run(
        ["bash", str(SCRIPTS / "openstack-ro"), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2  # noqa: PLR2004 — the wrapper's refusal exit code
    assert "refused" in r.stderr


def test_harvester_only_calls_the_wrapper():
    src = (SCRIPTS / "harvest-incidents.py").read_text()
    # the only way the harvester names the client is the wrapper constant
    assert re.search(r'"openstack"', src) is None
    assert 'with_name("openstack-ro")' in src
    assert src.count("subprocess.run(\n        [OPENSTACK_RO") == 1


# --- the installer writes the renderer's line ------------------------------------------


def test_installer_crontab_line_matches_renderer():
    installer = (SCRIPTS / "install-harvester.sh").read_text()
    m = re.search(r'LINE="(\*/5 \* \* \* \* .*)"', installer)
    assert m, "install-harvester.sh has no LINE="
    line = (
        m.group(1)
        .replace("$OS_CLOUD_NAME", "{cloud}")
        .replace("$SOURCE_REPO", "{repo}")
        .replace("$STORE_REPO", "{store}")
    )
    rendered = next(
        f"{cron} {cmd}" for cron, cmd in clock.RUNNER_SCRIPTS if "{store}" in cmd
    )
    assert line == rendered


# --- classification -------------------------------------------------------------------


def test_signatures_first_match_wins(cfg):
    assert (
        hv.classify(cfg, "HTTP 502 Bad Gateway from api.github.com", None, "x.yml")
        == "github.api_5xx"
    )
    assert (
        hv.classify(cfg, "No valid host was found", None, "create-instance.yml")
        == "create.no_capacity"
    )
    assert (
        hv.classify(cfg, None, None, "report-dropped-command.yml")
        == "github.command_dropped"
    )
    assert hv.classify(cfg, "something new", "Some step", "x.yml") == "unclassified"
    assert (
        hv.classify(
            cfg, "Process completed with exit code 1.", None, "expire-shares.yml"
        )
        == "share.expire_failed"
    )


def test_api_5xx_only_on_the_error_line(cfg):
    # a github.com URL in the error line is not a 5xx
    assert (
        hv.classify(cfg, "https://github.com/x failed: exit 1", None, "y.yml")
        == "unclassified"
    )


def test_base_result():
    assert hv.base_result("timed_out") == "failure"
    assert hv.base_result("cancelled") == "cancelled"
    assert hv.base_result(None) == "unknown"


def test_skipped_runs_are_counts_only():
    assert not hv.needs_detail("command", "skipped")
    assert not hv.needs_detail("alert", "skipped")
    assert not hv.needs_detail("sweep", "success")
    assert hv.needs_detail("command", "success")
    assert hv.needs_detail("sweep", "failure")
    assert not hv.wants_outcome_line("command", "skipped")
    assert hv.wants_outcome_line("command", "success")
    assert hv.wants_outcome_line("sweep", "cancelled")
    assert not hv.wants_outcome_line("sweep", "success")


def test_finalize_dropped_report_and_alerts(cfg):
    reported = [
        {
            "steps": [
                {
                    "name": "Tell the user the command was dropped",
                    "conclusion": "success",
                }
            ]
        }
    ]
    silent = [
        {
            "steps": [
                {
                    "name": "Tell the user the command was dropped",
                    "conclusion": "skipped",
                }
            ]
        }
    ]
    wf = "report-dropped-command.yml"
    assert hv.finalize(cfg, wf, "alert", "success", reported) == ("dropped", True)
    assert hv.finalize(cfg, wf, "alert", "success", silent) == ("success", False)
    assert hv.finalize(cfg, wf, "alert", "skipped", []) == ("skipped", False)
    assert hv.finalize(cfg, "on-admin-mention.yml", "alert", "success", []) == (
        "success",
        True,
    )
    assert hv.finalize(cfg, "on-admin-mention.yml", "alert", "skipped", []) == (
        "skipped",
        False,
    )
    assert hv.finalize(cfg, "control-instance.yml", "command", "refused", []) == (
        "refused",
        False,
    )
    assert hv.finalize(cfg, "control-instance.yml", "command", "success", []) == (
        "success",
        False,
    )
    assert hv.finalize(cfg, "follow-through.yml", "sweep", "cancelled", []) == (
        "cancelled",
        True,
    )
    assert hv.finalize(cfg, "ci.yml", "other", "failure", []) == ("failure", True)


def test_refusal(cfg):
    assert (
        hv.is_refusal(cfg, {"job": "authorize", "step": "unshelve command"}, "")
        == "job:authorize"
    )
    assert (
        hv.is_refusal(cfg, {"job": "create", "step": "Check authorization"}, "")
        == "Check authorization"
    )
    assert (
        hv.is_refusal(
            cfg, {"job": "create", "step": "Poll"}, "x\nREFUSED reason=maintenance\n"
        )
        == "maintenance"
    )
    assert hv.is_refusal(cfg, {"job": "create", "step": "Poll"}, "no") is None


def test_first_failed_step():
    jobs = [
        {
            "name": "a",
            "conclusion": "success",
            "steps": [{"name": "s", "conclusion": "success", "number": 1}],
        },
        {
            "name": "b",
            "conclusion": "failure",
            "steps": [
                {"name": "ok", "conclusion": "success", "number": 1},
                {
                    "name": "Poll instance until ACTIVE",
                    "conclusion": "failure",
                    "number": 2,
                },
                {"name": "later", "conclusion": "failure", "number": 3},
            ],
        },
    ]
    assert hv.first_failed_step(jobs)["step"] == "Poll instance until ACTIVE"
    assert hv.first_failed_step([jobs[0]]) is None


# --- the log archive ----------------------------------------------------------------------


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "1_control.txt",
            "2026-11-09T00:00:00.0Z line one\n2026-11-09T00:00:01.0Z ##[error]Process completed with exit code 1.\n",
        )
        zf.writestr("0_authorize.txt", "2026-11-09T00:00:00.0Z auth ok\n")
        zf.writestr("control/system.txt", "sys\n")
        zf.writestr(
            "control/3_Poll instance until ACTIVE.txt",
            "".join(f"2026-11-09T00:00:0{i % 10}.0Z poll {i}\n" for i in range(250))
            + "2026-11-09T00:00:09.0Z ##[error]Timed out waiting for ACTIVE (unshelve)\n",
        )
    return buf.getvalue()


def test_assemble_log_order_tail_and_error_line():
    whole, tail, error = hv.assemble_log(
        make_zip(),
        {"job": "control", "step": "Poll instance until ACTIVE", "number": 3},
    )
    assert whole.index("===== 0_authorize.txt =====") < whole.index(
        "===== 1_control.txt ====="
    )
    assert tail.count("\n") == hv.TAIL_LINES
    assert tail.endswith("##[error]Timed out waiting for ACTIVE (unshelve)\n")
    assert error == "##[error]Timed out waiting for ACTIVE (unshelve)".replace(
        "##[error]", ""
    )


def test_assemble_log_without_failed_step_uses_last_error_in_whole_log():
    whole, tail, error = hv.assemble_log(make_zip(), None)
    assert tail == ""
    assert error == "Process completed with exit code 1."


# --- issue, instance, command --------------------------------------------------------------


def test_identify_from_title_and_log(cfg):
    run = {
        "display_title": "MC-Individual-230",
        "event": "issue_comment",
        "path": ".github/workflows/control-instance.yml",
    }
    ident = hv.identify(
        run,
        "x command_name=unshelve y\nserver morpho-cloud-test_instance-230 ACTIVE",
        "morpho-cloud-test_instance-",
        cfg,
    )
    assert ident == {
        "issue": 230,
        "name": "morpho-cloud-test_instance-230",
        "name_source": "log",
        "command": "/unshelve",
    }


def test_identify_derives_name_and_command_fallback(cfg):
    run = {
        "display_title": "MC-Individual-12",
        "event": "issue_comment",
        "path": ".github/workflows/create-instance.yml",
    }
    ident = hv.identify(run, "nothing useful", "instance-", cfg)
    assert ident == {
        "issue": 12,
        "name": "instance-12",
        "name_source": "derived",
        "command": "/create",
    }


def test_identify_workflow_dispatch_title_is_not_an_issue(cfg):
    run = {
        "display_title": "Reconcile Instances",
        "event": "workflow_dispatch",
        "path": ".github/workflows/reconcile-instances.yml",
    }
    assert hv.identify(run, "issue_number=77 shelving", "instance-", cfg)["issue"] == 77  # noqa: PLR2004
    assert hv.identify(run, None, "instance-", cfg)["issue"] is None


# --- actor_kind ---------------------------------------------------------------------------------


def dispatch_log(tmp_path: Path, lines: list[str]) -> hv.Dispatches:
    p = tmp_path / "morphocloud-dispatch.log"
    p.write_text("\n".join(lines) + "\n")
    return hv.Dispatches([p, tmp_path / "missing.log"])


def run_of(
    actor,
    event="workflow_dispatch",
    wf="follow-through.yml",
    created="2026-11-09T03:00:05Z",
    trigger=None,
):
    return {
        "actor": {"login": actor},
        "triggering_actor": {"login": trigger or actor},
        "event": event,
        "path": f".github/workflows/{wf}",
        "created_at": created,
    }


def test_rule1_dispatch_line_makes_automation_whoever_the_actor_is(cfg, tmp_path):
    d = dispatch_log(
        tmp_path,
        [
            "2026-11-09T03:00:01Z DISPATCH follow-through.yml repo=MorphoCloud/Instances issue=- by=morphocloud-dispatch.sh result=ok"
        ],
    )
    assert hv.actor_kind(
        cfg, run_of("muratmaga"), "MorphoCloud/Instances", None, None, d
    ) == ("automation", "dispatch-line")
    # a RERUN keeps attempt 1's created_at, so the line it matches is attempt
    # 1's; the person who clicked re-run is the actor of attempt 2 (T6 on Test)
    rerun = dict(run_of("muratmaga"), run_attempt=2)
    assert hv.actor_kind(cfg, rerun, "MorphoCloud/Instances", None, None, d) == (
        "admin",
        "admin",
    )
    # 61 s away, or another repository: not this clock's dispatch
    late = run_of("muratmaga", created="2026-11-09T03:01:02Z")
    assert (
        hv.actor_kind(cfg, late, "MorphoCloud/Instances", None, None, d)[0] == "admin"
    )
    assert (
        hv.actor_kind(
            cfg, run_of("muratmaga"), "MorphoCloud/Test-Instances", None, None, d
        )[0]
        == "admin"
    )


def test_rule1_legacy_dispatched_line_and_issue_match(cfg, tmp_path):
    d = dispatch_log(
        tmp_path,
        [
            "2026-11-09T03:00:01Z dispatched reconcile-instances.yml",
            "2026-11-09T03:00:02Z DISPATCH admin-alert.yml repo=MorphoCloud/Instances issue=214 by=police-floating-ips.py result=ok",
        ],
    )
    assert (
        hv.actor_kind(
            cfg,
            run_of("muratmaga", wf="reconcile-instances.yml"),
            "MorphoCloud/Instances",
            None,
            None,
            d,
        )[0]
        == "automation"
    )
    alert = run_of("muratmaga", wf="admin-alert.yml")
    assert (
        hv.actor_kind(cfg, alert, "MorphoCloud/Instances", 214, None, d)[0]
        == "automation"
    )
    assert (
        hv.actor_kind(cfg, alert, "MorphoCloud/Instances", 215, None, d)[0] == "admin"
    )


def test_schedule_and_workflow_run_events_are_automation(cfg, tmp_path):
    d = dispatch_log(tmp_path, [])
    # GitHub credits a schedule run to the workflow file's last committer
    sched = run_of("jcfr", event="schedule", wf="collect-instance-uptime.yml")
    assert hv.actor_kind(cfg, sched, "r", None, None, d) == (
        "automation",
        "event:schedule",
    )
    # a RERUN of a scheduled run is whoever clicked: the five rules decide
    rerun = dict(sched, run_attempt=2, triggering_actor={"login": "muratmaga"})
    assert hv.actor_kind(cfg, rerun, "r", None, None, d) == ("admin", "admin")
    follow = run_of("Sarah-G-88", event="workflow_run", wf="report-dropped-command.yml")
    assert hv.actor_kind(cfg, follow, "r", None, None, d) == (
        "automation",
        "event:workflow_run",
    )


def test_rules_2_to_5(cfg, tmp_path):
    d = dispatch_log(tmp_path, [])
    assert hv.actor_kind(cfg, run_of("github-actions[bot]"), "r", None, None, d) == (
        "automation",
        "bot",
    )
    assert hv.actor_kind(
        cfg, run_of("amm554", event="issue_comment"), "r", 5, "amm554", d
    ) == ("test", "test-account")
    assert hv.actor_kind(
        cfg, run_of("muratmaga", event="issue_comment"), "r", 5, "someone", d
    ) == ("admin", "admin")
    assert hv.actor_kind(
        cfg, run_of("muratmaga", event="issue_comment"), "r", 5, "muratmaga", d
    ) == ("member", "admin-own-issue")
    assert hv.actor_kind(
        cfg, run_of("Sarah-G-88", event="issue_comment"), "r", 5, "Sarah-G-88", d
    ) == ("member", "default")


# --- outside context ------------------------------------------------------------------------------


ACCESS_ROWS = [
    {
        "URN": "urn:1",
        "Subject": "Jetstream2 Scheduled Maintenance",
        "OutageStart": "2026-10-06T12:00:00Z",
        "OutageEnd": "2026-10-07T00:00:00Z",
        "OutageType": "Partial",
        "AffectedResources": "[{'ResourceID': 'jetstream2-gpu.indiana.access-ci.org'}]",
    },
    {
        "URN": "urn:2",
        "Subject": "Anvil Outage",
        "OutageStart": "2026-10-06T12:00:00Z",
        "OutageEnd": "2026-10-07T00:00:00Z",
        "OutageType": "Full",
        "AffectedResources": "[{'ResourceID': 'anvil.purdue.access-ci.org'}]",
    },
]


def test_access_context_filters_resource_and_overlap():
    t = hv.parse_ts("2026-10-06T15:00:00Z")
    ctx = hv.access_context(ACCESS_ROWS, "jetstream", t, t + 600)
    assert ctx["status"] == "outage"
    assert [o["urn"] for o in ctx["outages"]] == ["urn:1"]
    later = hv.parse_ts("2026-10-08T00:00:00Z")
    assert (
        hv.access_context(ACCESS_ROWS, "jetstream", later, later + 1)["status"]
        == "operational"
    )
    assert hv.access_context(None, "jetstream", t, t)["status"] == "unavailable"


def test_github_status_unknown_beyond_the_feed():
    inc = [
        {
            "id": "a",
            "name": "API",
            "impact": "minor",
            "created_at": "2026-11-08T10:00:00Z",
            "resolved_at": "2026-11-08T12:00:00Z",
            "components": [{"name": "API Requests"}],
        }
    ]
    inside = hv.parse_ts("2026-11-08T11:00:00Z")
    assert hv.github_status_context(inc, inside, inside + 1)["status"] == "incident"
    quiet = hv.parse_ts("2026-11-09T11:00:00Z")
    assert hv.github_status_context(inc, quiet, quiet + 1)["status"] == "operational"
    old = hv.parse_ts("2026-09-01T00:00:00Z")
    assert hv.github_status_context(inc, old, old + 1)["status"] == "unknown"


def test_grafana_points_and_flavor_table(cfg):
    data = {
        "results": {
            "A": {"frames": [{"data": {"values": [[1, 2], [23, 12]]}}]},
            "B": {"frames": []},
        }
    }
    assert hv.grafana_points(data, "A") == [[1, 23], [2, 12]]
    assert hv.grafana_points(data, "B") == []
    assert hv.grafana_vacancy(cfg, "m3.large", 0, 1) == {
        "status": "not_tracked",
        "flavor": "m3.large",
    }
    assert hv.grafana_vacancy(cfg, None, 0, 1) == {"status": "no_flavor"}


# --- labels at a moment, owner rule --------------------------------------------------------------------


def test_labels_at_and_flavor():
    tl = [
        {
            "event": "labeled",
            "created_at": "2026-11-01T00:00:00Z",
            "label": "flavor:g3.xl",
        },
        {
            "event": "labeled",
            "created_at": "2026-11-01T00:00:00Z",
            "label": "followup:unshelve",
        },
        {
            "event": "unlabeled",
            "created_at": "2026-11-02T00:00:00Z",
            "label": "followup:unshelve",
        },
        {
            "event": "labeled",
            "created_at": "2026-11-03T00:00:00Z",
            "label": "invariant-violation",
        },
    ]
    at = hv.labels_at(tl, "2026-11-01T12:00:00Z")
    assert at == ["flavor:g3.xl", "followup:unshelve"]
    assert hv.flavor_of(at) == "g3.xl"
    assert hv.owner_rule(at) == "follow-through"
    assert hv.owner_rule(hv.labels_at(tl, "2026-11-04T00:00:00Z")) == "reconcile"
    assert hv.owner_rule(["followup:setup"]) == "setup-watch"


# --- the store: dedupe, rollups, dry run -----------------------------------------------------------------


def test_store_marks_and_dedupes(tmp_path):
    st = hv.Store(tmp_path, "instances", dry_run=False)
    assert not st.recorded("2026-11-09T03:00:07Z", 1, 1)
    st.mark("2026-11-09T03:00:07Z", 1, 1, "follow-through.yml", "success")
    st.mark("2026-11-09T03:00:07Z", 2, 1, "follow-through.yml", "success")
    st.mark("2026-11-09T03:00:07Z", 1, 2, "follow-through.yml", "failure")
    assert st.recorded("2026-11-09T03:00:07Z", 1, 1)
    assert st.recorded("2026-11-09T03:00:07Z", 1, 2)
    assert not (tmp_path / "rollups").exists()  # nothing on disk until flush()
    st.flush()
    roll = json.loads((tmp_path / "rollups/2026/11/09/instances.json").read_text())
    assert roll["counts"] == {"follow-through.yml": {"success": 2, "failure": 1}}
    assert roll["recorded"] == ["1-1", "1-2", "2-1"]
    # a fresh Store reads the same day file back
    assert hv.Store(tmp_path, "instances", dry_run=False).recorded(
        "2026-11-09T03:00:07Z", 2, 1
    )


def test_store_dry_run_writes_nothing(tmp_path):
    st = hv.Store(tmp_path, "instances", dry_run=True)
    st.mark("2026-11-09T03:00:07Z", 1, 1, "x.yml", "success")
    st.write_gz("logs/a.log.gz", "text")
    st.append_jsonl("outcomes/x.jsonl", {"a": 1})
    st.flush()
    assert list(tmp_path.iterdir()) == []
    assert st.writes == 3  # noqa: PLR2004 — gz, jsonl, and the flushed rollup


def test_runner_log_offsets_restart_on_rotation(tmp_path, monkeypatch, cfg):
    store = hv.Store(tmp_path / "store", "test-instances", dry_run=False)
    local = hv.Local(tmp_path / "local.json", dry_run=False)
    logf = tmp_path / "fip-police.log"
    logf.write_text("one bob@x.org\ntwo\npartial")
    cfg2 = hv.Config(
        dict(
            cfg.raw,
            runner_logs=[{"name": "fip-police", "path": str(logf)}],
            diag_dir=None,
        ),
        {},
    )
    gh = hv.GitHub("t")
    h = hv.Harvester.__new__(hv.Harvester)
    h.cfg, h.store, h.local, h.redactor, h.src = (
        cfg2,
        store,
        local,
        hv.Redactor(SED, None),
        {"short": "test-instances"},
    )
    monkeypatch.setattr(hv.socket, "gethostname", lambda: "host1")
    now = hv.parse_ts("2026-11-09T04:00:00Z")
    h.runner_logs(now)
    shipped = tmp_path / "store/runner/2026/11/09/host1/fip-police.log"
    assert shipped.read_text() == "one [email]\ntwo\n"  # the partial last line waits
    assert local.data["offsets"]["fip-police"] == len("one bob@x.org\ntwo\n")
    logf.write_text("fresh\n")  # rotated: shorter than the offset
    h.runner_logs(now)
    assert shipped.read_text() == "one [email]\ntwo\nfresh\n"
    assert local.data["offsets"]["fip-police"] == len("fresh\n")
    assert gh.calls == 0


# --- time -----------------------------------------------------------------------------------------------------


def test_timestamps_are_utc():
    assert hv.to_iso(hv.parse_ts("2026-11-09T03:00:07Z")) == "2026-11-09T03:00:07Z"
    assert hv.day_path("2026-11-09T03:00:07Z") == "2026/11/09"
    assert hv.month_path("2026-11-09T03:00:07Z") == "2026/11"
    assert hv.parse_ts(None) == 0.0


# --- review findings (local review of PR #402) ---------------------------------------------


def test_identify_sweep_runs_take_no_instance_from_the_log(cfg):
    run = {
        "display_title": "Reconcile Instances",
        "event": "workflow_dispatch",
        "path": ".github/workflows/reconcile-instances.yml",
    }
    logtext = "checking instance-12 ... instance-57 ... issue_number=57"
    ident = hv.identify(run, logtext, "instance-", cfg, kind="sweep")
    assert ident["issue"] is None
    assert ident["name"] is None
    # an alert fired on an issue comment still gets its issue from the title
    alert = {
        "display_title": "MC-Individual-230",
        "event": "issue_comment",
        "path": ".github/workflows/on-admin-mention.yml",
    }
    ident = hv.identify(alert, logtext, "instance-", cfg, kind="alert")
    assert ident["issue"] == 230  # noqa: PLR2004
    assert ident["name"] is None  # not derived for an alert


def test_feeds_empty_is_not_unavailable(cfg, monkeypatch):
    monkeypatch.setattr(
        hv,
        "fetch_json",
        lambda *_a, **_k: {"results": [], "incidents": []},
    )
    feeds = hv.Feeds(cfg)
    assert feeds.access() == []
    assert feeds.github() == []
    assert (
        hv.access_context(feeds.access(), "jetstream", 0, 1)["status"] == "operational"
    )
    assert hv.github_status_context(feeds.github(), 0, 1)["status"] == "operational"


def test_diag_new_lines_ships_each_line_once(tmp_path):
    d = tmp_path / "_diag"
    d.mkdir()
    f = d / "Runner_1.log"
    f.write_text("INFO ok\nERR one\n")
    offsets: dict = {}
    pat = re.compile("ERR|Exception")
    assert hv.diag_new_lines(d, offsets, pat) == ["# Runner_1.log\n", "ERR one\n"]
    assert hv.diag_new_lines(d, offsets, pat) == []  # touched, nothing new
    with f.open("a") as fh:
        fh.write("INFO more\nException two\npartial")
    assert hv.diag_new_lines(d, offsets, pat) == ["# Runner_1.log\n", "Exception two\n"]
    f.unlink()
    assert hv.diag_new_lines(d, offsets, pat) == []
    assert offsets == {}


def test_safe_record_contains_one_bad_run(monkeypatch):
    h = hv.Harvester.__new__(hv.Harvester)
    h.errors = []
    monkeypatch.setattr(
        h, "record", lambda _run: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert h.safe_record({"id": 1, "run_attempt": 1}) is False
    assert h.errors == ["run 1/1: RuntimeError: boom"]


def test_dispatch_sh_logs_the_failure_reason(tmp_path):
    """The bash '${err: -200}' trap: an empty tail when stderr is short."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text("#!/bin/sh\necho 'HTTP 502: Bad Gateway' >&2\nexit 1\n")
    (fake / "gh").chmod(0o755)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "dispatch.pat").write_text("x\n")
    env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MORPHOCLOUD_DISPATCH_REPO": "MorphoCloud/Test-Instances",
        "MORPHOCLOUD_DISPATCH_TOKEN_FILE": str(cfg_dir / "dispatch.pat"),
    }
    subprocess.run(
        ["bash", str(SCRIPTS / "morphocloud-dispatch.sh"), "follow-through.yml"],
        env=env,
        check=True,
    )
    line = (tmp_path / "morphocloud-dispatch.log").read_text().strip()
    assert (
        " DISPATCH follow-through.yml repo=MorphoCloud/Test-Instances issue=- by=morphocloud-dispatch.sh result=FAILED HTTP 502: Bad Gateway"
        in line
    )
    assert hv.DISPATCH_RE.match(line)


def test_assemble_log_matches_a_sanitized_job_folder():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("0_Create _ provision_ step.txt", "x\n")
        zf.writestr("Create _ provision_ step/2_Poll.txt", "a\n##[error]boom\n")
        zf.writestr("Other job/2_Poll.txt", "##[error]not this one\n")
    _, tail, error = hv.assemble_log(
        buf.getvalue(), {"job": "Create / provision: step", "step": "Poll", "number": 2}
    )
    assert error == "boom"
    assert tail == "a\n##[error]boom\n"


def test_remote_diag_parses_listing_and_forgets_rotated_files(tmp_path, monkeypatch):
    h = hv.Harvester.__new__(hv.Harvester)
    h.cfg = hv.Config({"diag_match": "ERR"}, {})
    h.store = hv.Store(tmp_path / "store", "instances", dry_run=False)
    h.local = hv.Local(tmp_path / "local.json", dry_run=False)
    h.local.data["diag_remote_offsets"] = {"Runner_old.log": 500, "Worker_1.log": 10}
    # a stub: the subprocess.run patch below would catch the redactor's sed too
    h.redactor = SimpleNamespace(text=lambda t: (t, 0))
    out = "control-host\n==FILE Worker_1.log 40\nERR two\n==FILE Worker_2.log 7\n"
    monkeypatch.setattr(
        hv.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=out, stderr=""
        ),
    )
    assert h.remote_diag("exouser@control", "2026/11/09") == "ok"
    assert h.local.data["diag_remote_offsets"] == {
        "Worker_1.log": 40,
        "Worker_2.log": 7,
    }
    shipped = (tmp_path / "store/runner/2026/11/09/control-host/diag.log").read_text()
    assert shipped == "# Worker_1.log\nERR two\n"


def test_assemble_log_empty_archive_means_no_log():
    """A run cancelled in the concurrency queue has an empty archive (seen on
    Test: 61 of 189 backfilled incidents); no empty job.log.gz is written."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    assert hv.assemble_log(buf.getvalue(), None) == (None, "", None)
    # an archive with only nested files is still kept
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("job/system.txt", "sys\n")
    whole, _, _ = hv.assemble_log(buf.getvalue(), None)
    assert whole == "===== job/system.txt =====\nsys\n"
