# Telegram Poll Bot - Lambda Version

Telegram bot that creates rating polls, deployed on AWS Lambda.

## Architecture

- **AWS Lambda**: Runs the bot code (3 functions: `webhook`, `notifySignup`, `notifyFeedback`)
- **API Gateway (HTTP API v2)**: Receives the webhook from Telegram
- **SSM Parameter Store**: Stores the bot token securely (see [Data Model](#data-model)
  for exactly which parameters exist and which are actually read at runtime)
- **Terraform** — two fully independent, symmetric stacks, one per environment:
  `aws_infra/lambda/ttrpg_poll_bot_dev` and `aws_infra/lambda/ttrpg_poll_bot_prod`. Each
  owns its own functions, its own API Gateway (own webhook URL, own registered Telegram
  bot), its own IAM role, its own DynamoDB Stream event source mappings (reading that
  same environment's `dynamodb/ttrpg_club/<env>` tables), and its own Terraform-managed
  SSM placeholders for the bot token and admin chat ID. This repo only ever ships
  *code*, via `build.sh` + `aws lambda update-function-code` (by hand or via GitHub
  Actions — same code both places, but each push only updates the one stack matching
  the branch: `develop` → dev's 3 functions, `main` → prod's 3 functions) — no
  CloudFormation, no Serverless Framework. Matches exactly how `ttrpg_website2`'s
  backend deploys (branch → environment promotion).

  (These two modules used to be one combined `lambda/ttrpg_poll_bot` module — see that
  directory's `migrate-to-split-modules.sh` if you're looking at history; the live prod
  bot's function names/API Gateway never changed across that split, so its Telegram
  webhook was never disrupted.)

## Prerequisites

1. Python 3.14 + pip
2. AWS CLI configured with credentials
3. Telegram bot token from [@BotFather](https://t.me/botfather) — a separate bot per environment
4. Terraform (only needed when the *infrastructure* changes — not for routine code pushes)

## Setup

Pick an environment directory: `aws_infra/lambda/ttrpg_poll_bot_dev` or
`.../ttrpg_poll_bot_prod`. The two are independent — repeat this whole section for each.

### 1. Apply the Terraform

```bash
cd ../../../ttrpg_poll_bot && ./build.sh   # produces build/, which Terraform's source_path reads
cd -   # back in aws_infra/lambda/ttrpg_poll_bot_<env>
terraform init
terraform apply
```

This also creates three Terraform-managed SSM placeholders (`/ttrpg_club/<env>/poll_bot/token`,
`/ttrpg_club/<env>/telegram_admin_chat_id`, `/ttrpg_club/<env>/telegram_club_chat_id` —
value `"replace_me!"` until you set them below; Terraform ignores further changes to
their value, so it won't clobber the real one once set).

(`ttrpg_poll_bot_prod/import-from-serverless.sh` is only relevant if you're migrating an
existing Serverless Framework deployment from scratch rather than starting fresh.)

### 2. Store the Real Values in SSM

None of these are in source control — the bot token, the admin's personal chat ID (for
signup notifications), and the club chat ID (see Chat Restriction below) are all set
directly against SSM:

```bash
aws ssm put-parameter --name "/ttrpg_club/<env>/poll_bot/token" \
  --value "YOUR_BOT_TOKEN" --type SecureString --overwrite --region eu-west-2

aws ssm put-parameter --name "/ttrpg_club/<env>/telegram_admin_chat_id" \
  --value "<admin's numeric chat id>" --type SecureString --overwrite --region eu-west-2

aws ssm put-parameter --name "/ttrpg_club/<env>/telegram_club_chat_id" \
  --value "<club group's numeric chat id>" --type SecureString --overwrite --region eu-west-2
```

`terraform output webhook_url` gives you the API Gateway URL.

### 3. Set Webhook

```bash
python lambda_handler.py "$(terraform output -raw webhook_url)"
```

Or manually:
```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"<webhook_url from above>"}'
```

### Routine code changes

Once the infrastructure above exists, day-to-day changes to `lambda_handler.py` don't go
through Terraform at all — either push to `develop` (updates the 3 dev functions) or
`main` (updates the 3 prod functions) — see GitHub Actions Deployment below — or,
locally, for whichever stack you're updating:

```bash
./build.sh
cd build && zip -r ../function.zip . -x "__pycache__/*" && cd ..
aws lambda update-function-code --function-name telegram-poll-bot-<env>-webhook --zip-file fileb://function.zip
aws lambda update-function-code --function-name telegram-poll-bot-<env>-notifySignup --zip-file fileb://function.zip
aws lambda update-function-code --function-name telegram-poll-bot-<env>-notifyFeedback --zip-file fileb://function.zip
```

This is enforced, not just a convention: all 3 modules in both environments set
`ignore_source_code_hash = true`. Without it, any later `terraform apply` for an
unrelated change (env vars, IAM) would notice that the function's real deployed code
(pushed by CI) no longer matches what's in the local `build/` directory and silently
revert it back — undoing every code deploy that happened outside Terraform since the
last local build. With the flag set, Terraform only ever pushes code on the very first
`apply` (when the function doesn't exist yet); every apply after that only touches
infrastructure (env vars, IAM, memory/timeout, etc.), never the deployed code.

## Usage

- `/rate <text>` - Create a rating poll (1-10); also sends a "Leave Feedback" Mini App link
- `/stats` - Open the club's Telegram Mini App: your own rating stats, game history, and a leaderboard
- `/start` - A short Ukrainian description of the bot (it's for this club only) plus a
  button linking to the club's website (`CLUB_WEBSITE_URL` — see Configuration)

## Chat Restriction

Each bot (dev and prod) is locked to exactly one Telegram group — the numeric ID stored
in SSM at `/ttrpg_club/<env>/telegram_club_chat_id` (read at runtime via
`ALLOWED_CHAT_ID_SSM_PARAMETER`, cached per warm container). If it's added to any other
group/supergroup, or receives a command in one it was already sitting in, it replies
with a short message pointing at its own `@<bot_username>` for DMs, then calls
`leaveChat` on itself (`_reject_unauthorized_chat` in `lambda_handler.py`). This only
ever fires for groups/supergroups — private chats with the bot are never restricted,
since the rejection message's whole point is to send people to a DM. The join-time check
uses the `my_chat_member` webhook update (Telegram's default `allowed_updates` already
includes it, no webhook re-registration needed); note this same update type also fires
for private chats on block/unblock, which is why the handler gates on `chat.type` first.

Set the real value once, per environment (Terraform creates the parameter as a
`replace_me!` placeholder and ignores changes to its value):
```bash
aws ssm put-parameter --name "/ttrpg_club/<env>/telegram_club_chat_id" \
  --value "<numeric_chat_id>" --type SecureString --overwrite
```

## Configuration

Edit `aws_infra/lambda/ttrpg_poll_bot_<env>/variables.tf` (region, `mini_app_deep_link`,
`bot_username`, `club_website_url`) or `main.tf` (memory, timeout, environment variables)
— these now live in Terraform, not `serverless.yml` (removed as part of the Terraform
migration). `club_website_url` defaults to `""` (no default set — dev and prod point at
different sites) and is linked from `/start`; until it's set, `/start` sends its
description text without a website button. The two chat IDs (`admin_chat_id`,
`allowed_chat_id`) aren't Terraform variables at all — they're kept out of source control
entirely and read from SSM at runtime (see Chat Restriction above and the Setup section's
SSM parameters).

## Local Development

The original polling-based bot is still in [main.py](main.py) for local testing:

```bash
export TELEGRAM_BOT_TOKEN="your_token"
python main.py
```

## Logs

```bash
aws logs tail /aws/lambda/telegram-poll-bot-prod-webhook --follow
```

**Prod also alerts to Telegram automatically** — the 3 prod functions log structured
JSON (`logging_log_format = "JSON"` in Terraform) so every `ERROR`-level line has a
reliable `level` field; a CloudWatch Logs subscription filter forwards those straight to
a small Lambda that DMs the admin chat, reusing this bot's own token
(`aws_infra/monitoring/ttrpg_club_prod_alerts`). Dev deliberately isn't wired up to
this — active development produces expected errors that aren't actionable alerts.

## Cleanup

```bash
cd aws_infra/lambda/ttrpg_poll_bot_<env> && terraform destroy
```

Don't forget to delete that environment's SSM parameters:
```bash
aws ssm delete-parameter --name "/ttrpg_club/<env>/poll_bot/token"
aws ssm delete-parameter --name "/ttrpg_club/<env>/telegram_admin_chat_id"
aws ssm delete-parameter --name "/ttrpg_club/<env>/telegram_club_chat_id"
```

## Dev vs Prod (two independent bots)

Dev and prod are two completely separate Telegram bots, each with its own registered
webhook, own IAM role, and own DynamoDB tables (`ttrpg_club_dev_*` / `ttrpg_club_prod_*`)
— nothing is shared, so testing in dev can never touch real prod data or vice versa.
Set each one up via the Setup section above, once per environment. For the dev bot's
Mini App (stats/feedback pages), apply `aws_infra/s3_cloudfront/ttrpg_club_frontend_dev`
(the website's dev frontend — see that repo's README), register a Mini App with
@BotFather pointing at its CloudFront domain, and set
`-var="mini_app_deep_link=https://t.me/<dev_bot>/<app>"` when applying
`lambda/ttrpg_poll_bot_dev`.

Routine code pushes update both bots' functions together in one CI run (see the GitHub
Actions section below, and the manual command list under "Routine code changes" above)
— since the code is identical, there's no separate per-environment build/deploy step.

## Data Model

All tables live in `aws_infra/dynamodb/ttrpg_club/<env>` (one Terraform state, all 11
of that project's tables — see `../ttrpg_website2/README.md` for the other 8, which
this bot never touches). Point-in-time recovery is enabled on all prod tables — see
`aws_infra/dynamodb/ttrpg_club/prod`.

| Table | Key | Written by | Read by | Purpose |
|---|---|---|---|---|
| `telegram_rating_polls` | `pollId` (+ `creatorUserId-index` GSI) | This bot (`handle_poll_command`) | This bot, website's Mini App API | One row per `/rate` poll — question text, GM (`creatorUserId`, captured from the command's sender). |
| `telegram_rating_votes` | `pollId` + `telegramUserId` (+ `telegramUserId-index` GSI) | This bot (`handle_poll_answer`) | Website's Mini App API | One row per person's current rating on a poll. The row's existence *is* the vote — a retraction or switch to "view results" **deletes** the row rather than storing a null rating. |
| `telegram_feedback` | `pollId` + `feedbackId` | Website's Mini App (`POST /telegram/feedback`) | This bot (`notify_new_feedback`, via a DynamoDB Stream) | Detailed per-session feedback submitted through the Mini App form — this bot only reads it (to DM the GM), never writes it. |
| `signup_requests` *(website-owned)* | `requestId` | Website backend | This bot (`notify_new_signup`, via a DynamoDB Stream) | Not one of this bot's own tables — `notifySignup` just consumes its Stream to DM the admin about new club applications. |

Three SSM parameters per environment, Terraform-managed placeholders (`aws_ssm_parameter`
with `value = "replace_me!"` and `ignore_changes` on value, so Terraform never
overwrites what you actually set — see Setup above): `/ttrpg_club/<env>/poll_bot/token`
(the bot token), `/ttrpg_club/<env>/telegram_admin_chat_id` (the admin's personal chat,
for signup notifications), and `/ttrpg_club/<env>/telegram_club_chat_id` (the one group
this bot is allowed to operate in — see Chat Restriction below). None of the three are
in source control; the Lambda reads each by name (`*_SSM_PARAMETER` env vars) and caches
the value for the life of the warm container.

## Backfilling Historical Data

If the bot was running (creating `/rate` polls) before DynamoDB tracking existed,
`scripts/backfill_historical_polls.py` can recover that history straight from Telegram:
it logs in as a personal account (MTProto — the Bot API has no method to read chat
history at all) and walks the chat, matching each poll back to the `/rate` command that
created it and fetching every voter. See the script's own docstring for the full
`check` → `scan` → `load` workflow, safety notes (it's read/inspect-before-you-write
throughout), and why it sometimes needs to cast a real "Подивитись відповідь" vote to
unlock a poll's results (harmless — the live bot treats that option as "not a rating").

## Club Signup Notifications (`notifySignup`)

A second function, `notifySignup`, is triggered directly by the `ttrpg_club_signup_requests`
DynamoDB Stream from the separate `ttrpg_website2`/`aws_infra` project — it posts a message
to the admin's chat (fetched from `/ttrpg_club/<env>/telegram_admin_chat_id` via SSM)
whenever someone submits a new club membership request. It reuses this bot's existing
token (same SSM parameter, `Bot.send_message`), so no second bot is needed.

The stream ARN needs no manual wiring — `aws_infra`'s Terraform for that environment's
`signup_requests` table publishes it to SSM as `/ttrpg_club/<env>/signup_requests_stream_arn`,
which `lambda/ttrpg_poll_bot_<env>`'s Terraform reads directly via a
`data "aws_ssm_parameter"` block. Just make sure that table's Terraform has been applied
at least once before applying this one.

## Personal Stats Mini App (`/stats`)

Every `/rate` poll is non-anonymous, so Telegram sends this bot a `poll_answer` webhook
update whenever someone votes — the `webhook` function now records these (who rated
what, when) into two new DynamoDB tables managed by the `ttrpg_website2`/`aws_infra`
Terraform: `ttrpg_club_<env>_telegram_rating_polls` (which poll_id was rating which
text, plus who created it — see below) and `ttrpg_club_<env>_telegram_rating_votes`
(the actual votes).
The "Подивитись відповідь" / view-results option (index 0) is never stored as a rating —
and neither is a **retracted** vote: if someone taps their selection again to deselect it,
or switches to "view results" after having voted, Telegram sends a `poll_answer` with
that new state, and the bot deletes any previously stored rating for them. This matters
because it's how "My Games Played" (below) decides who actually played a session.

Each poll's item also remembers who ran it (`creatorUserId` etc., captured from the
`/rate` command's sender) — that's the session's GM for stats purposes, and who gets
DMed detailed feedback (see the next section).

The `ttrpg_website2` frontend has a standalone Mini App page (`/telegram`) that reads
Telegram's signed `initData` (proof of identity — no separate login) and calls a new
`POST /telegram/stats` endpoint on the club's own API to show a user their own rating
history. That endpoint needs `ssm:GetParameter` on this bot's token (to verify
`initData`'s signature) and read access to the votes table — both already wired into
`aws_infra`'s `lambda/ttrpg_club_api_<env>` Terraform (each environment's website
backend reads the bot token from that same environment's
`/ttrpg_club/<env>/poll_bot/token`).

**People who voted don't need to be registered on the club website at all** — stats are
keyed purely by Telegram user ID, independent of the website's own member accounts.

To make `/stats` actually open the Mini App, you need to register it with @BotFather
once (Telegram only allows `web_app` inline buttons in private chats, not this bot's
group chat — so `/stats` instead sends a plain URL button using a `t.me/<bot>/<app>`
deep link, which works everywhere):

1. Message [@BotFather](https://t.me/botfather): `/newapp`, pick this bot, give it a
   name/short name (e.g. `stats`), and when asked for the Web App URL, use your
   deployed site's `/telegram` page — e.g. `https://your-cloudfront-domain/telegram`.
2. Apply with the resulting deep link:
   ```bash
   terraform apply -var="mini_app_deep_link=https://t.me/your_bot/stats"
   ```

Until `mini_app_deep_link` is set, `/stats` replies with a "temporarily unavailable"
message instead of a broken button.

The Mini App also has three navigation buttons under `/stats` — **My Games Played**,
**My Games Conducted**, **All Games** — each date-range filterable and drilling down
into a per-session voter breakdown. These read from the same two tables above (plus a
`creatorUserId-index` GSI on the polls table for "conducted") via new `POST
/telegram/games/*` endpoints on the club API — no bot changes needed for this part.

A fourth button, **🏆 Leaderboard**, shows two top-10 lists — most games played, most
games run as GM — with standard competition ranking (ties share a place; e.g. two
people tied for 1st are both shown 🥇, the next distinct score is 3rd, never 2nd), gold/
silver/bronze medals for the top 3, and a default filter of "this month" (with buttons
for last month, all time, or a custom range). Backed by `POST /telegram/leaderboard`.
One deliberate exclusion: a GM's own vote on their own session doesn't count as them
"playing" it — only counted on the GM board, not the player board.

## Session Feedback (`?startapp=feedback_<pollId>`)

Alongside the quick 1-10 poll, every `/rate` also sends a second message with a
**"📝 Залишити фідбек"** button — a `t.me/<bot>/<app>?startapp=feedback_<pollId>` deep
link that opens the Mini App straight to that session's detailed feedback form (four
1-10 questions — adventure/story, table, GM, self — plus optional free text). This is
deliberately separate from the quick poll vote: it's a private, mostly-anonymous channel
for the GM, not part of the public rating/player-list bookkeeping above.

**Gated on having actually voted**: the Mini App checks `POST /telegram/feedback/eligibility`
before showing the form, and `POST /telegram/feedback` re-checks the same thing
server-side — both look up `telegramUserId` + `pollId` in `telegram_rating_votes`.
Someone who hasn't rated the session sees an explanatory message instead of the form
(this bot doesn't enforce it — it's the website backend's job, see
`../ttrpg_website2/README.md`'s Data Model).

Feedback submissions are stored in a third new table, `ttrpg_club_telegram_feedback`
(also in `aws_infra`'s Terraform). Delivery to the GM works the same way as
`notifySignup`: a DynamoDB Stream on that table triggers this repo's `notifyFeedback`
function (`lambda_handler.notify_new_feedback`), which DMs the GM (looked up via the
poll's `creatorUserId`) with the feedback content — revealing the submitter's identity
only if they checked "reveal" in the form, otherwise the DM just says "Анонімно".

**Note**: a Telegram bot can only DM a user who has already started a private chat with
it at least once — if the GM never has, the DM silently fails (logged as a warning, not
an error) rather than crashing. If GMs report not receiving feedback, the fix is usually
just having them send `/start` to the bot in a private chat once.

Like the signup notifications above, this table's stream ARN needs no manual wiring —
`aws_infra`'s Terraform for that environment's `telegram_feedback` table publishes it to
SSM as `/ttrpg_club/<env>/telegram_feedback_stream_arn`, which
`lambda/ttrpg_poll_bot_<env>`'s Terraform reads directly. Just make sure that table's
Terraform has been applied at least once first.

## GitHub Actions Deployment

Pushing to `develop` or `main` deploys automatically via `.github/workflows/deploy.yml`
(or trigger it by hand from the Actions tab — it also has `workflow_dispatch`, which
uses whichever branch you run it from): build the Python package, zip it, and
`aws lambda update-function-code` on **only that branch's 3 functions** —
`develop` → `telegram-poll-bot-dev-*`, `main` → `telegram-poll-bot-prod-*` (the
workflow picks the name prefix from `github.ref_name`). Same OIDC pattern and the same
"just push code" shape `ttrpg_website2`'s backend pipeline uses (no
CloudFormation/Serverless involved, so no `SERVERLESS_ACCESS_KEY` or similar is needed
here). One-time setup:

1. **Apply the infrastructure first** — both `aws_infra/lambda/ttrpg_poll_bot_dev` and
   `.../ttrpg_poll_bot_prod` (see Setup above) — CI only ever updates code on functions
   that already exist.
2. **Apply the deploy role's Terraform** — `aws_infra/iam/github_actions_ttrpg_poll_bot`.
   A role dedicated to this repo (reuses the OIDC provider `ttrpg_website2`'s pipeline
   already created, but doesn't share its role). `terraform output deploy_role_arn`
   afterward gives you the value for the next step. If `AssumeRoleWithWebIdentity` fails
   with a `sub` claim mismatch, see that module's `variables.tf` comment — the same
   "immutable IDs" issue hit during `ttrpg_website2`'s setup can recur here since it's a
   different GitHub account.
3. **In this repo's GitHub Settings → Secrets and variables → Actions**, set secret
   `AWS_DEPLOY_ROLE_ARN` to the ARN from step 2.

This role is intentionally narrow — just `lambda:UpdateFunctionCode` +
`GetFunctionConfiguration`, scoped by a `telegram-poll-bot-*` name pattern that covers
both stacks' 6 functions (the workflow itself is what limits each run to just the 3
matching the pushed branch). All the broader stack-management permissions a
CloudFormation-driven deploy would have needed are gone now that Terraform owns the
infrastructure directly.

## Cost

AWS Lambda free tier includes:
- 1M requests/month
- 400,000 GB-seconds of compute time/month

This bot should stay within free tier for moderate usage. Two small additions beyond
Lambda itself, both negligible at this project's table sizes: point-in-time recovery on
the prod DynamoDB tables (~$0.20/GB-month) and the error-alerting Lambda in
`aws_infra/monitoring/ttrpg_club_prod_alerts`, which costs nothing beyond its own
(effectively free-tier) invocations — it's a plain CloudWatch Logs subscription filter,
deliberately not a CloudWatch Alarm + SNS setup, since alarms bill a flat monthly fee
per alarm whether or not they ever fire.
