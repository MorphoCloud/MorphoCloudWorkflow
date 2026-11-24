"""Shared by the storage-term sweep (run.py) and the long-shelved scrub
(scrub.py): the runner's script, the member's login and address, the email
copy from PortalContent, mail, and the instance delete dispatch
(STORAGE_REDESIGN_PLAN.md decisions 7 and 21).

DRY_RUN ("true"): decide and print only; change nothing, send nothing.
"""

import json
import os
import re
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
# Errors one share or instance can raise; one never stops the next.
FAILURES = (
    RuntimeError,
    OSError,
    KeyError,
    ValueError,
    subprocess.SubprocessError,
    smtplib.SMTPException,  # not an OSError: a refused login or recipient
)


def say(text: str) -> None:
    print(text)  # noqa: T201 — the run log is this script's output


def env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        msg = f"{name} is not set"
        raise RuntimeError(msg)
    return v


def login_for(gid: int, recorded: str | None) -> str:
    """The owner's current login from the GitHub id: GET /user/{account_id}.
    Logins change, so a recorded login is only the fallback."""
    r = subprocess.run(
        ["gh", "api", f"user/{gid}", "--jq", "[.id, .login] | @tsv"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    got_id, _, got_login = (
        r.stdout.strip().partition("\t") if r.returncode == 0 else ("", "", "")
    )
    if got_id == str(gid) and got_login:
        return got_login
    if recorded:
        say(f"  (GitHub did not answer for {gid}; using the recorded login {recorded})")
        return recorded
    msg = f"gh api user/{gid}: {r.stderr.strip()[-200:] or 'unexpected answer'}"
    raise RuntimeError(msg)


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


def template() -> dict:
    return json.loads(Path(env("TEMPLATE_FILE")).read_text())


def render(tpl: dict, key: str, subs: dict) -> str:
    text = tpl[key]
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
    msg.set_content(body)
    if DRY:
        say(f"  would mail {subject!r} to {to}")
        return
    with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=60) as s:
        s.starttls()
        s.login(env("MAIL_USERNAME"), env("MAIL_PASSWORD"))
        s.send_message(msg)


def mail(tpl: dict, kind: str, to: str, subs: dict) -> None:
    send(to, render(tpl, f"{kind}_subject", subs), render(tpl, f"{kind}_body", subs))


def runner(*args: str, read_only: bool = False) -> dict | list:
    """mc_share_runner.py on this host; its JSON on stdout. A dry run still
    runs the read-only commands (sweep, info, stale-shelved, portal-requests)."""
    if DRY and not read_only:
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


def day(iso_date: str | None) -> str:
    """2027-03-23 -> 23 Mar 2027, as the emails say dates."""
    if not iso_date:
        return "unknown"
    t = time.strptime(iso_date[:10], "%Y-%m-%d")
    return f"{t.tm_mday} {time.strftime('%b %Y', t)}"


def issue_of(instance_name: str) -> int | None:
    """The request issue from an instance name ([<prefix>_]instance-<n>)."""
    prefix = os.environ.get("INSTANCE_PREFIX", "")
    lead = re.escape(f"{prefix}_") if prefix else ""
    m = re.fullmatch(rf"{lead}instance-([0-9]+)", instance_name or "")
    return int(m.group(1)) if m else None


def dispatch_delete(issue: int) -> None:
    """The share-mode delete of an instance (clean shutdown, then the request
    closes), as the instance sweep dispatches it."""
    if DRY:
        say(f"  would dispatch the delete of #{issue}")
        return
    r = subprocess.run(
        [
            "gh",
            "workflow",
            "run",
            "control-instance-from-workflow.yml",
            "-f",
            f"issue_number={issue}",
            "-f",
            "command_name=delete",
            "-f",
            "unapprove_after_delete=true",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        msg = f"dispatching the delete of #{issue}: {r.stderr.strip()[-300:]}"
        raise RuntimeError(msg)


def report_failures(failures: list[str]) -> int:
    if not failures:
        return 0
    with Path(os.environ.get("GITHUB_OUTPUT", "/dev/null")).open("a") as out:
        out.write("failures<<__EOF__\n" + "\n".join(failures) + "\n__EOF__\n")
    return 1
