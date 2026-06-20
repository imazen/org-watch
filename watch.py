#!/usr/bin/env python3
"""imazen org-watch — email an alert when anyone other than the owner interacts
with any repo in the GitHub org.

How it works
------------
Every run it sweeps the GitHub *Events* feed of every (non-archived) repo in the
org in parallel, keeps only human-collaboration events (issues, PRs, comments,
reviews) whose actor is neither the owner nor a bot, drops anything already seen,
and emails the new activity via SMTP (or Resend).

Why per-repo events instead of the org feed: `GET /orgs/{org}/events` only
returns *public* events, so it silently misses every private repo. The
authenticated per-repo feed covers public + private uniformly and attributes the
real actor of each action. Filtering by event type makes it immune to the
owner's own push noise.

Stdlib only — nothing to `pip install` on the runner.

Env (all optional unless noted):
  GH_TOKEN / GITHUB_TOKEN  GitHub token with org read (repo + read:org) [required]
  ALERT_TO                 comma-sep recipients                         [required unless dry-run]
  ALERT_FROM               from address (defaults to SMTP_USER)
  -- pick ONE sender (checked in this order) --
  APPRISE_URL              notifier URL(s): Telegram/ntfy/Pushover/Discord/Slack via Apprise  [preferred]
  SMTP_HOST/PORT/USER/PASS SMTP server + app password (587 STARTTLS / 465 SSL)
  RESEND_API_KEY           Resend API key
  WATCH_ORG                org to watch (default: imazen)
  WATCH_SELF_LOGINS        comma-sep logins treated as "the owner"      (default: lilith)
  WATCH_BOT_DENYLIST       comma-sep extra bot logins to ignore
  WATCH_STATE_REPO         owner/repo holding the WATCH_STATE variable  (default: <org>/org-watch)
  WATCH_STATE_FILE         use a local JSON file for state instead of the repo variable
  WATCH_COLD_START_MIN     first-run lookback minutes (default: 15)
  WATCH_WINDOW_MIN         steady-state lookback minutes per run (default: 60)
  WATCH_SEEN_RETAIN_H      hours to remember alerted event ids (default: 24)
  WATCH_MAX_WORKERS        parallel repo fetches (default: 8)
  WATCH_INCLUDE_ARCHIVED   set to 1 to also sweep archived repos
  WATCH_DRY_RUN            set to 1 to print instead of email / write state
  WATCH_LOOKBACK_MIN       manual override of the lookback window (ignores cold-start logic)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

GITHUB_API = "https://api.github.com"
RESEND_API = "https://api.resend.com/emails"

# ---------------------------------------------------------------- config

def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default

def _truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")

ORG            = _env("WATCH_ORG", "imazen")
OWNER_LOGINS   = {x.strip().lower() for x in _env("WATCH_SELF_LOGINS", "lilith").split(",") if x.strip()}
# Bots that do NOT carry a "[bot]" suffix and must be named explicitly.
_DEFAULT_BOTS  = "copilot,codecov-commenter,github-actions,renovate,mergify,pre-commit-ci,sonarcloud,coderabbitai,sentry-io,vercel,netlify,changeset-bot,allcontributors,imgbot,deepsource-autofix,restyled-io,semgrep-app,snyk-bot"
BOT_DENYLIST   = {x.strip().lower() for x in (_DEFAULT_BOTS + "," + _env("WATCH_BOT_DENYLIST", "")).split(",") if x.strip()}

GH_TOKEN       = _env("GH_TOKEN") or _env("GITHUB_TOKEN")
# Delivery, in priority order:
#   1. APPRISE_URL    — notifier URL(s): Telegram, ntfy, Pushover, Discord/Slack, … (via Apprise)
#   2. SMTP_*         — plain email over SMTP (stdlib)
#   3. RESEND_API_KEY — Resend transactional email
APPRISE_URL    = _env("APPRISE_URL")
SMTP_HOST      = _env("SMTP_HOST")
SMTP_PORT      = int(_env("SMTP_PORT", "587"))     # 587 STARTTLS (default) or 465 SSL
SMTP_USER      = _env("SMTP_USER")
SMTP_PASS      = _env("SMTP_PASS")
RESEND_KEY     = _env("RESEND_API_KEY")
ALERT_TO       = [x.strip() for x in _env("ALERT_TO", "").split(",") if x.strip()]
ALERT_FROM     = _env("ALERT_FROM")                # defaulted per-sender below

STATE_REPO     = _env("WATCH_STATE_REPO", f"{ORG}/org-watch")
STATE_FILE     = _env("WATCH_STATE_FILE")
COLD_START_MIN = int(_env("WATCH_COLD_START_MIN", "15"))
WINDOW_MIN     = int(_env("WATCH_WINDOW_MIN", "60"))
SEEN_RETAIN_H  = int(_env("WATCH_SEEN_RETAIN_H", "24"))
MAX_WORKERS    = int(_env("WATCH_MAX_WORKERS", "8"))
INCLUDE_ARCH   = _truthy(_env("WATCH_INCLUDE_ARCHIVED", ""))
DRY_RUN        = _truthy(_env("WATCH_DRY_RUN", ""))
LOOKBACK_MIN   = _env("WATCH_LOOKBACK_MIN")
# Print per-item repo/actor/URL detail to stdout. OFF by default so this is safe to run in
# a PUBLIC repo (Actions logs are world-readable) — details only ever go in the email.
# Turn on for local debugging where the terminal is private.
VERBOSE        = _truthy(_env("WATCH_VERBOSE", ""))

# Event types that count as "a person interacted".
HUMAN_TYPES = {
    "IssuesEvent",
    "IssueCommentEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "PullRequestReviewThreadEvent",
    "CommitCommentEvent",
    # Emitted only on some orgs; harmless if never seen:
    "DiscussionEvent",
    "DiscussionCommentEvent",
}

# ---------------------------------------------------------------- time helpers

def now_utc():
    return datetime.now(timezone.utc)

def parse_ts(s):
    # GitHub timestamps look like 2026-06-19T08:16:46Z
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

# ---------------------------------------------------------------- http

def _request(method, url, token=None, body=None, accept="application/vnd.github+json"):
    headers = {"User-Agent": "imazen-org-watch", "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return urllib.request.Request(url, data=data, headers=headers, method=method)

def gh(method, path, token, body=None, params=None, retries=4):
    url = path if path.startswith("http") else GITHUB_API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = _request(method, url, token=token, body=body)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                hdrs = dict(resp.headers)
                payload = json.loads(raw) if raw else None
                return resp.status, hdrs, payload
        except urllib.error.HTTPError as e:
            last = e
            # Secondary/primary rate limit handling.
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                remaining = e.headers.get("X-RateLimit-Remaining")
                if remaining == "0" and reset:
                    wait = max(1, min(60, int(reset) - int(time.time()) + 1))
                    time.sleep(wait)
                    continue
                retry_after = e.headers.get("Retry-After")
                time.sleep(int(retry_after) if retry_after else (2 ** attempt))
                continue
            if 500 <= e.code < 600:
                time.sleep(2 ** attempt)
                continue
            # 404 / 410 etc. — not retryable
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt)
    if isinstance(last, Exception):
        raise last
    raise RuntimeError(f"request failed: {method} {url}")

def gh_paginate(path, token, params=None):
    """Follow Link rel=next, concatenating list responses."""
    out = []
    url = GITHUB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    while url:
        status, hdrs, payload = gh("GET", url, token)
        if isinstance(payload, list):
            out.extend(payload)
        url = None
        link = hdrs.get("Link") or hdrs.get("link")
        if link:
            for part in link.split(","):
                seg = part.split(";")
                if len(seg) >= 2 and 'rel="next"' in seg[1]:
                    url = seg[0].strip().strip("<>")
    return out

# ---------------------------------------------------------------- github data

def list_repos(token):
    repos = gh_paginate(f"/orgs/{ORG}/repos", token, {"per_page": 100, "type": "all"})
    out = []
    for r in repos:
        if r.get("archived") and not INCLUDE_ARCH:
            continue
        out.append(r["full_name"])
    return out

def repo_events(full_name, token, since):
    """Page-1 (and page-2 if it's full and still inside the window) of a repo's events."""
    events = []
    for page in (1, 2):
        try:
            status, hdrs, payload = gh("GET", f"/repos/{full_name}/events", token,
                                       params={"per_page": 100, "page": page})
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 451):  # gone / DMCA / unavailable
                return []
            raise
        if not isinstance(payload, list) or not payload:
            break
        keep = [e for e in payload if parse_ts(e["created_at"]) >= since]
        events.extend(keep)
        # Only walk to page 2 if page 1 was full AND its oldest event is still in-window.
        if len(payload) < 100 or parse_ts(payload[-1]["created_at"]) < since:
            break
    return events

# ---------------------------------------------------------------- filtering

def is_owner(login):
    return login.lower() in OWNER_LOGINS

def is_bot(login):
    lo = login.lower()
    return lo.endswith("[bot]") or lo in BOT_DENYLIST

def wanted(ev):
    if ev.get("type") not in HUMAN_TYPES:
        return False
    actor = (ev.get("actor") or {}).get("login", "")
    if not actor or is_owner(actor) or is_bot(actor):
        return False
    return True

def describe(ev):
    t = ev["type"]
    p = ev.get("payload", {}) or {}
    action = p.get("action", "")
    repo = ev["repo"]["name"]
    actor = ev["actor"]["login"]
    url, title, snippet, verb = "", "", "", t

    if t == "IssuesEvent":
        iss = p.get("issue", {}) or {}
        url, title = iss.get("html_url", ""), iss.get("title", "")
        verb = f"{action} issue"
    elif t == "IssueCommentEvent":
        iss = p.get("issue", {}) or {}
        c = p.get("comment", {}) or {}
        url = c.get("html_url", iss.get("html_url", ""))
        title = iss.get("title", "")
        kind = "PR" if iss.get("pull_request") else "issue"
        verb = f"commented on {kind}"
        snippet = (c.get("body") or "")[:240]
    elif t == "PullRequestEvent":
        pr = p.get("pull_request", {}) or {}
        url, title = pr.get("html_url", ""), pr.get("title", "")
        verb = "merged PR" if (action == "closed" and pr.get("merged")) else f"{action} PR"
    elif t == "PullRequestReviewEvent":
        pr = p.get("pull_request", {}) or {}
        r = p.get("review", {}) or {}
        url = r.get("html_url", pr.get("html_url", ""))
        title = pr.get("title", "")
        verb = f"reviewed PR ({r.get('state', '')})"
    elif t == "PullRequestReviewCommentEvent":
        pr = p.get("pull_request", {}) or {}
        c = p.get("comment", {}) or {}
        url = c.get("html_url", pr.get("html_url", ""))
        title = pr.get("title", "")
        verb = "review comment on PR"
        snippet = (c.get("body") or "")[:240]
    elif t == "CommitCommentEvent":
        c = p.get("comment", {}) or {}
        url = c.get("html_url", "")
        verb = "commented on a commit"
        snippet = (c.get("body") or "")[:240]
    elif t in ("DiscussionEvent", "DiscussionCommentEvent"):
        d = p.get("discussion", {}) or {}
        url = d.get("html_url", "")
        title = d.get("title", "")
        verb = "discussion comment" if t == "DiscussionCommentEvent" else f"{action} discussion"

    return {
        "id": ev["id"], "type": t, "repo": repo, "actor": actor,
        "when": ev["created_at"], "verb": verb, "title": title,
        "url": url, "snippet": snippet,
    }

# ---------------------------------------------------------------- state

def load_state(token):
    if STATE_FILE:
        try:
            with open(STATE_FILE) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}
    try:
        _, _, payload = gh("GET", f"/repos/{STATE_REPO}/actions/variables/WATCH_STATE", token)
        return json.loads(payload["value"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise

def save_state(token, state):
    blob = json.dumps(state, separators=(",", ":"))
    if STATE_FILE:
        with open(STATE_FILE, "w") as fh:
            fh.write(blob)
        return
    body = {"name": "WATCH_STATE", "value": blob}
    try:
        gh("PATCH", f"/repos/{STATE_REPO}/actions/variables/WATCH_STATE", token, body=body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            gh("POST", f"/repos/{STATE_REPO}/actions/variables", token, body=body)
        else:
            raise

# ---------------------------------------------------------------- email

def render_html(items):
    rows = []
    for it in items:
        snip = ""
        if it["snippet"]:
            esc = (it["snippet"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            snip = f'<div style="color:#555;font-size:12px;margin-top:2px">{esc}</div>'
        title = (it["title"] or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        link = it["url"] or "#"
        rows.append(f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;white-space:nowrap;font-size:12px;color:#888">{it['when']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee"><b>{it['actor']}</b></td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{it['verb']}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee">
            <a href="{link}" style="color:#0969da;text-decoration:none">{it['repo']}</a>
            <span style="color:#555"> — {title}</span>{snip}
          </td>
        </tr>""")
    return f"""<!doctype html><html><body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111">
    <h2 style="margin:0 0 4px">🔔 Outside activity in <code>{ORG}/*</code></h2>
    <p style="margin:0 0 12px;color:#666">{len(items)} interaction(s) by someone other than {", ".join(sorted(OWNER_LOGINS))} (bots excluded).</p>
    <table style="border-collapse:collapse;width:100%;font-size:14px">
      <thead><tr style="text-align:left;color:#888;font-size:12px">
        <th style="padding:6px 10px">when (UTC)</th><th style="padding:6px 10px">who</th>
        <th style="padding:6px 10px">what</th><th style="padding:6px 10px">where</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <p style="margin:14px 0 0;color:#aaa;font-size:11px">imazen org-watch · {STATE_REPO}</p>
    </body></html>"""

