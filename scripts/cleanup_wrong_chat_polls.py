#!/usr/bin/env python3
"""
One-off tool to delete "phantom" telegram_rating_polls (and everything derived from
them: telegram_rating_votes, telegram_feedback, and any telegram_xp_ledger entries) for
polls that were created OUTSIDE the club's configured chat — most commonly a poll
created in a DM with the bot, before lambda_handler.py's update_handler started
rejecting /rate anywhere except the configured chat.

These rows should never have existed at all — they're not "historical data to keep but
hide", they're bad data from the same bug the /rate gate now fixes at the source. This
script exists purely to clean up what already accumulated before that fix shipped.
Website endpoints (stats, leaderboard, games lists) don't filter by chat at read time —
they trust telegram_rating_polls/telegram_rating_votes completely — so deleting the
phantom rows outright is what actually stops them showing up there, not just in
gamification.

After running this with --apply, also wipe telegram_player_level/telegram_achievements
and re-run backfill_gamification.py --apply: this script doesn't touch either table
(weekly_bonus ledger entries don't reference a specific pollId, so a bonus partly earned
via a phantom vote can't be surgically corrected here) — a full backfill re-run
recomputes everything correctly from the now-cleaned tables.

Dry-run by default — prints exactly what would be deleted, deletes nothing. Pass
--apply to actually delete.

Usage:
    python cleanup_wrong_chat_polls.py \
        --rating-polls-table ttrpg_club_dev_telegram_rating_polls \
        --rating-votes-table ttrpg_club_dev_telegram_rating_votes \
        --feedback-table ttrpg_club_dev_telegram_feedback \
        --xp-ledger-table ttrpg_club_dev_telegram_xp_ledger \
        --allowed-chat-id -1002578567898 \
        --region eu-west-2 [--apply]
"""
from __future__ import annotations

import argparse

import boto3
from boto3.dynamodb.conditions import Key


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

    print(f"Scanning {args.rating_polls_table} ...")
    polls = _scan_table(polls_table)
    wrong_chat_polls = [
        p for p in polls if p.get("chatId") is None or int(p["chatId"]) != args.allowed_chat_id
    ]
    wrong_chat_poll_ids = {p["pollId"] for p in wrong_chat_polls}

    if not wrong_chat_poll_ids:
        print(f"No polls found outside chat {args.allowed_chat_id}. Nothing to do.")
        return

    print(f"\n{len(wrong_chat_poll_ids)} poll(s) created outside chat {args.allowed_chat_id}:")
    for p in wrong_chat_polls:
        print(
            f"    {p['pollId']} — chatId={p.get('chatId')}, "
            f"creatorUserId={p.get('creatorUserId')}, question={p.get('questionText')!r}"
        )

    votes_to_delete = []
    for poll_id in wrong_chat_poll_ids:
        result = votes_table.query(KeyConditionExpression=Key("pollId").eq(poll_id))
        votes_to_delete.extend(result.get("Items", []))

    feedback_to_delete = []
    for poll_id in wrong_chat_poll_ids:
        result = feedback_table.query(KeyConditionExpression=Key("pollId").eq(poll_id))
        feedback_to_delete.extend(result.get("Items", []))

    print(f"\nScanning {args.xp_ledger_table} for related entries ...")
    ledger_source_ids = {f"vote#{pid}" for pid in wrong_chat_poll_ids} | {
        f"feedback_bonus#{pid}" for pid in wrong_chat_poll_ids
    }
    all_ledger_items = _scan_table(ledger_table)
    ledger_to_delete = [item for item in all_ledger_items if item.get("sourceId") in ledger_source_ids]

    print("\nWill delete:")
    print(f"  {len(wrong_chat_poll_ids)} poll(s) from {args.rating_polls_table}")
    print(f"  {len(votes_to_delete)} vote(s) from {args.rating_votes_table}")
    print(f"  {len(feedback_to_delete)} feedback submission(s) from {args.feedback_table}")
    print(f"  {len(ledger_to_delete)} XP ledger entr{'y' if len(ledger_to_delete) == 1 else 'ies'} from {args.xp_ledger_table}")

    if not args.apply:
        print(
            "\nDry run — nothing deleted. Re-run with --apply to actually delete, then "
            "wipe telegram_player_level/telegram_achievements and re-run "
            "backfill_gamification.py --apply to reconcile XP/levels/achievements."
        )
        return

    with polls_table.batch_writer() as batch:
        for poll_id in wrong_chat_poll_ids:
            batch.delete_item(Key={"pollId": poll_id})

    with votes_table.batch_writer() as batch:
        for item in votes_to_delete:
            batch.delete_item(Key={"pollId": item["pollId"], "telegramUserId": item["telegramUserId"]})

    with feedback_table.batch_writer() as batch:
        for item in feedback_to_delete:
            batch.delete_item(Key={"pollId": item["pollId"], "feedbackId": item["feedbackId"]})

    with ledger_table.batch_writer() as batch:
        for item in ledger_to_delete:
            batch.delete_item(Key={"telegramUserId": item["telegramUserId"], "sourceId": item["sourceId"]})

    print(
        "\nDone. Now wipe telegram_player_level/telegram_achievements and re-run "
        "backfill_gamification.py --apply to reconcile XP/levels/achievements."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rating-polls-table", required=True)
    parser.add_argument("--rating-votes-table", required=True)
    parser.add_argument("--feedback-table", required=True)
    parser.add_argument("--xp-ledger-table", required=True)
    parser.add_argument("--allowed-chat-id", required=True, type=int)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run)")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
