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


def test_diag_new_lines_primes_then_ships_each_line_once(tmp_path):
    d = tmp_path / "_diag"
    d.mkdir()
    f = d / "Runner_1.log"
    f.write_text("INFO ok\nERR one\n")
    offsets: dict = {}
    pat = re.compile("ERR|Exception")
    # first cycle: history is recorded, not shipped
    assert hv.diag_new_lines(d, offsets, pat) == []
    assert offsets == {"Runner_1.log": f.stat().st_size}
    assert hv.diag_new_lines(d, offsets, pat) == []  # touched, nothing new
    with f.open("a") as fh:
        fh.write("INFO more\nException two\npartial")
    assert hv.diag_new_lines(d, offsets, pat) == ["# Runner_1.log\n", "Exception two\n"]
    (d / "Worker_2.log").write_text("ERR new file\n")  # born after priming: from 0
    assert hv.diag_new_lines(d, offsets, pat) == ["# Worker_2.log\n", "ERR new file\n"]
    f.unlink()
    (d / "Worker_2.log").unlink()
    assert hv.diag_new_lines(d, offsets, pat) == []
    assert offsets == {}


def test_diag_match_is_the_runners_err_level_only():
    sources = hv.yaml.safe_load(
        (hv.data_dir() / "incident-sources.yml").read_text("utf-8")
    )
    pat = re.compile(sources["diag_match"])
    assert pat.search(
        "[2026-11-09 04:50:49Z ERR  GitHubActionsService] GET request failed"
    )
    for noise in (
        "[2026-11-09 16:50:36Z INFO ProcessInvokerWrapper] STDOUT/STDERR stream read finished.",
        " ---> System.Net.Sockets.SocketException (125): Operation canceled",
        "   at System.Net.Sockets.Socket.AwaitableSocketAsyncEventArgs.ThrowException(SocketError e)",
        '                "lit": "BASELINE_ERROR_REQS"',
        "[2026-11-09 04:50:49Z WARN GitHubActionsService] slow",
    ):
        assert not pat.search(noise), noise


def test_ship_diag_caps_a_day_once(tmp_path, monkeypatch):
    monkeypatch.setattr(hv, "DIAG_DAY_CAP", 40)
    h = hv.Harvester.__new__(hv.Harvester)
    h.store = hv.Store(tmp_path / "store", "instances", dry_run=False)
    h.redactor = SimpleNamespace(text=lambda t: (t, 0))
    rel = "runner/2026/11/09/host/diag.log"
    h.ship_capped(rel, ["# A.log\n", "ERR one\n"])  # 16 bytes, under the cap
    h.ship_capped(rel, ["# B.log\n", "ERR two is longer than the rest\n"])  # crosses it
    h.ship_capped(rel, ["# C.log\n", "ERR three\n"])  # dropped
    text = (tmp_path / "store" / rel).read_text()
    assert text.startswith(
        "# A.log\nERR one\n# B.log\nERR two is longer than the rest\n"
    )
    assert text.count("# capped:") == 1
    assert "ERR three" not in text


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


def test_remote_diag_first_sight_primes_without_shipping(tmp_path, monkeypatch):
    h = hv.Harvester.__new__(hv.Harvester)
    h.cfg = hv.Config({"diag_match": "ERR"}, {})
    h.store = hv.Store(tmp_path / "store", "instances", dry_run=False)
    h.local = hv.Local(tmp_path / "local.json", dry_run=False)
    h.redactor = SimpleNamespace(text=lambda t: (t, 0))
    sent: dict = {}

    def fake_run(*_a, **kw):
        sent["script"] = kw["input"]
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="control-host\n==FILE Runner_1.log 900\n",
            stderr="",
        )

    monkeypatch.setattr(hv.subprocess, "run", fake_run)
    assert h.remote_diag("exouser@control", "2026/11/09") == "ok"
    assert sent["script"].startswith("prime=1\n")
    assert h.local.data["diag_remote_offsets"] == {"Runner_1.log": 900}
    assert not (tmp_path / "store/runner/2026/11/09/control-host/diag.log").exists()
    # with offsets in hand the script ships
    h.remote_diag("exouser@control", "2026/11/09")
    assert sent["script"].startswith("prime=\n")


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
    # nested files without a trailing newline, and a non-text entry, in the fallback
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("job/a.txt", "no newline")
        zf.writestr("job/b.txt", "second")
        zf.writestr("job/blob.bin", b"\x00\x01")
    whole, _, _ = hv.assemble_log(buf.getvalue(), None)
    assert whole == "===== job/a.txt =====\nno newline\n===== job/b.txt =====\nsecond\n"


# --- the :00 race between the dispatcher and the harvester (production, 2026-11-09 07:15Z)


