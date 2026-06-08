#!/usr/bin/env bash
# Weekly check that runner-1's dispatch tokens aren't about to expire.
#
# Reads each token's LIVE expiry from the GitHub `github-authentication-token-expiration`
# response header (no stored dates; survives rotation). If a token is within WARN_DAYS,
# it dispatches MorphoCloud/MorphoCloudAnalytics/token-expiry-alert.yml (using the
# analytics-dispatcher token) to email GH_ADMIN_EMAILS — the runner itself can't send mail.
#
# Install + crontab: see runner-cron-dispatcher.md
set -u
WARN="${WARN_DAYS:-28}"
CFG="$HOME/.config/morphocloud"
ALERT_TOKEN_FILE="$CFG/analytics-dispatch.pat"
ts() { date -u +%FT%TZ; }

dispatch_alert() {
  local name="$1" val="$2" days="$3"
  if [[ ! -s "$ALERT_TOKEN_FILE" ]]; then
    echo "$(ts) ERROR: $ALERT_TOKEN_FILE missing; cannot send alert for $name"; return
  fi
  if GH_TOKEN="$(tr -d '\r\n' <"$ALERT_TOKEN_FILE")" gh workflow run token-expiry-alert.yml \
       --repo MorphoCloud/MorphoCloudAnalytics --ref main \
       -f token_name="$name" -f expires_at="$val" -f days="$days"; then
    echo "$(ts) $name: alert dispatched ($days days left)"
  else
    echo "$(ts) $name: FAILED to dispatch alert"
  fi
}

check() {
  local name="$1" file="$2" tok val days
  if [[ ! -s "$file" ]]; then echo "$(ts) $name: token file $file missing"; return; fi
  tok="$(tr -d '\r\n' <"$file")"
  val="$(GH_TOKEN="$tok" gh api -i /rate_limit 2>/dev/null \
        | grep -i 'authentication-token-expiration' | sed 's/.*[Ee]xpiration: //' | tr -d '\r')"
  if [[ -z "$val" ]]; then echo "$(ts) $name: no expiry header (non-expiring?)"; return; fi
  days=$(( ( $(date -d "$val" +%s) - $(date +%s) ) / 86400 ))
  echo "$(ts) $name: expires $val ($days days left)"
  if (( days <= WARN )); then dispatch_alert "$name" "$val" "$days"; fi
}

check "MorphoCloud-workflow-dispatcher"     "$CFG/dispatch.pat"
check "GH_MorphoCloud-analytics-dispatcher" "$CFG/analytics-dispatch.pat"
