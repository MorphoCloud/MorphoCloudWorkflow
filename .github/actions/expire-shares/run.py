"""The storage expiry sweep (STORAGE_REDESIGN_PLAN.md, "Share expiry").

Reads the runner's plan (mc_share_runner.py sweep), then for every share that
is not simply kept: warns the owner by email and records the warning on the
share, or deletes the share (mc_share_runner.py expire) and tells the owner.
Every share is handled on its own; a failure on one is reported and the next
one is still done. Exit 1 when anything failed, so the workflow alerts the
admins; the next day's sweep tries again.

Environment: PLAN_FILE, TEMPLATE_FILE, RUNNER_SCRIPT, EXPIRY_DAYS, LOOKUP_URL,
LOOKUP_API_KEY, MAIL_USERNAME, MAIL_PASSWORD, PORTAL_URL, GH_TOKEN (gh api),
DRY_RUN ("true": decide and print, change and send nothing).
"""

import json
import os
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

MAIL_HOST, MAIL_PORT = "webhost.dynadot.com", 587
MAIL_FROM = "MorphoCloudPortal <no-reply@morphocloud.org>"
LOOKUP_ATTEMPTS = 3
DRY = os.environ.get("DRY_RUN", "").lower() == "true"


def say(text: str) -> None:
    print(text)  # noqa: T201 — the run log is this script's output


def env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        msg = f"{name} is not set"
        raise RuntimeError(msg)
    return v


def login_for(gid: int) -> str:
    """The owner's current login from the GitHub id (logins change)."""
    r = subprocess.run(
        ["gh", "api", f"user/{gid}", "--jq", ".login"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip():
        msg = f"gh api user/{gid}: {r.stderr.strip()[-200:]}"
        raise RuntimeError(msg)
    return r.stdout.strip()


def email_for(login: str) -> str:
    """The verified address from the join lookup, as the credentials email uses."""
    url = (
        f"{env('LOOKUP_URL')}?action=lookup&github_username={urllib.parse.quote(login)}"
    )
    req = urllib.request.Request(url, headers={"X-Api-Key": env("LOOKUP_API_KEY")})
    data: dict = {}
    for attempt in range(1, LOOKUP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            break
        except (OSError, ValueError) as e:
            if attempt == LOOKUP_ATTEMPTS:
                msg = f"lookup for {login}: {e}"
                raise RuntimeError(msg) from e
            time.sleep(5)
    email = data.get("email")
    if not email:
        msg = f"lookup for {login}: {data.get('error') or 'no address'}"
        raise RuntimeError(msg)
    return email


def render(template: dict, key: str, subs: dict) -> str:
    text = template[key]
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
    msg.set_content(body)
    if DRY:
        say(f"  would mail {subject!r}")
        return
    with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=60) as s:
        s.starttls()
        s.login(env("MAIL_USERNAME"), env("MAIL_PASSWORD"))
        s.send_message(msg)


def runner(*args: str) -> dict:
    """mc_share_runner.py on this host: touch or expire. Its JSON on stdout."""
    if DRY:
        say(f"  would run mc_share_runner.py {' '.join(args)}")
        return {}
    script = os.environ.get("RUNNER_SCRIPT") or str(
        Path("~/mc-data/mc_share_runner.py").expanduser()
    )
    r = subprocess.run(
        [sys.executable, script, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=1200,
    )
    if r.returncode != 0:
        msg = f"mc_share_runner.py {args[0]}: {r.stderr.strip()[-400:]}"
        raise RuntimeError(msg)
    return json.loads(r.stdout or "{}")


def day(iso: str) -> str:
    t = time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    return time.strftime("%-d %b %Y", t)


def handle(a: dict, template: dict) -> str:
    gid, verdict = a["github_id"], a["verdict"]
    if verdict == "error":
        msg = f"{a['name']}: {a.get('error')}"
        raise RuntimeError(msg)
    login = login_for(gid)
    subs = {
        "days_left": max(a["days_left"], 0),
        "kept_until": day(a["expires_at"]),
        "idle_days": env("EXPIRY_DAYS"),
        "portal_url": env("PORTAL_URL"),
        "contact_email": template["contact_email"],
    }
    if verdict in ("warn:first", "warn:last"):
        kind = verdict.split(":")[1]
        to = email_for(login)
        send(
            to,
            render(template, f"warning_{kind}_subject", subs),
            render(template, f"warning_{kind}_body", subs),
        )
        runner("touch", str(gid), "--warned", kind)
        return f"warned ({kind}) {login}: {a['days_left']} days left"
    if verdict == "expire":
        out = runner("expire", str(gid), "--expiry-days", env("EXPIRY_DAYS"))
        note = (
            ""
            if DRY or out.get("portal_recorded")
            else " (the portal did not record it)"
        )
        try:
            send(
                email_for(login),
                render(template, "deleted_subject", subs),
                render(template, "deleted_body", subs),
            )
        except RuntimeError as e:
            note += f" (owner not told: {e})"
        if note:
            msg = f"deleted the storage of {login}{note}"
            raise RuntimeError(msg)
        return f"deleted the storage of {login} (last activity {a['last_activity']})"
    msg = f"unknown verdict {verdict!r}"
    raise RuntimeError(msg)


def main() -> int:
    plan = json.loads(Path(env("PLAN_FILE")).read_text())
    template = json.loads(Path(env("TEMPLATE_FILE")).read_text())
    kept = [a for a in plan if a["verdict"] == "keep"]
    todo = [a for a in plan if a["verdict"] != "keep"]
    say(
        f"{len(plan)} share(s): {len(kept)} kept, {len(todo)} to act on"
        + (" (dry run)" if DRY else "")
    )
    for a in kept:
        held = f", held by {a['mounted_by']}" if a.get("held") else ""
        say(f"  keep {a['name']}: {a['days_left']} days left{held}")
    failures = []
    for a in todo:
        try:
            say(f"  {handle(a, template)}")
        except (
            RuntimeError,
            OSError,
            KeyError,
            ValueError,
            subprocess.SubprocessError,
        ) as e:
            say(f"::error ::{a.get('name', a.get('github_id'))}: {e}")
            failures.append(str(e))
    if failures:
        with Path(os.environ.get("GITHUB_OUTPUT", "/dev/null")).open("a") as out:
            out.write("failures<<__EOF__\n" + "\n".join(failures) + "\n__EOF__\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
