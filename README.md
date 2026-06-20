# imazen org-watch

Notifies you when **anyone other than you** interacts with a repo in the `imazen` org —
issues, PRs, comments, and reviews — so third-party activity stands out from your own
flood of GitHub notifications.

GitHub can't do this natively: watching a repo still emails you about your own actions
and buries everything in bot noise. This is a small scheduled sweep that filters to the
signal you actually want.

## What it catches

Every 30 minutes (tunable — see the `cron` in `.github/workflows/watch.yml`) it sweeps the
GitHub Events feed of **every non-archived repo in the org** (public *and* private), keeps only:

- `IssuesEvent`, `IssueCommentEvent` — issue opened/closed/commented
- `PullRequestEvent`, `PullRequestReviewEvent`, `PullRequestReviewCommentEvent` — PR opened/merged/reviewed
- `CommitCommentEvent`, discussion events

...where the actor is **not** you and **not** a bot, then sends the new ones to your notifier
(Telegram / ntfy / Pushover / Discord / Slack / … via [Apprise](https://github.com/caronc/apprise),
or SMTP email as a fallback).

Why per-repo events instead of `GET /orgs/{org}/events`: the org feed is **public-only**
and silently misses all private repos. The authenticated per-repo feed covers both and
attributes the real actor of each action. Filtering by event type makes it immune to your
own push noise.

Bots are excluded by `[bot]` suffix **plus** an explicit denylist for the suffix-less
ones that actually show up here — `Copilot`, `codecov-commenter`, `dependabot`, etc.
(see `WATCH_BOT_DENYLIST` in `watch.py`).

## Setup (3 steps)

**1. Create a GitHub token** that can read every repo's events (incl. private) and
read/write one Actions variable:
- **Classic (easiest):** https://github.com/settings/tokens/new → check **`repo`** + **`read:org`**.
  (If you're on the *fine-grained* page you won't see these checkboxes — that's the wrong page.)
- **Fine-grained (alt):** https://github.com/settings/personal-access-tokens/new → owner `imazen`,
  All repositories; Repository permissions: Metadata=Read, Contents=Read, Issues=Read,
  Pull requests=Read, **Variables=Read and write**.

**2. Pick a notifier** and get its [Apprise](https://github.com/caronc/apprise/wiki) URL — no
email account needed. Telegram is a good default (free + private):
- **Telegram:** DM `@BotFather` → `/newbot` → copy the bot token; get your numeric chat id
  (DM the bot once, then open `https://api.telegram.org/bot<token>/getUpdates`); URL = `tgram://<token>/<chatid>`
- **Pushover / Discord / Slack / ntfy / 100+ others:** see the Apprise wiki for the URL format.
  Prefer a private service — alerts include private-repo names.

(SMTP email still works as a fallback — fill the commented block in `.env.example` instead.)

**3. Wire it up:**

```bash
cp .env.example .env      # fill in ORG_READ_TOKEN + APPRISE_URL
./setup.sh                # pushes them as repo secrets/variables (needs gh admin auth)
```

Then send yourself a real test email over the last 3 days:

```bash
gh workflow run watch.yml -R imazen/org-watch -f lookback_min=4320
```

The schedule takes over automatically after that.

## Configuration

Secrets (`gh secret set`): `ORG_READ_TOKEN`, plus either `APPRISE_URL` (notifier) **or** `SMTP_PASS` (email).
Variables (`gh variable set`): for the SMTP fallback only — `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`ALERT_TO`/`ALERT_FROM`. Optionally:

| variable / env          | default            | meaning                                      |
|-------------------------|--------------------|----------------------------------------------|
| `WATCH_ORG`             | `imazen`           | org to watch                                 |
| `WATCH_SELF_LOGINS`     | `lilith`           | comma-sep logins treated as "you"            |
| `WATCH_BOT_DENYLIST`    | *(built-in list)*  | extra bot logins to ignore                   |
| `WATCH_WINDOW_MIN`      | `60`               | steady-state lookback per run (absorbs cron jitter / missed runs) |
| `WATCH_COLD_START_MIN`  | `15`               | first-run lookback (keeps the first run quiet)|
| `WATCH_SEEN_RETAIN_H`   | `24`               | how long to remember alerted event ids (dedup)|
| `WATCH_INCLUDE_ARCHIVED`| off                | also sweep archived repos                     |

## State & dedup

A single repo Actions variable `WATCH_STATE` holds the set of recently-alerted event ids
(pruned to 24h). Each run looks back a generous window and drops anything already in that
set — so cron delays or a missed run never cause a miss, and never double-alert. No commit
noise; the PAT reads/writes the variable directly.

## Local testing

```bash
GH_TOKEN="$(gh auth token)" WATCH_DRY_RUN=1 WATCH_VERBOSE=1 WATCH_STATE_FILE=/tmp/s.json \
  WATCH_LOOKBACK_MIN=129600 python3 watch.py     # 90-day dry run, prints detail, sends nothing
```

`WATCH_VERBOSE=1` prints per-item repo/actor/URL detail. It's **off by default** so the
Actions log (world-readable if this repo is public) never exposes private-repo activity —
only counts. Leave it off in CI; the details go in the email.

## Known limitations

- **Reviews** rely on GitHub emitting `PullRequestReviewEvent` in the per-repo events feed;
  approvals with no comment may not always appear. Issue/PR opens and comments are always covered.
- **Discussions / wiki** events aren't reliably emitted by the Events API.
- GitHub's Events API lags a few minutes and the cron is best-effort; the 60-minute window
  is what makes this reliable rather than instant. For true real-time, switch to an
  org-level webhook (not built here).

## Cadence & cost

Default cadence is **every 30 minutes** (~1,440 Actions-minutes/month on a private repo —
about half the Team plan's 3,000 free minutes). To tighten to 15 min, edit the `cron` to
`*/30` → `*/15` (≈2,880 min/month). Keep this repo **private**: its Actions logs would
otherwise expose private-repo names and who interacted. (Making it public would give free
unlimited minutes but leak that into public build logs.)
