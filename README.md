# TTRPG Club Poll Bot

A Telegram bot for a tabletop RPG club: rating polls after each session, DM
notifications to the GM/admin, and a companion Telegram Mini App (built into the club's
website, [`../ttrpg_website2`](../ttrpg_website2)) for personal stats, a leaderboard,
and private session feedback.

**Production runs on AWS Lambda — see [`README_LAMBDA.md`](README_LAMBDA.md) for
architecture, deployment, the data model, and every feature in detail.** This file
covers what the bot does and how to run a minimal version locally.

## Commands

- `/rate <text>` or `/poll <text>` — create a non-anonymous 1–10 rating poll (e.g.
  `/rate Curse of Strahd session 4`), plus a "📝 Залишити фідбек" button opening the
  Mini App's private feedback form for that session.
- `/bool <question>` — a plain Yes/No poll (not tracked in stats — just a quick vote).
- `/stats` — opens the Mini App: your own rating history, a leaderboard, and
  Played/Conducted/All game lists.
- `/start` — help text.

Rating polls always have 11 options: "Подивитись відповідь" (view results, not counted
as a rating) plus 1–10. The exact emoji per rating have changed at least once over the
bot's history — anything parsing poll options should match on the `N / 10` numeric
prefix, not the emoji (see `scripts/backfill_historical_polls.py` for why this matters
in practice).

## Two implementations in this repo

| File | What | Used for |
|---|---|---|
| `lambda_handler.py` | The real bot — Bot API calls via raw `requests` (no `python-telegram-bot` dependency, to keep the Lambda zip small), DynamoDB for state, SSM for the token. Three entry points: `lambda_handler` (webhook), `notify_new_signup`, `notify_new_feedback`. | **Production**, on AWS Lambda. See `README_LAMBDA.md`. |
| `main.py` | A minimal standalone long-polling bot using the `python-telegram-bot` library. Same rating-poll mechanic, none of the DynamoDB/Mini App/notification features. | **Local testing only** — never deployed. |

## Local development (`main.py`)

```bash
pip install -r requirements.txt
pip install python-telegram-bot   # main.py's own dependency — deliberately not in
                                   # requirements.txt, since that file is also what
                                   # build.sh bundles into the production Lambda zip,
                                   # and main.py isn't part of that deployment at all
export TELEGRAM_BOT_TOKEN="your_test_bot_token_here"
python3 main.py
```

Get a token from [@BotFather](https://t.me/botfather). This mode only exercises the
poll-creation mechanic — for anything involving stats, the Mini App, or notifications,
you need the real Lambda deployment (dev environment) — see `README_LAMBDA.md`'s Setup
section.

## Repo layout

- `lambda_handler.py` — the production bot (see above).
- `main.py` — local-only test bot (see above).
- `build.sh` — packages `lambda_handler.py` + dependencies into `build/`, which both
  Terraform (initial deploy) and CI (routine deploys) read from.
- `requirements.txt` — `lambda_handler.py`'s dependencies only (`requests`, `boto3`).
- `scripts/backfill_historical_polls.py` — one-off tool to import polls/votes that
  predate DynamoDB tracking, by reading Telegram chat history via a personal account
  (MTProto) — the Bot API has no method for this. See the script's own docstring.
- `.github/workflows/deploy.yml` — CI: push to `develop`/`main` updates the matching
  environment's 3 Lambda functions.
- `serverless.yml` — **legacy, unused.** This bot was originally deployed via
  Serverless Framework/CloudFormation; it's been fully migrated to Terraform
  (`../aws_infra/lambda/ttrpg_poll_bot_{dev,prod}`) and this file is no longer read by
  anything. Left in place rather than deleted only because no one's gotten around to
  it — safe to remove.

## Full documentation

**[`README_LAMBDA.md`](README_LAMBDA.md)** covers everything about the real,
deployed bot: architecture, the dev/prod split, one-time setup, routine deploys, the
DynamoDB data model, the Personal Stats / Leaderboard / Session Feedback Mini App
features end-to-end, GitHub Actions CI, and cost.
