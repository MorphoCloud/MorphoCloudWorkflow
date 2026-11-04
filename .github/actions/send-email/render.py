"""Render the instance-credentials email from the PortalContent template.

Reads the fetched JSON (argv[1]) and the placeholder values from the
environment, then appends the rendered subject/body to $GITHUB_OUTPUT.
Plain str.replace — no regex/sed — so passphrases, URLs with &/# and
markdown all pass through untouched.
"""

import json
import os
import secrets
import sys
from pathlib import Path

t = json.loads(Path(sys.argv[1]).read_text())

subs = {
    "{{repo_name}}": os.environ["REPO_NAME"],
    "{{repository}}": os.environ["REPOSITORY"],
    "{{issue_number}}": os.environ["ISSUE_NUMBER"],
    "{{instance_name}}": os.environ["INSTANCE_NAME"],
    "{{instance_ip}}": os.environ["INSTANCE_IP"],
    "{{connection_url}}": os.environ["CONNECTION_URL"],
    # Empty when the instance has no upload page (created before the feature,
    # or the page did not respond): every template line using it is dropped.
    "{{upload_url}}": os.environ.get("UPLOAD_URL", ""),
    "{{passphrase}}": os.environ["PASSPHRASE"],
    # The data portal, where the prototype's email sends the user to connect.
    # Empty where there is no portal (production).
    "{{portal_url}}": os.environ.get("PORTAL_URL", ""),
    "{{contact_email}}": t["contact_email"],
}


def render(text: str) -> str:
    # A line built around a missing value is dropped whole, never left as a
    # fragment ("Sign in at:" with nothing after it).
    for key in ("{{upload_url}}", "{{portal_url}}"):
        if not subs[key]:
            text = "\n".join(line for line in text.split("\n") if key not in line)
    for key, value in subs.items():
        text = text.replace(key, value)
    return text


# A per-run delimiter: a rendered line equal to a fixed one would end the
# value early and silently drop the rest of the email (#228).
delim = f"__RENDER_{secrets.token_hex(8)}__"
with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
    out.write(f"subject<<{delim}\n")
    out.write(render(t["credentials_subject"]) + "\n")
    out.write(f"{delim}\n")
    out.write(f"body<<{delim}\n")
    out.write(render(t["credentials_body"]) + "\n")
    out.write(f"{delim}\n")