def test_dispatch_index_refreshes_for_a_run_newer_than_the_read(cfg, tmp_path):
    logf = tmp_path / "morphocloud-dispatch.log"
    logf.write_text("")
    d = hv.Dispatches([logf])
    now = hv.to_iso(d.loaded_at + 1)
    run = run_of("muratmaga", created=now)
    # the dispatcher writes its line a moment after the harvester read the log
    logf.write_text(f"{now} dispatched follow-through.yml\n")
    assert hv.actor_kind(cfg, run, "MorphoCloud/Instances", None, None, d) == (
        "automation",
        "dispatch-line",
    )
    # a human dispatch in the same window re-reads the logs once, not on every miss
    human = run_of("muratmaga", wf="reconcile-instances.yml", created=now)
    assert (
        hv.actor_kind(cfg, human, "MorphoCloud/Instances", None, None, d)[0] == "admin"
    )
    loaded = d.loaded_at
    assert (
        hv.actor_kind(cfg, human, "MorphoCloud/Instances", None, None, d)[0] == "admin"
    )
    assert d.loaded_at == loaded


def test_too_fresh_runs_wait_for_the_next_cycle():
    now = hv.parse_ts("2026-11-09T07:15:03Z")
    assert hv.too_fresh({"created_at": "2026-11-09T07:15:02Z"}, now)
    assert hv.too_fresh({"created_at": "2026-11-09T07:13:10Z"}, now)
    assert not hv.too_fresh({"created_at": "2026-11-09T07:12:50Z"}, now)


# --- coverage amendment: classes I, J, K -------------------------------------------

jr = load("mc-journal-read.py", "journal_reader")


def _entry(us: int, msg: str, **kw) -> str:
    e = {"__REALTIME_TIMESTAMP": str(us), "__CURSOR": f"s=x;i={us}", "MESSAGE": msg}
    e.update(kw)
    return json.dumps(e) + "\n"


class _FakeJournal:
    """subprocess.Popen stand-in for journalctl -o json."""

    def __init__(self, lines: list[str], rc: int = 0):
        self.lines, self.rc, self.argv = lines, rc, None

    def __call__(self, argv, **_kw):
        self.argv = argv
        self.stdout = iter(self.lines)
        return self

    def terminate(self):
        return None

    def wait(self, timeout=None):  # noqa: ARG002 — Popen's signature
        return self.rc


def test_reader_accepts_only_its_two_forms():
    assert jr.parse_request("journal - actions.runner.*,cron.service") == (
        "-",
        ["actions.runner.*", "cron.service"],
    )
    assert jr.parse_request("journal s=ab;i=12 none") == ("s=ab;i=12", ["none"])
    for bad in (
        "journal - a;rm -rf /",
        "journal $(id) x",
        "bash -c id",
        "journal - x extra",
        "",
    ):
        assert jr.parse_request(bad) is None, bad


def test_reader_ships_warnings_kernel_and_listed_units_only(monkeypatch):
    lines = [
        _entry(
            1_700_000_000_000_000, "routine", PRIORITY="6", _SYSTEMD_UNIT="gdm.service"
        ),
        _entry(
            1_700_000_001_000_000, "disk full", PRIORITY="3", _SYSTEMD_UNIT="x.service"
        ),
        _entry(
            1_700_000_002_000_000,
            "libceph: mds7 rejected session",
            PRIORITY="6",
            _TRANSPORT="kernel",
        ),
        _entry(
            1_700_000_003_000_000,
            "mail 524 too frequently",
            PRIORITY="6",
            _SYSTEMD_UNIT="mc-data-portal.service",
        ),
    ]
    fake = _FakeJournal(lines)
    monkeypatch.setattr(jr.subprocess, "Popen", fake)
    out = io.StringIO()
    last, n, rc = jr.read(["--after-cursor=s=x;i=1"], ["mc-data-*.service"], "c0", out)
    text = out.getvalue()
    assert (n, rc) == (4, 0)
    assert last == "s=x;i=1700000003000000"
    assert "routine" not in text
    assert "disk full" in text
    assert "mds7 rejected" in text
    assert "524" in text
    assert text.startswith("2023-11-14T22:13:21Z x.service p3: disk full")


def test_reader_stops_at_budget_and_resumes_from_last_read(monkeypatch):
    lines = [_entry(1_700_000_000_000_000 + i, f"m{i}", PRIORITY="3") for i in range(5)]
    monkeypatch.setattr(jr.subprocess, "Popen", _FakeJournal(lines))
    monkeypatch.setattr(jr, "BUDGET", -1)  # out of time after the first entry
    out = io.StringIO()
    last, n, rc = jr.read([], [], "c0", out)
    assert (n, rc) == (1, 0)
    assert last == "s=x;i=1700000000000000"
    assert out.getvalue().count("\n") == 1


