#!/usr/bin/env python3
"""
Pulls every piece of extended feedback (the Mini App's 4-rating + free-text form) left
for one GM's sessions, formatted the same way each item is DMed to the GM by
notify_new_feedback in lambda_handler.py (see _feedback_message_text there) — grouped
by session, newest first, so a GM can read their whole history in one document instead
of hunting through old DMs.

Read-only — never writes to DynamoDB.

Usage:
    python export_gm_feedback.py \
        --rating-polls-table ttrpg_club_prod_telegram_rating_polls \
        --feedback-table ttrpg_club_prod_telegram_feedback \
        --users-table ttrpg_club_prod_users \
        --region eu-west-2 \
        --gm-name "Олег Гук-Сатайкін"

    # or, if you already know their Telegram id:
    python export_gm_feedback.py ... --gm-user-id 123456789

    # write to a file instead of stdout:
    python export_gm_feedback.py ... --gm-name "Олег" --output oleh_feedback.txt
"""
from __future__ import annotations

import argparse
import re
import sys

import boto3
from boto3.dynamodb.conditions import Key


def _scan_table(table) -> list:
    items = []
    kwargs: dict = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def _query_all(table, **kwargs) -> list:
    items = []
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


def format_game_title(question_text: str) -> str:
    """Mirrors shared/src/gameSystems.ts's formatGameTitle: strips the "Оцінка (...)"
    wrapper /rate polls are created with; falls back to the raw text otherwise."""
    match = re.match(r"^Оцінка\s*\((.+)\)$", question_text or "", re.DOTALL)
    return match.group(1) if match else question_text


def feedback_message_text(feedback: dict) -> str:
    """Same formatting as _feedback_message_text in lambda_handler.py — the GM's DM."""
    if feedback.get("revealIdentity"):
        first = feedback.get("submitterFirstName") or ""
        last = feedback.get("submitterLastName") or ""
        username = feedback.get("submitterUsername") or ""
        who = f"{first} {last}".strip() or "Учасник"
        if username:
            who += f" (@{username})"
    else:
        who = "Анонімно"

    lines = [
        "📝 Новий фідбек про сесію",
        f"Від: {who}",
        "",
        f"Пригода/історія: {feedback.get('adventureRating', '—')}/10",
        f"Стіл: {feedback.get('tableRating', '—')}/10",
        f"Майстер: {feedback.get('gmRating', '—')}/10",
        f"Себе: {feedback.get('selfRating', '—')}/10",
    ]

    text = feedback.get("feedbackText", "")
    if text:
        lines += ["", text]

    return "\n".join(lines)


def resolve_gm(users_table, gm_name: str | None, gm_user_id: int | None) -> tuple[int, str]:
    """Returns (creatorUserId as int, a display label) for the GM to filter polls by."""
    if gm_user_id is not None:
        return gm_user_id, str(gm_user_id)

    users = _scan_table(users_table)
    needle = gm_name.strip().lower()
    matches = [
        u for u in users if needle in f"{u.get('firstName', '')} {u.get('lastName', '')}".strip().lower()
    ]
    if not matches:
        sys.exit(f'No user matches "{gm_name}". Try --gm-user-id with their Telegram id instead.')
    if len(matches) > 1:
        listing = "\n".join(
            f"  - {u.get('firstName', '')} {u.get('lastName', '')} (userId {u['userId']})" for u in matches
        )
        sys.exit(f'"{gm_name}" matches more than one user:\n{listing}\nNarrow it down or pass --gm-user-id.')

    user = matches[0]
    display = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
    return int(user["userId"]), display


def run(args) -> None:
    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    polls_table = dynamodb.Table(args.rating_polls_table)
    feedback_table = dynamodb.Table(args.feedback_table)
    users_table = dynamodb.Table(args.users_table)

    gm_user_id, gm_label = resolve_gm(users_table, args.gm_name, args.gm_user_id)

    # creatorUserId-index — same GSI getTelegramGamesConducted queries on the website side.
    polls = _query_all(
        polls_table,
        IndexName="creatorUserId-index",
        KeyConditionExpression=Key("creatorUserId").eq(gm_user_id),
    )
    polls.sort(key=lambda p: p.get("createdAt", ""), reverse=True)

    sections = []
    total_feedback = 0
    for poll in polls:
        feedback_items = _query_all(
            feedback_table, KeyConditionExpression=Key("pollId").eq(poll["pollId"])
        )
        if not feedback_items:
            continue
        feedback_items.sort(key=lambda f: f.get("submittedAt", ""))

        title = format_game_title(poll.get("questionText", ""))
        date = (poll.get("createdAt") or "")[:10]
        header = f"{title} ({date})" if date else title

        blocks = [header, "=" * len(header), ""]
        for i, fb in enumerate(feedback_items):
            blocks.append(feedback_message_text(fb))
            if i < len(feedback_items) - 1:
                blocks.append("\n" + ("-" * 40) + "\n")
        sections.append("\n".join(blocks))
        total_feedback += len(feedback_items)

    report = f"Фідбек для {gm_label} — {len(sections)} сесій, {total_feedback} відгуків\n\n"
    report += ("\n\n" + ("#" * 60) + "\n\n").join(sections)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Written to {args.output} ({len(sections)} sessions, {total_feedback} feedback items).")
    else:
        print(report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rating-polls-table", required=True)
    parser.add_argument("--feedback-table", required=True)
    parser.add_argument("--users-table", required=True)
    parser.add_argument("--region", default="eu-west-2")
    gm_group = parser.add_mutually_exclusive_group(required=True)
    gm_group.add_argument("--gm-name", help="Substring match against the GM's first+last name")
    gm_group.add_argument("--gm-user-id", type=int, help="The GM's Telegram user id directly")
    parser.add_argument("--output", help="Write the report to this file instead of stdout")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
