#!/usr/bin/env bash
# One-shot: push the secrets/variables this watcher needs onto the repo.
# Prereq: `gh auth login` as an imazen admin, and a filled-in ./.env (see .env.example).
set -euo pipefail

REPO="${WATCH_STATE_REPO:-imazen/org-watch}"

if [[ ! -f .env ]]; then
  echo "No .env found — copy .env.example to .env and fill it in first." >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

need() { [[ -n "${!1:-}" ]] || { echo "Missing $1 in .env" >&2; exit 1; }; }
need ORG_READ_TOKEN; need RESEND_API_KEY; need ALERT_TO; need ALERT_FROM

echo "Setting secrets + variables on $REPO ..."
gh secret   set ORG_READ_TOKEN -R "$REPO" --body "$ORG_READ_TOKEN"
gh secret   set RESEND_API_KEY -R "$REPO" --body "$RESEND_API_KEY"
gh variable set ALERT_TO       -R "$REPO" --body "$ALERT_TO"
gh variable set ALERT_FROM     -R "$REPO" --body "$ALERT_FROM"
[[ -n "${WATCH_SELF_LOGINS:-}"  ]] && gh variable set WATCH_SELF_LOGINS  -R "$REPO" --body "$WATCH_SELF_LOGINS"
[[ -n "${WATCH_BOT_DENYLIST:-}" ]] && gh variable set WATCH_BOT_DENYLIST -R "$REPO" --body "$WATCH_BOT_DENYLIST"

echo
echo "Done. Send yourself a real test email (3-day lookback, actually sends):"
echo "  gh workflow run watch.yml -R $REPO -f lookback_min=4320"
echo
echo "Or a no-send dry run (prints in the Actions log only):"
echo "  gh workflow run watch.yml -R $REPO -f dry_run=true -f lookback_min=129600"
