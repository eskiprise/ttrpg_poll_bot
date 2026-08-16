#!/usr/bin/env python3
"""
One-off tool to backfill telegram_xp_ledger / telegram_player_level / telegram_achievements
from data ALREADY in DynamoDB (telegram_rating_polls, telegram_rating_votes,
telegram_feedback). Unlike backfill_historical_polls.py, this needs no MTProto/Telegram
API access at all — the source data is already live in the tables the bot itself writes
to; this just derives the gamification tables from it.

Mirrors lambda_handler.py's gamification logic (XP amounts, the level formula,
achievement thresholds) — keep both in sync if either changes.

Historical scope: only votes/feedback dated on/after --cutoff (default 2026-01-01) count
toward XP AND the gamesPlayed/feedbackGiven achievement counters — one consistent
historical scan for the whole feature, even though the live bot's achievement counters
are NOT cutoff-limited (see the design plan for why this script intentionally differs).

GM exclusion is a hard requirement, not a "close enough" one: this script prints every
poll with a missing/null creatorUserId prominently, since backfill_historical_polls.py
can leave that null for polls it couldn't attribute — such a poll's voters can't be
excluded as the GM and may incorrectly earn XP/achievements. Fix these (or explicitly
accept the risk) before running with --apply.

Dry-run by default — prints a full summary, writes nothing. Pass --apply to actually
write. Safe to re-run at any time as a reconciliation tool: ledger/achievement rows are
only ever added, never duplicated (skipped if already present, preserving their original
awardedAt/unlockedAt) — but the final telegram_player_level row is always fully
recomputed and overwritten from the current qualifying votes/feedback, so a later re-run
(e.g. to fix drift from a live optimistic-lock exhaustion, or "recalculate later" once
the real title/threshold values are settled) is a safe, idempotent reconciliation.

Usage:
    python backfill_gamification.py \
        --rating-polls-table ttrpg_club_dev_telegram_rating_polls \
        --rating-votes-table ttrpg_club_dev_telegram_rating_votes \
        --feedback-table ttrpg_club_dev_telegram_feedback \
        --xp-ledger-table ttrpg_club_dev_telegram_xp_ledger \
        --player-level-table ttrpg_club_dev_telegram_player_level \
        --achievements-table ttrpg_club_dev_telegram_achievements \
        --region eu-west-2 [--cutoff 2026-01-01] [--apply]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone

import boto3

XP_VOTE = 10
XP_WEEKLY_BONUS = 5
XP_FEEDBACK_BONUS = 2
WEEKLY_BONUS_VOTES_REQUIRED = 2

LEVEL_UP_BASE_XP = 100
LEVEL_UP_MULTIPLIER = 1.2
MAX_PLAYER_LEVEL = 10

GAMES_PLAYED_ACHIEVEMENT_THRESHOLDS = [1, 10, 50, 100]
FEEDBACK_GIVEN_ACHIEVEMENT_THRESHOLDS = [1, 10, 20, 50]


def _compute_level_thresholds() -> list:
    thresholds = [LEVEL_UP_BASE_XP]
    while len(thresholds) < MAX_PLAYER_LEVEL - 1:
        thresholds.append(int((thresholds[-1] * LEVEL_UP_MULTIPLIER) // 10) * 10)
    return thresholds


LEVEL_THRESHOLDS = _compute_level_thresholds()


def apply_xp(level: int, current_xp: int, gained_xp: int) -> tuple:
    """Same order-independent formula as lambda_handler.py's apply_xp — summing a
    user's total XP once and applying it here produces an identical result to replaying
    every individual award in order, since this is just successive fixed-radix
    reduction against a fixed set of thresholds."""
    xp = current_xp + gained_xp
    while level < MAX_PLAYER_LEVEL:
        threshold = LEVEL_THRESHOLDS[level - 1]
        if xp < threshold:
            break
        xp -= threshold
        level += 1
    return level, xp


def _iso_week_key(iso_timestamp: str) -> str:
    year, week, _ = datetime.fromisoformat(iso_timestamp).isocalendar()
    return f"{year}-W{week:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_table(table) -> list:
    items = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def run(args) -> None:
    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    polls_table = dynamodb.Table(args.rating_polls_table)
    votes_table = dynamodb.Table(args.rating_votes_table)
    feedback_table = dynamodb.Table(args.feedback_table)
    ledger_table = dynamodb.Table(args.xp_ledger_table)
    player_level_table = dynamodb.Table(args.player_level_table)
    achievements_table = dynamodb.Table(args.achievements_table)

    cutoff = f"{args.cutoff}T00:00:00+00:00"

    print(f"Scanning {args.rating_polls_table} ...")
    polls = _scan_table(polls_table)
    creator_by_poll = {p.get("pollId"): p.get("creatorUserId") for p in polls}
    missing_creator_polls = [poll_id for poll_id, creator in creator_by_poll.items() if creator is None]
    if missing_creator_polls:
        print(
            f"\n⚠ {len(missing_creator_polls)} poll(s) have no recorded creatorUserId — "
            "their voters CANNOT be excluded as the GM and may incorrectly earn XP/achievements:"
        )
        for poll_id in missing_creator_polls:
            print(f"    {poll_id}")
        print("Fix these in telegram_rating_polls (or accept the risk) before running with --apply.\n")

    print(f"Scanning {args.rating_votes_table} ...")
    votes = _scan_table(votes_table)
    print(f"Scanning {args.feedback_table} ...")
    feedback_items = _scan_table(feedback_table)

    qualifying_votes = [
        v
        for v in votes
        if v.get("answeredAt", "") >= cutoff and v.get("telegramUserId") != creator_by_poll.get(v.get("pollId"))
    ]
    qualifying_feedback = [
        f
        for f in feedback_items
        if f.get("submittedAt", "") >= cutoff and f.get("telegramUserId") != creator_by_poll.get(f.get("pollId"))
    ]
    print(f"{len(qualifying_votes)} / {len(votes)} votes qualify (>= {cutoff}, excluding the poll's GM)")
    print(f"{len(qualifying_feedback)} / {len(feedback_items)} feedback submissions qualify")

    # Candidate ledger entries: {(telegramUserId, sourceId): {xp, type, awardedAt}}
    ledger_entries = {}
    for v in qualifying_votes:
        user_id = int(v["telegramUserId"])
        ledger_entries[(user_id, f"vote#{v['pollId']}")] = {
            "xp": XP_VOTE,
            "type": "vote",
            "awardedAt": v.get("answeredAt", cutoff),
        }

    # Weekly bonus — group each user's qualifying votes by ISO week, award once per
    # week they reach WEEKLY_BONUS_VOTES_REQUIRED, timestamped at that week's Nth vote.
    votes_by_user = defaultdict(list)
    for v in qualifying_votes:
        votes_by_user[int(v["telegramUserId"])].append(v.get("answeredAt", cutoff))
    for user_id, timestamps in votes_by_user.items():
        by_week = defaultdict(list)
        for ts in sorted(timestamps):
            by_week[_iso_week_key(ts)].append(ts)
        for week_key, ts_list in by_week.items():
            if len(ts_list) >= WEEKLY_BONUS_VOTES_REQUIRED:
                ledger_entries[(user_id, f"weekly_bonus#{week_key}")] = {
                    "xp": XP_WEEKLY_BONUS,
                    "type": "weekly_bonus",
                    "awardedAt": ts_list[WEEKLY_BONUS_VOTES_REQUIRED - 1],
                }

    for f in qualifying_feedback:
        user_id = int(f["telegramUserId"])
        ledger_entries[(user_id, f"feedback_bonus#{f['pollId']}")] = {
            "xp": XP_FEEDBACK_BONUS,
            "type": "feedback_bonus",
            "awardedAt": f.get("submittedAt", cutoff),
        }

    print(f"Checking {len(ledger_entries)} candidate ledger entries against what's already written ...")
    ledger_to_write = {}
    for (user_id, source_id), entry in ledger_entries.items():
        existing = ledger_table.get_item(Key={"telegramUserId": user_id, "sourceId": source_id}).get("Item")
        if not existing:
            ledger_to_write[(user_id, source_id)] = entry
    print(f"{len(ledger_to_write)} new ledger entries to write ({len(ledger_entries) - len(ledger_to_write)} already present)")

    games_played_by_user = defaultdict(int)
    for v in qualifying_votes:
        games_played_by_user[int(v["telegramUserId"])] += 1
    feedback_given_by_user = defaultdict(int)
    for f in qualifying_feedback:
        feedback_given_by_user[int(f["telegramUserId"])] += 1

    # Final level/XP: sum ALL qualifying XP (already-written + about-to-be-written) and
    # apply once — order-independent for a fixed total, no chronological replay needed.
    total_xp_by_user = defaultdict(int)
    for (user_id, _source_id), entry in ledger_entries.items():
        total_xp_by_user[user_id] += entry["xp"]

    all_user_ids = set(games_played_by_user) | set(feedback_given_by_user) | set(total_xp_by_user)

    player_level_rows = {}
    achievement_candidates = []
    for user_id in all_user_ids:
        level, current_xp = apply_xp(1, 0, total_xp_by_user.get(user_id, 0))
        games_played = games_played_by_user.get(user_id, 0)
        feedback_given = feedback_given_by_user.get(user_id, 0)
        player_level_rows[user_id] = {
            "telegramUserId": user_id,
            "level": level,
            "currentXp": current_xp,
            "totalXp": total_xp_by_user.get(user_id, 0),
            "gamesPlayed": games_played,
            "feedbackGiven": feedback_given,
            "updatedAt": _now_iso(),
            "version": 0,
        }
        for threshold in GAMES_PLAYED_ACHIEVEMENT_THRESHOLDS:
            if games_played >= threshold:
                achievement_candidates.append((user_id, f"games_played_{threshold}"))
        for threshold in FEEDBACK_GIVEN_ACHIEVEMENT_THRESHOLDS:
            if feedback_given >= threshold:
                achievement_candidates.append((user_id, f"feedback_given_{threshold}"))
        if level >= MAX_PLAYER_LEVEL:
            achievement_candidates.append((user_id, "max_level"))

    print(f"\nChecking {len(achievement_candidates)} candidate achievements against what's already unlocked ...")
    achievements_to_write = []
    for user_id, achievement_id in achievement_candidates:
        existing = achievements_table.get_item(Key={"telegramUserId": user_id, "achievementId": achievement_id}).get("Item")
        if not existing:
            achievements_to_write.append((user_id, achievement_id))
    print(f"{len(achievements_to_write)} new achievements to write ({len(achievement_candidates) - len(achievements_to_write)} already unlocked)")

    print(f"\n{len(player_level_rows)} player(s) will have their telegram_player_level row reconciled:")
    for user_id, row in sorted(player_level_rows.items()):
        print(
            f"  {user_id}: level {row['level']}, {row['currentXp']} XP into level "
            f"({row['totalXp']} total), {row['gamesPlayed']} games, {row['feedbackGiven']} feedback"
        )

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to actually write.")
        return

    print(f"\nWriting {len(ledger_to_write)} ledger entries ...")
    with ledger_table.batch_writer() as batch:
        for (user_id, source_id), entry in ledger_to_write.items():
            batch.put_item(
                Item={
                    "telegramUserId": user_id,
                    "sourceId": source_id,
                    "xp": entry["xp"],
                    "type": entry["type"],
                    "awardedAt": entry["awardedAt"],
                }
            )

    print(f"Writing {len(player_level_rows)} telegram_player_level rows (full reconciliation) ...")
    with player_level_table.batch_writer() as batch:
        for row in player_level_rows.values():
            batch.put_item(Item=row)

    print(f"Writing {len(achievements_to_write)} achievement rows ...")
    with achievements_table.batch_writer() as batch:
        for user_id, achievement_id in achievements_to_write:
            batch.put_item(
                Item={
                    "telegramUserId": user_id,
                    "achievementId": achievement_id,
                    "unlockedAt": _now_iso(),
                }
            )

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rating-polls-table", required=True)
    parser.add_argument("--rating-votes-table", required=True)
    parser.add_argument("--feedback-table", required=True)
    parser.add_argument("--xp-ledger-table", required=True)
    parser.add_argument("--player-level-table", required=True)
    parser.add_argument("--achievements-table", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--cutoff", default="2026-01-01", help="YYYY-MM-DD, inclusive (default: 2026-01-01)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default is dry-run)")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
