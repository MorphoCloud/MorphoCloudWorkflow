"""The weekly scrub of long-shelved instances (STORAGE_REDESIGN_PLAN.md
decision 21, "Long-shelved instances").

Reads the runner's list (mc_share_runner.py stale-shelved: shelved for more
than SHELVED_DAYS by Nova's action history), and for each member's instance:
dispatches its delete (the request closes) and emails the member that the
instance was deleted because it was not used, that their files in MyDrive
are safe, and when MyDrive is kept until. An instance whose history could
not be read is never deleted; it is reported to the admins. Only requests a
member opened (request-creator:user) are touched.

Environment: TEMPLATE_FILE, RUNNER_SCRIPT, SHELVED_DAYS, INSTANCE_PREFIX,
LOOKUP_URL, LOOKUP_API_KEY, MAIL_USERNAME, MAIL_PASSWORD, PORTAL_URL,
GH_TOKEN, GH_REPO, DRY_RUN.
"""

import json
import subprocess
import sys

import common as c


def issue_facts(issue: int) -> tuple[str, list[str], str]:
    """The request's author login, labels and state."""
    r = subprocess.run(
        ["gh", "issue", "view", str(issue), "--json", "author,labels,state"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        msg = f"gh issue view {issue}: {r.stderr.strip()[-200:]}"
        raise RuntimeError(msg)
    data = json.loads(r.stdout)
    return (
        data["author"]["login"],
        [lb["name"] for lb in data.get("labels", [])],
        data.get("state", ""),
    )


def github_id(login: str) -> int:
    r = subprocess.run(
        ["gh", "api", f"users/{login}", "--jq", ".id"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        msg = f"gh api users/{login}: {r.stderr.strip()[-200:] or 'unexpected answer'}"
        raise RuntimeError(msg)
    return int(r.stdout.strip())


def handle(item: dict, tpl: dict, days: str) -> str:
    name, issue = item["name"], item["issue"]
    if item.get("error"):
        msg = f"{name}: shelved, but its age is unknown ({item['error']}); not deleted"
        raise RuntimeError(msg)
    login, labels, _state = issue_facts(issue)
    if "request-creator:user" not in labels:
        return f"{name}: not a member's request; left alone"
    gid = github_id(login)
    try:
        info = c.runner("info", str(gid), read_only=True)
        kept_until = c.day(info.get("end_date"))
    except c.FAILURES as e:
        c.say(f"  ({name}: MyDrive's end date unknown: {e})")
        kept_until = "unknown"
    c.dispatch_delete(issue)
    subs = {
        "instance_name": name,
        "days": days,
        "kept_until": kept_until,
        "portal_url": c.env("PORTAL_URL"),
        "contact_email": tpl["contact_email"],
    }
    try:
        c.mail(tpl, "shelved_deleted", c.email_for(login), subs)
    except c.FAILURES as e:
        msg = f"{name}: deleting (shelved {item['days']} days), but {login} was not told: {e}"
        raise RuntimeError(msg) from e
    return f"{name}: shelved {item['days']} days since {item['shelved_at']}; deleting, {login} told"


def main() -> int:
    days = c.env("SHELVED_DAYS")
    for name in ("PORTAL_URL", "LOOKUP_URL", "LOOKUP_API_KEY") + (
        () if c.DRY else ("MAIL_USERNAME", "MAIL_PASSWORD")
    ):
        c.env(name)
    tpl = c.template()
    items = c.runner("stale-shelved", "--days", days, read_only=True)
    c.say(
        f"{len(items)} instance(s) shelved more than {days} days"
        + (" (dry run)" if c.DRY else "")
    )
    failures = []
    for item in items:
        try:
            c.say(f"  {handle(item, tpl, days)}")
        except c.FAILURES as e:
            c.say(f"::error ::{e}")
            failures.append(str(e))
    return c.report_failures(failures)


if __name__ == "__main__":
    sys.exit(main())
