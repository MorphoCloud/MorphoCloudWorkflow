# Redaction rules shared by the incident harvester (every job log, comment
# and runner log it writes) and, from PR B, by the record-facts action inside
# the job. Run as `sed -E -f redact.sed`; POSIX extended regular expressions
# only, so GNU sed (the runners) and BSD sed (the maintainer's machine, where
# the tests run) agree. Email addresses are NOT here: they are replaced by an
# HMAC in a second, Python pass (INCIDENT_STORE_PLAN.md, "Redaction and
# privacy"). Bump rules_version in harvest-incidents.py when a rule changes.
#
# GitHub tokens (classic, app, fine-grained).
s/(^|[^A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}/\1[token]/g
s/(^|[^A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}/\1[token]/g
# Private-key blocks: every line from BEGIN to END, the markers included.
/-----BEGIN [A-Z ]*PRIVATE KEY-----/,/-----END [A-Z ]*PRIVATE KEY-----/s/.*/[private-key]/
# OpenStack credentials: OS_PASSWORD, application-credential secrets, and the
# clouds.yaml / JSON shapes `password: x`, "password": "x", `secret: x`.
s/(OS_PASSWORD|OS_APPLICATION_CREDENTIAL_SECRET|MAIL_PASSWORD|SMTP_PASSWORD)([=:][[:space:]]*)[^[:space:]"']+/\1\2[redacted]/g
s/("?(password|passwd|secret|application_credential_secret)"?[[:space:]]*[=:][[:space:]]*"?)[^[:space:]"',]+/\1[redacted]/g
# The instance passphrase, kept as a Nova tag `exoPw:<passphrase>`.
s/exoPw:[^[:space:]"',]*/exoPw:[redacted]/g
# VNC password lines (vncpasswd, VNC password, vnc_password=...).
s/([Vv][Nn][Cc][^[:space:]]*([Pp][Aa][Ss][Ss]|[Pp][Ww])[^[:space:]]*[=:][[:space:]]*)[^[:space:]]+/\1[redacted]/g
# The CephFS share access key from mc_share_runner.py's output.
s/("?access_key"?[[:space:]]*[=:][[:space:]]*"?)[A-Za-z0-9+\/=]+/\1[redacted]/g