def test_reader_file_form_reads_only_the_allowlist(tmp_path):
    log = tmp_path / "w.log"
    log.write_text("one\ntwo\npart")
    allow = tmp_path / "allow"
    allow.write_text(f"welcomes {log}\nbad name /etc/shadow\n")
    out = io.StringIO()
    assert jr.read_file("welcomes", "0", out, str(allow)) == 0
    assert out.getvalue() == "one\ntwo\n##OFFSET 8\n"
    out = io.StringIO()
    assert jr.read_file("welcomes", "999", out, str(allow)) == 0  # rotated: from 0
    assert out.getvalue().endswith("##OFFSET 8\n")
    assert jr.read_file("shadow", "0", io.StringIO(), str(allow)) == 2  # noqa: PLR2004
    assert jr.read_file("welcomes", "-1", io.StringIO(), str(allow)) == 2  # noqa: PLR2004


def test_code_kind_is_counted_never_evidence(cfg):
    assert cfg.kind("claude-code-review.yml") == "code"
    assert not hv.needs_detail("code", "failure")
    assert not hv.wants_outcome_line("code", "failure")
    assert hv.finalize(cfg, "ci.yml", "code", "failure", []) == ("failure", False)


def _jh(tmp_path, cfg, hosts):
    h = hv.Harvester.__new__(hv.Harvester)
    h.cfg = cfg
    h.store = hv.Store(tmp_path / "store", "instances", dry_run=False)
    h.local = hv.Local(tmp_path / "local.json", dry_run=False)
    h.redactor = SimpleNamespace(text=lambda t: (t, 0))
    h.src = {"short": "instances", "hosts": hosts}
    return h


def test_journals_file_lines_by_day_summarize_firewall_and_noise(
    tmp_path, cfg, monkeypatch
):
    h = _jh(
        tmp_path, cfg, [{"name": "portal", "ssh": "mcjournal@10.1.1.1", "units": ["x"]}]
    )
    replies = [
        (
            [
                "2026-11-09T20:00:00Z kernel p6: libceph: mds7 rejected session\n",
                "2026-11-09T20:00:01Z kernel p4: [UFW BLOCK] IN=ens3 SRC=1.2.3.4 DST=x PROTO=TCP DPT=23 \n",
                "2026-11-09T20:00:02Z user@1002.service p4: jackdbus: Failed\n",
                "2026-11-10T01:00:00Z mc-data-portal.service p6: share 14 unreadable\n",
            ],
            "s=next",
            None,
        )
    ]
    monkeypatch.setattr(h, "host_call", lambda *_a: replies.pop(0))
    status = h.journals(hv.parse_ts("2026-11-10T02:00:00Z"))
    assert status == {"portal": "ok"}
    day9 = (tmp_path / "store/runner/2026/11/09/portal/journal.log").read_text()
    day10 = (tmp_path / "store/runner/2026/11/10/portal/journal.log").read_text()
    assert "mds7 rejected session" in day9
    assert "UFW" not in day9.split("# daily")[0]
    assert "jackdbus" not in day9.split("# daily")[0]
    assert "firewall blocks 1; top ports 23x1; top sources 1.2.3.0/24x1" in day9
    assert "noise removed pipewire-jackdbus=1" in day9
    assert "share 14 unreadable" in day10
    assert h.local.data["journal"]["portal"]["cursor"] == "s=next"


def test_journals_record_a_host_going_down_once_and_coming_back(
    tmp_path, cfg, monkeypatch
):
    h = _jh(
        tmp_path, cfg, [{"name": "join", "ssh": "mcjournal@10.0.0.9", "units": ["x"]}]
    )
    replies = [([], "", "timed out"), ([], "", "timed out"), ([], "s=1", None)]
    monkeypatch.setattr(h, "host_call", lambda *_a: replies.pop(0))
    t = hv.parse_ts("2026-11-09T18:00:00Z")
    assert h.journals(t)["join"] == "unreachable since 2026-11-09T18:00:00Z"
    assert h.journals(t + 300)["join"] == "unreachable since 2026-11-09T18:00:00Z"
    assert h.journals(t + 600)["join"] == "ok"
    text = (tmp_path / "store/runner/2026/11/09/join/journal.log").read_text()
    assert text.count("unreachable") == 1
    assert "reachable again" in text
    assert (
        h.local.data["journal"]["join"]["cursor"] == "s=1"
    )  # never advanced while down


