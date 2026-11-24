"""The daily storage-term sweep (STORAGE_REDESIGN_PLAN.md decision 7,
"Storage term").

Reads the runner's plan (mc_share_runner.py sweep, which asked the portal
which requests are waiting) and the portal's requests report, then:

- remind:<N>       emails the member, records the reminder on the share
- delete-instance  past the end date with an instance: dispatches its
                   delete; the share is locked by the next day's sweep
- lock             locks the share (mc_share_runner.py lock), emails the member
- delete           deletes a share locked for its whole grace
                   (mc_share_runner.py expire), emails the member
- hold, locked, keep: nothing (a waiting request holds the lock)

then reminds the admins of requests waiting more than 3 days and emails the
members the decisions made since the last run. Every item is handled on its
own; a failure on one is reported and the next is still done. Exit 1 when
anything failed, so the workflow alerts the admins; the next run tries again.

Environment: PLAN_FILE, REQUESTS_FILE, TEMPLATE_FILE, RUNNER_SCRIPT,
TERM_DAYS, WARN_DAYS, GRACE_DAYS, INSTANCE_PREFIX, LOOKUP_URL, LOOKUP_API_KEY,
MAIL_USERNAME, MAIL_PASSWORD, PORTAL_URL, ADMIN_EMAILS, GH_TOKEN (gh api,
gh workflow run), DRY_RUN.
"""

import json
import sys
import time
from pathlib import Path

import common as c


def term_args() -> list[str]:
    return [
        "--term-days",
        c.env("TERM_DAYS"),
        "--warn",
        c.env("WARN_DAYS"),
        "--grace-days",
        c.env("GRACE_DAYS"),
    ]


def subs_for(a: dict, tpl: dict) -> dict:
    return {
        "days_left": max(a.get("days_to_end") or 0, 0),
        "kept_until": c.day(a.get("end_date")),
        "delete_on": c.day(a.get("delete_on")),
        "grace_days": c.env("GRACE_DAYS"),
        "term_days": c.env("TERM_DAYS"),
        "portal_url": c.env("PORTAL_URL"),
        "contact_email": tpl["contact_email"],
        "today": c.day(time.strftime("%Y-%m-%d", time.gmtime())),
    }


def handle(a: dict, tpl: dict) -> str | None:
    """One share's verdict. Returns what was done, or None for nothing."""
    gid, verdict = a["github_id"], a["verdict"]
    if verdict in ("keep", "hold", "locked"):
        return None
    if verdict == "error":
        msg = f"{a['name']}: {a.get('error')}"
        raise RuntimeError(msg)
    login = c.login_for(gid, a.get("login"))
    subs = subs_for(a, tpl)
    if verdict.startswith("remind:"):
        days = verdict.split(":", 1)[1]
        c.mail(tpl, "reminder", c.email_for(login), subs)
        c.runner(
            "mark", str(gid), "--reminded", days, "--term-days", c.env("TERM_DAYS")
        )
        return f"reminded {login} ({days} days before {a['end_date']})"
    if verdict == "delete-instance":
        issue = c.issue_of(a.get("mounted_by") or "")
        if issue is None:
            msg = f"{a['name']}: past its end date, held by {a.get('mounted_by')!r}, which is no request's instance"
            raise RuntimeError(msg)
        c.dispatch_delete(issue)
        return f"past its end date: deleting {login}'s instance (#{issue}); the lock follows tomorrow"
    if verdict == "lock":
        out = c.runner("lock", str(gid), *term_args())
        subs["delete_on"] = c.day(out.get("delete_on") or a.get("delete_on"))
        try:
            c.mail(tpl, "locked", c.email_for(login), subs)
        except c.FAILURES as e:
            msg = f"locked the storage of {login}, but the owner was not told: {e}"
            raise RuntimeError(msg) from e
        return f"locked the storage of {login} (end date {a['end_date']})"
    if verdict == "delete":
        out = c.runner("expire", str(gid), *term_args())
        note = (
            ""
            if c.DRY or out.get("portal_recorded")
            else " (the portal did not record it)"
        )
        try:
            c.mail(tpl, "deleted", c.email_for(login), subs)
        except c.FAILURES as e:
            note += f" (owner not told: {e})"
        if note:
            msg = f"deleted the storage of {login}{note}"
            raise RuntimeError(msg)
        return f"deleted the storage of {login} (end date {a['end_date']}, locked {a['locked_at']})"
    msg = f"unknown verdict {verdict!r}"
    raise RuntimeError(msg)


def note_line(note: str) -> str:
    return f"\n\nNote from the MorphoCloud team: {note}" if note else ""


def remind_admins(r: dict, tpl: dict) -> str:
    to = c.env("ADMIN_EMAILS")
    asked = c.day(r.get("created_at"))
    subs = {
        "login": r["login"],
        "reason": r["reason"],
        "asked": asked,
        "link": f"{c.env('PORTAL_URL').rstrip('/')}/admin/requests/{r['request_id']}",
        "contact_email": tpl["contact_email"],
    }
    c.mail(tpl, "admin_reminder", to, subs)
    c.runner("portal-mailed", r["request_id"], "admin")
    return f"reminded the admins of {r['login']}'s request (asked {asked})"


def tell_member(r: dict, tpl: dict) -> str:
    login = c.login_for(r["github_id"], r.get("login"))
    subs = {
        "kept_until": c.day(r.get("kept_until")),
        "note_line": note_line(r.get("note") or ""),
        "portal_url": c.env("PORTAL_URL"),
        "contact_email": tpl["contact_email"],
    }
    kind = "approved" if r["decision"] == "approved" else "declined"
    c.mail(tpl, kind, c.email_for(login), subs)
    c.runner("portal-mailed", r["request_id"], "member")
    return f"told {login} their request was {kind}"


def main() -> int:
    for name in (
        "TERM_DAYS",
        "WARN_DAYS",
        "GRACE_DAYS",
        "PORTAL_URL",
        "LOOKUP_URL",
        "LOOKUP_API_KEY",
        "ADMIN_EMAILS",
    ) + (() if c.DRY else ("MAIL_USERNAME", "MAIL_PASSWORD")):
        c.env(name)
    plan = json.loads(Path(c.env("PLAN_FILE")).read_text())
    requests = json.loads(Path(c.env("REQUESTS_FILE")).read_text())
    tpl = c.template()
    todo = [a for a in plan if a["verdict"] not in ("keep", "locked")]
    c.say(
        f"{len(plan)} share(s): {len(plan) - len(todo)} kept or locked, {len(todo)} to act on"
        + (" (dry run)" if c.DRY else "")
    )
    failures = []
    for a in plan:
        try:
            done = handle(a, tpl)
            label = a.get("name") or a.get("github_id")
            c.say(f"  {label}: {done or a['verdict']} (end {a.get('end_date')})")
        except c.FAILURES as e:
            c.say(f"::error ::{a.get('name', a.get('github_id'))}: {e}")
            failures.append(str(e))
    for r in requests.get("stale", []):
        try:
            c.say(f"  {remind_admins(r, tpl)}")
        except c.FAILURES as e:
            c.say(f"::error ::admin reminder for request {r.get('request_id')}: {e}")
            failures.append(str(e))
    for r in requests.get("decided", []):
        try:
            c.say(f"  {tell_member(r, tpl)}")
        except c.FAILURES as e:
            c.say(f"::error ::decision email for request {r.get('request_id')}: {e}")
            failures.append(str(e))
    return c.report_failures(failures)


if __name__ == "__main__":
    sys.exit(main())