def render_text(items):
    lines = [f"Outside activity in {ORG}/* — {len(items)} interaction(s):", ""]
    for it in items:
        lines.append(f"- {it['when']}  {it['actor']}  {it['verb']}  {it['repo']}")
        if it["title"]:
            lines.append(f"    {it['title']}")
        lines.append(f"    {it['url']}")
    return "\n".join(lines)

def subject(items):
    actors = sorted({it["actor"] for it in items})
    if len(items) == 1:
        it = items[0]
        return f"[imazen-watch] {it['actor']} {it['verb']} — {it['repo']}"
    who = ", ".join(actors[:3]) + (f" +{len(actors) - 3} more" if len(actors) > 3 else "")
    return f"[imazen-watch] {len(items)} outside interactions ({who})"

def send_email_smtp(items):
    msg = EmailMessage()
    msg["Subject"] = subject(items)
    msg["From"] = ALERT_FROM or SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)
    msg.set_content(render_text(items))
    msg.add_alternative(render_html(items), subtype="html")
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    print(f"smtp: sent to {len(ALERT_TO)} recipient(s) via {SMTP_HOST}:{SMTP_PORT}")


def send_email_resend(items):
    body = {
        "from": ALERT_FROM or "imazen org-watch <onboarding@resend.dev>",
        "to": ALERT_TO,
        "subject": subject(items),
        "html": render_html(items),
        "text": render_text(items),
    }
    status, _, payload = gh("POST", RESEND_API, RESEND_KEY, body=body, accept="application/json")
    print(f"resend: status={status} id={(payload or {}).get('id')}")