def test_platform_writes_only_transitions(tmp_path, cfg, monkeypatch):
    h = _jh(tmp_path, cfg, [{"name": "runner", "nova": "morphocloud-instances-runner"}])
    monkeypatch.setenv("OS_CLOUD", "X")
    states = [
        [
            {
                "Name": "morphocloud-instances-runner",
                "Status": "ACTIVE",
                "Power State": "Running",
            }
        ],
        [
            {
                "Name": "morphocloud-instances-runner",
                "Status": "ACTIVE",
                "Power State": "Running",
            }
        ],
        [
            {
                "Name": "morphocloud-instances-runner",
                "Status": "PAUSED",
                "Power State": "Paused",
            }
        ],
    ]
    monkeypatch.setattr(hv, "ro_json", lambda *_a: states.pop(0))
    t = hv.parse_ts("2026-11-09T17:50:00Z")
    for i in range(3):
        assert h.platform(t + 300 * i) == "ok"
    rows = [
        json.loads(x)
        for x in (tmp_path / "store/platform/2026/11/09.jsonl").read_text().splitlines()
    ]
    assert [r["to"]["status"] for r in rows] == ["ACTIVE", "PAUSED"]
    assert rows[1]["from"]["status"] == "ACTIVE"
    assert rows[1]["at"] == "2026-11-09T18:00:00Z"


def test_org_repos_excludes_the_source_and_the_deny_list_and_caches(tmp_path, cfg):
    h = _jh(tmp_path, cfg, [])
    h.repo, h.src = "MorphoCloud/Instances", {"short": "instances", "harvest_org": True}
    calls = []

    def get(path, params=None):
        calls.append((path, params))
        return [
            {"full_name": "MorphoCloud/Instances"},
            {"full_name": "MorphoCloud/Test-Instances"},
            {"full_name": "MorphoCloud/MC-PCOM-ONC-101"},
            {"full_name": "MorphoCloud/old", "archived": True},
        ]

    h.gh = SimpleNamespace(get=get)
    assert h.org_repos(1000.0) == ["MorphoCloud/MC-PCOM-ONC-101"]
    assert h.org_repos(1000.0 + 60) == ["MorphoCloud/MC-PCOM-ONC-101"]
    assert len(calls) == 1  # re-listed hourly, not every cycle


def test_one_failing_repository_does_not_stop_the_others(tmp_path, cfg, monkeypatch):
    h = _jh(tmp_path, cfg, [])
    h.extras, h.unreadable = {}, {}
    monkeypatch.setattr(h, "org_repos", lambda _now: ["MorphoCloud/A", "MorphoCloud/B"])
    seen = []

    def fake_extra(repo):
        def boom(_now):
            msg = "HTTP 403"
            raise RuntimeError(msg)

        def fine(_now):
            seen.append(repo)

        return SimpleNamespace(
            new_runs=boom if repo.endswith("A") else fine,
            watch=lambda: None,
            lookback=lambda _n: None,
            followups=lambda _n: None,
        )

    monkeypatch.setattr(h, "extra", fake_extra)
    h.extras_cycle(1000.0)
    assert seen == ["MorphoCloud/B"]
    assert "HTTP 403" in h.unreadable["MorphoCloud/A"]


def test_every_host_entry_is_complete(cfg):
    for repo, src in cfg.raw["sources"].items():
        hosts = src.get("hosts") or []
        assert hosts, f"{repo} lists no hosts"
        assert sum(1 for x in hosts if x.get("ssh") == "local") == 1, repo
        for host in hosts:
            assert host.get("name"), host
            assert host.get("nova"), host
            assert host.get("units"), host
            assert host["ssh"] == "local" or host["ssh"].startswith("mcjournal@"), host
            assert jr.UNITS_RE.match(",".join(host["units"])), host
            for name, path in (host.get("files") or {}).items():
                assert jr.NAME_RE.match(name), name
                assert path.startswith("/"), path


def test_reader_refuses_when_it_cannot_see_the_system_journal(monkeypatch):
    monkeypatch.setattr(jr.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(jr.os, "getgroups", lambda: [1000])
    monkeypatch.setattr(
        jr.grp, "getgrgid", lambda _g: SimpleNamespace(gr_name="exouser")
    )
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "journal - none")
    assert jr.main() == 2  # noqa: PLR2004 — refused, nothing read


@pytest.mark.parametrize(
    "path",
    [
        "dynamic/dependabot/dependabot-updates",
        "dynamic/copilot-swe-agent/copilot",
        "dynamic/copilot-pull-request-reviewer/copilot-pull-request-reviewer",
        ".github/workflows/claude-code-review.yml",
    ],
)
def test_github_synthetic_runs_are_code(cfg, path):
    """The `path` values GitHub returned for these runs on 2026-11-10."""
    assert cfg.kind(hv.workflow_file({"path": path})) == "code"


def test_journal_installer_rejects_quote_breaking_input():
    text = (SCRIPTS / "install-journal-reader.sh").read_text()
    assert "awk '{print $1, $2}'" in text  # the key comment never reaches the host
    assert "^/[A-Za-z0-9._/-]{1,255}$" in text  # no quote can appear in a path