def send_notify_apprise(items):
    import apprise  # installed via `pip install apprise` in the workflow; imported lazily
    ap = apprise.Apprise()
    added = 0
    for url in APPRISE_URL.split():  # whitespace / newlines separate multiple targets
        if ap.add(url):
            added += 1
    if not added:
        sys.exit("ERROR: APPRISE_URL had no valid notifier targets")
    ok = ap.notify(title=subject(items), body=render_text(items))
    print(f"apprise: notify ok={ok} via {added} target(s)")
    if not ok:
        sys.exit("ERROR: apprise notify failed (check APPRISE_URL / service)")


def deliver(items):
    if APPRISE_URL:
        send_notify_apprise(items)
    elif SMTP_HOST:
        send_email_smtp(items)
    elif RESEND_KEY:
        send_email_resend(items)
    else:
        sys.exit("ERROR: no sender configured (set APPRISE_URL, SMTP_*, or RESEND_API_KEY)")

# ---------------------------------------------------------------- main

def main():
    if not GH_TOKEN:
        # Not configured yet (secrets not added) — exit cleanly so early scheduled
        # runs are green no-ops instead of red failures that email you noise.
        print("ORG_READ_TOKEN not set yet — run ./setup.sh to configure. Skipping.")
        return

    state = load_state(GH_TOKEN)
    seen = state.get("seen", {})  # {event_id: created_at_iso}
    first_run = not state.get("initialized")

    if LOOKBACK_MIN is not None:
        window = int(LOOKBACK_MIN)
    elif first_run:
        window = COLD_START_MIN
    else:
        window = WINDOW_MIN
    since = now_utc() - timedelta(minutes=window)

    repos = list_repos(GH_TOKEN)
    print(f"sweeping {len(repos)} repos in {ORG}/ since {since.isoformat()} "
          f"(window={window}m, first_run={first_run}, dry_run={DRY_RUN})")

    all_events = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(repo_events, r, GH_TOKEN, since): r for r in repos}
        for fut in cf.as_completed(futs):
            try:
                all_events.extend(fut.result())
            except Exception as e:  # noqa: BLE001 — one repo must not sink the run
                print(f"WARN: {futs[fut]} failed: {e}", file=sys.stderr)

    fresh = [e for e in all_events if wanted(e) and e["id"] not in seen]
    fresh.sort(key=lambda e: e["created_at"])
    items = [describe(e) for e in fresh]

    print(f"found {len(all_events)} in-window events, {len(items)} new outside interactions")
    if VERBOSE:  # detail is sensitive (private-repo names/actors) — opt-in only
        for it in items:
            print(f"  · {it['when']}  {it['actor']:20}  {it['verb']:24}  {it['repo']}  {it['url']}")

    if items and not DRY_RUN:
        has_sender = APPRISE_URL or (ALERT_TO and (SMTP_HOST or RESEND_KEY))
        if not has_sender:
            sys.exit("ERROR: configure a sender — APPRISE_URL, or SMTP_*/RESEND_API_KEY + ALERT_TO "
                     "(or set WATCH_DRY_RUN=1)")
        deliver(items[:80])  # cap absurd backlogs so one notification can't balloon

    # Record everything we just alerted on, prune old ids.
    for e in fresh:
        seen[e["id"]] = e["created_at"]
    cutoff = now_utc() - timedelta(hours=SEEN_RETAIN_H)
    seen = {k: v for k, v in seen.items() if parse_ts(v) >= cutoff}
    new_state = {"initialized": True, "seen": seen, "last_run": now_utc().isoformat()}

    if not DRY_RUN:
        save_state(GH_TOKEN, new_state)
        print(f"state saved: {len(seen)} ids retained")
    else:
        print("dry-run: state NOT written, email NOT sent")

if __name__ == "__main__":
    main()
