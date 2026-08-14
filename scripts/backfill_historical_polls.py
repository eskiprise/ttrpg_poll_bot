#!/usr/bin/env python3
"""
One-off tool to backfill telegram_rating_polls / telegram_rating_votes with polls the
bot sent BEFORE DynamoDB tracking existed. Not part of the Lambda deployment — never
imported by lambda_handler.py, never copied into build/ by build.sh.

Why this needs a user account (MTProto), not the bot: the Bot API has no method to
read chat history or list historical messages at all, and the one method that returns
per-voter poll results (messages.getPollVotes) explicitly cannot be called by bots —
see https://core.telegram.org/method/messages.getPollVotes. So this script logs in as
a regular Telegram user (via Telethon) who is a member of the club chat.

Three phases, run in order — each is safe to inspect before moving to the next:

  1. check  — fetches ONE known poll via MTProto and prints its id, so you can confirm
              it matches the pollId already stored in DynamoDB for that same poll
              before trusting anything else this script finds.
  2. scan   — walks the whole chat history, finds every /rate-created poll, fetches its
              voters, and writes everything to a local JSON file. Touches Telegram only,
              never AWS. Prints a summary including anything it couldn't resolve.
              Telegram hides a non-anonymous poll's results from you until you've voted
              in it yourself — by default this auto-casts "Подивитись відповідь" on any
              poll that needs it to unlock results (harmless: the live bot treats that
              option as "not a rating" and doesn't count it — see handle_poll_answer).
              Pass --no-vote-to-unlock to skip-and-report those polls instead, like the
              old behavior.
  3. load   — reads that JSON and writes to DynamoDB. Idempotent: skips any poll/vote
              that's already in the table (never overwrites live data), and defaults to
              --dry-run so you see exactly what would be written first.

Setup:
    pip install -r requirements-backfill.txt
    Get api_id / api_hash from https://my.telegram.org/apps (your personal account,
    not the bot) and export them:
        export TG_API_ID=12345678
        export TG_API_HASH=abcdef0123456789abcdef0123456789
    First run of `check` or `scan` will prompt for your phone number and login code
    (SMS/Telegram) and save a local session file (backfill.session) so you're not
    re-prompted every run. That session file is a live credential — it's already in
    .gitignore, never commit it, delete it when you're done with this script.

Usage:
    python backfill_historical_polls.py check --chat <chat_id_or_@username> --message-id <id>
    python backfill_historical_polls.py scan --chat <chat_id_or_@username> --out export.json [--since 2024-01-01] [--until 2025-06-01]
    python backfill_historical_polls.py load --in export.json \
        --polls-table ttrpg_club_dev_telegram_rating_polls \
        --votes-table ttrpg_club_dev_telegram_rating_votes \
        --region eu-west-2 [--yes]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

COMMAND_RE = re.compile(r"^/(rate|poll)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)

# lambda_handler.py's RATING_OPTIONS emoji have drifted at least once over the bot's
# history (confirmed: current "9/10"/"10/10" emoji don't match an earlier version) —
# an exact copy of today's list would silently reject every poll created under an older
# version. Match structurally on the "N / 10" numeric prefix instead, which is the one
# thing that's stayed constant since /rate's first version.
RATING_LABEL_RE = re.compile(r"^(\d{1,2})\s*/\s*10\b")


def _rating_poll_shape(answers) -> dict | None:
    """
    {option_bytes: rating (1-10)} for a poll that looks like one /rate created, or None
    if it doesn't (e.g. a /bool yes/no poll, or anything with a different shape).
    Requires exactly 11 options and exactly one match per rating 1-10 — anything looser
    risks matching an unrelated poll that just happens to contain "N / 10" somewhere.
    """
    if len(answers) != 11:
        return None
    mapping: dict = {}
    seen = set()
    for a in answers:
        m = RATING_LABEL_RE.match(a.text.text)
        if not m:
            continue
        n = int(m.group(1))
        if not (1 <= n <= 10) or n in seen:
            return None
        seen.add(n)
        mapping[a.option] = n
    return mapping if len(seen) == 10 else None


def _view_results_option(answers, rating_by_bytes: dict) -> bytes | None:
    """
    The one answer option that ISN'T a rating — "Подивитись відповідь" — found by
    exclusion (whatever's left after the 10 "N / 10" options) rather than assumed to be
    answers[0], since _rating_poll_shape() already deliberately doesn't rely on option
    order. None if the shape isn't what's expected (shouldn't happen for anything that
    already passed _rating_poll_shape, but this is called separately).
    """
    others = [a.option for a in answers if a.option not in rating_by_bytes]
    return others[0] if len(others) == 1 else None


def _expected_question(args: str) -> str:
    return f"Оцінка ({args})"


def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _get_client(session_name: str):
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        sys.exit("Telethon isn't installed — run: pip install -r requirements-backfill.txt")

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Set TG_API_ID and TG_API_HASH first (from https://my.telegram.org/apps)")

    client = TelegramClient(session_name, int(api_id), api_hash)
    client.start()
    return client


def _resolve_chat(client, chat_arg: str):
    """
    Resolve --chat to an entity Telethon can actually use. Two things trip this up for
    a private group/supergroup with no @username:

      1. Telegram's Bot API represents supergroup/channel ids as "-100<channel_id>" —
         that's a Bot API convention, not the real MTProto id. MTProto wants the bare
         channel_id (strip the "-100" prefix).
      2. Even with the right numeric id, resolving a peer requires its access_hash,
         which Telethon only has cached for chats already seen in your dialog list —
         a cold numeric id lookup fails with exactly the "Cannot find any entity"
         error you hit. Calling get_dialogs() once first populates that cache.
    """
    chat_arg = chat_arg.strip()

    if not chat_arg.lstrip("-").isdigit():
        return client.get_entity(chat_arg)  # @username or invite link — no cache needed

    raw_id = int(chat_arg)
    channel_id = int(str(abs(raw_id))[3:]) if str(abs(raw_id)).startswith("100") and raw_id < 0 else abs(raw_id)

    client.get_dialogs()  # populate the entity/access_hash cache

    from telethon.tl.types import PeerChannel, PeerChat
    for peer_type in (PeerChannel, PeerChat):
        try:
            return client.get_entity(peer_type(channel_id))
        except (ValueError, TypeError):
            continue

    # Last resort: search the now-cached dialog list by id, and if that also fails,
    # show what IS available so you can pick the right one.
    for dialog in client.iter_dialogs():
        if dialog.id in (raw_id, channel_id, -channel_id):
            return dialog.entity

    print("Could not resolve that chat id. Chats visible to this account:", file=sys.stderr)
    for dialog in client.iter_dialogs():
        print(f"  {dialog.id:>16}  {dialog.name}", file=sys.stderr)
    sys.exit(f"Pick the exact id shown above for your club chat and pass it as --chat.")


def cmd_list_chats(args):
    """List every chat this account is in, with a real message count, to help pick the
    right --chat for the other commands — a wrong chat (e.g. a small test group) scans
    fine but silently finds zero polls, which looks identical to a real bug."""
    client = _get_client(args.session)
    for dialog in client.iter_dialogs():
        kind = "channel" if dialog.is_channel else "group" if dialog.is_group else "user"
        count = client.get_messages(dialog.entity, limit=0).total
        print(f"{dialog.id:>16}  [{kind:7}]  {count:>6} messages  {dialog.name}")


def cmd_check(args):
    """Fetch one poll by message ID and print its MTProto id for cross-checking."""
    client = _get_client(args.session)
    from telethon.tl.types import MessageMediaPoll

    chat = _resolve_chat(client, args.chat)
    msg = client.get_messages(chat, ids=args.message_id)
    if msg is None:
        sys.exit(f"No message with id {args.message_id} found in {args.chat}")
    if not isinstance(msg.media, MessageMediaPoll):
        sys.exit(f"Message {args.message_id} isn't a poll (type: {type(msg.media).__name__})")

    poll = msg.media.poll
    question = poll.question.text  # Poll.question is TextWithEntities, not a plain str
    print(f"Message date:       {msg.date.isoformat()}")
    print(f"Poll question:      {question}")
    print(f"MTProto poll.id:    {poll.id}")
    print()
    print("Compare this to the pollId already stored in DynamoDB for the same poll:")
    print(f"  aws dynamodb scan --table-name <your_polls_table> --region eu-west-2 \\")
    print(f'    --filter-expression "questionText = :q" \\')
    print(f'    --expression-attribute-values \'{{":q": {{"S": "{question}"}}}}\'')
    print()
    print("If the pollId in that scan output matches poll.id above (as a string), IDs")
    print("are compatible and it's safe to move on to `scan`.")


def cmd_scan(args):
    client = _get_client(args.session)
    from telethon.tl.types import MessageMediaPoll, MessagePeerVote, MessagePeerVoteMultiple, PeerUser
    from telethon.tl.functions.messages import GetPollVotesRequest, SendVoteRequest
    from telethon.errors import PollVoteRequiredError, FloodWaitError
    import time

    chat = _resolve_chat(client, args.chat)
    since = _parse_date(args.since) if args.since else None
    until = _parse_date(args.until) if args.until else None

    command_candidates = []  # [{message_id, date, sender, expected_question}]
    polls = []               # [{pollId, questionText, chatId, createdAt, creator*, messageThreadId}]
    votes = []                # [{pollId, telegramUserId, username, firstName, lastName, rating, questionText, answeredAt}]
    blocked_polls = []        # polls where votes still couldn't be fetched after an auto-vote attempt (or auto-vote is off)
    unlocked_polls = []       # polls where "Подивитись відповідь" was auto-voted to reveal results
    unmatched_creator = []    # polls where no preceding /rate command matched

    scanned = 0
    print(f"Scanning {args.chat}...", file=sys.stderr)

    for message in client.iter_messages(chat, reverse=True):
        scanned += 1
        if scanned % 500 == 0:
            print(f"  ...{scanned} messages scanned, {len(polls)} rating polls found", file=sys.stderr)

        if since and message.date < since:
            continue
        if until and message.date > until:
            break

        text = (message.text or message.message or "").strip()
        m = COMMAND_RE.match(text)
        if m and message.sender_id:
            sender = message.sender
            command_candidates.append({
                "message_id": message.id,
                "date": message.date,
                "sender_id": message.sender_id,
                "first_name": getattr(sender, "first_name", None),
                "last_name": getattr(sender, "last_name", None),
                "username": getattr(sender, "username", None),
                "expected_question": _expected_question((m.group(2) or "").strip()),
            })
            continue

        if not isinstance(message.media, MessageMediaPoll):
            continue

        poll = message.media.poll
        # Poll.question and PollAnswer.text are TextWithEntities (rich-text wrapper),
        # not plain strings, in current Telethon/TL — unwrap .text once up front.
        question = poll.question.text
        answers = poll.answers
        option_rating_by_bytes = _rating_poll_shape(answers)
        if option_rating_by_bytes is None:
            continue  # not one of our rating polls (e.g. a /bool yes-no poll)

        # Best preceding /rate or /poll command whose computed question text matches
        # this poll's actual question exactly — not just "nearest message before it".
        creator = None
        for cand in reversed(command_candidates):
            if cand["date"] > message.date:
                continue
            if cand["expected_question"] == question:
                creator = cand
                break

        thread_id = None
        reply_to = getattr(message, "reply_to", None)
        if reply_to is not None and getattr(reply_to, "forum_topic", False):
            thread_id = reply_to.reply_to_top_id or reply_to.reply_to_msg_id

        poll_row = {
            "pollId": str(poll.id),
            "questionText": question,
            "chatId": chat.id,
            "createdAt": message.date.isoformat(),
            "creatorUserId": creator["sender_id"] if creator else None,
            "creatorFirstName": creator["first_name"] if creator else None,
            "creatorLastName": creator["last_name"] if creator else None,
            "creatorUsername": creator["username"] if creator else None,
        }
        if thread_id is not None:
            poll_row["messageThreadId"] = thread_id
        polls.append(poll_row)

        if creator is None:
            unmatched_creator.append({"pollId": poll_row["pollId"], "question": question, "date": poll_row["createdAt"]})

        offset = ""
        poll_blocked = False
        voted_to_unlock = False
        while True:
            try:
                result = client(GetPollVotesRequest(peer=chat, id=message.id, offset=offset, limit=50))
            except PollVoteRequiredError:
                # Telegram hides a non-anonymous poll's results from you until you've
                # cast a vote in it yourself. "Подивитись відповідь" is built for
                # exactly this: the live bot's poll_answer handler treats option 0 as
                # "not a real rating" and clears any vote rather than recording one
                # (see lambda_handler.py's handle_poll_answer), so this can't pollute
                # anyone's stats. It DOES cast a real, live vote though — if this poll
                # is still open (the bot never closes them), that fires an actual
                # poll_answer webhook to whichever bot owns it, right now.
                if args.vote_to_unlock and not voted_to_unlock:
                    view_results_option = _view_results_option(answers, option_rating_by_bytes)
                    if view_results_option is None:
                        blocked_polls.append({"pollId": poll_row["pollId"], "question": question, "date": poll_row["createdAt"], "reason": "couldn't identify the 'view results' option"})
                        poll_blocked = True
                        break
                    try:
                        client(SendVoteRequest(peer=chat, msg_id=message.id, options=[view_results_option]))
                    except Exception as vote_err:
                        blocked_polls.append({"pollId": poll_row["pollId"], "question": question, "date": poll_row["createdAt"], "reason": f"auto-vote failed: {vote_err}"})
                        poll_blocked = True
                        break
                    voted_to_unlock = True
                    unlocked_polls.append({"pollId": poll_row["pollId"], "question": question, "date": poll_row["createdAt"]})
                    time.sleep(0.5)  # let the vote propagate before asking for results again
                    continue
                blocked_polls.append({"pollId": poll_row["pollId"], "question": question, "date": poll_row["createdAt"]})
                poll_blocked = True
                break
            except FloodWaitError as e:
                print(f"  FloodWait: sleeping {e.seconds}s...", file=sys.stderr)
                time.sleep(e.seconds + 1)
                continue

            users_by_id = {u.id: u for u in result.users}
            for v in result.votes:
                if isinstance(v, MessagePeerVoteMultiple):
                    continue  # not used by our single-choice rating polls
                if not isinstance(v, MessagePeerVote):
                    continue  # MessagePeerVoteInputOption etc. — not a cast vote
                if not isinstance(v.peer, PeerUser):
                    continue  # anonymous/channel vote — shouldn't happen in a group poll
                rating = option_rating_by_bytes.get(v.option)
                if rating is None:
                    continue  # "view results" or an unrecognized option — not a rating
                user_id = v.peer.user_id
                u = users_by_id.get(user_id)
                votes.append({
                    "pollId": poll_row["pollId"],
                    "telegramUserId": user_id,
                    "username": getattr(u, "username", None),
                    "firstName": getattr(u, "first_name", None),
                    "lastName": getattr(u, "last_name", None),
                    "rating": rating,
                    "questionText": question,
                    "answeredAt": v.date.isoformat() if v.date else poll_row["createdAt"],
                })

            if not result.next_offset:
                break
            offset = result.next_offset

        if not poll_blocked:
            unlocked_note = " [auto-voted to unlock]" if voted_to_unlock else ""
            print(f"  poll {poll_row['pollId']} ({question}): {sum(1 for v in votes if v['pollId'] == poll_row['pollId'])} votes{unlocked_note}", file=sys.stderr)

    out = {
        "chat": args.chat,
        "scanned_messages": scanned,
        "polls": polls,
        "votes": votes,
        "blocked_polls": blocked_polls,
        "unlocked_polls": unlocked_polls,
        "unmatched_creator": unmatched_creator,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print()
    print(f"Scanned {scanned} messages.")
    print(f"Found {len(polls)} rating polls, {len(votes)} votes.")
    if unlocked_polls:
        print(f"\n{len(unlocked_polls)} poll(s) required voting 'Подивитись відповідь' to reveal results —")
        print("done automatically. This cast a real vote on each (harmless: the live bot treats")
        print("that option as 'not a rating' and doesn't count it), and fired a live poll_answer")
        print("webhook to whichever bot owns each poll, right now, for any that are still open:")
        for p in unlocked_polls:
            print(f"  - {p['date']}  {p['question']}  (pollId {p['pollId']})")
    if blocked_polls:
        reason = "auto-vote-to-unlock is off (--no-vote-to-unlock)" if not args.vote_to_unlock else "even after voting to unlock — see each row's 'reason'"
        print(f"\n{len(blocked_polls)} poll(s) still skipped — {reason}:")
        for p in blocked_polls:
            extra = f"  ({p['reason']})" if p.get("reason") else ""
            print(f"  - {p['date']}  {p['question']}  (pollId {p['pollId']}){extra}")
    if unmatched_creator:
        print(f"\n{len(unmatched_creator)} poll(s) had no matching /rate command before them")
        print("(creatorUserId will be null — edit the JSON by hand if you know who ran these):")
        for p in unmatched_creator:
            print(f"  - {p['date']}  {p['question']}  (pollId {p['pollId']})")
    print(f"\nWrote {args.out}. Inspect it, then run `load` against dev tables first.")


def cmd_load(args):
    import boto3

    with open(args.__dict__["in"]) as f:
        data = json.load(f)

    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    polls_table = dynamodb.Table(args.polls_table)
    votes_table = dynamodb.Table(args.votes_table)

    # DynamoDB's BatchWriteItem rejects a batch containing two items with the same key
    # ("Provided list of item keys contains duplicates") — dedupe defensively before
    # writing rather than assume the export is clean. For polls (same message, so any
    # duplicate should be identical) keep the first. For votes, keep whichever has the
    # latest answeredAt — same "last write wins" semantics as the live poll_answer
    # handler's put_item, in case a person's vote legitimately changed between the
    # (possibly paginated) snapshots this was scanned from.
    seen_polls: dict = {}
    poll_dupes = 0
    for p in data["polls"]:
        if p["pollId"] in seen_polls:
            poll_dupes += 1
            continue
        seen_polls[p["pollId"]] = p

    seen_votes: dict = {}
    vote_dupes = 0
    for v in data["votes"]:
        key = (v["pollId"], v["telegramUserId"])
        prev = seen_votes.get(key)
        if prev is not None:
            vote_dupes += 1
            if v.get("answeredAt", "") <= prev.get("answeredAt", ""):
                continue  # prev is same-or-newer, keep it
        seen_votes[key] = v

    if poll_dupes or vote_dupes:
        print(f"Deduped {poll_dupes} duplicate poll row(s), {vote_dupes} duplicate vote row(s) from the export.")

    polls_to_write, polls_skipped = [], 0
    for p in seen_polls.values():
        existing = polls_table.get_item(Key={"pollId": p["pollId"]}).get("Item")
        if existing:
            polls_skipped += 1
            continue
        item = {k: v for k, v in p.items() if v is not None}
        polls_to_write.append(item)

    votes_to_write, votes_skipped = [], 0
    for v in seen_votes.values():
        existing = votes_table.get_item(
            Key={"pollId": v["pollId"], "telegramUserId": v["telegramUserId"]}
        ).get("Item")
        if existing:
            votes_skipped += 1
            continue
        item = {k: val for k, val in v.items() if val is not None}
        votes_to_write.append(item)

    print(f"Polls:  {len(polls_to_write)} to write, {polls_skipped} already present (skipped)")
    print(f"Votes:  {len(votes_to_write)} to write, {votes_skipped} already present (skipped)")

    if not args.yes:
        print("\nDry run — nothing written. Re-run with --yes to actually write these rows.")
        return

    with polls_table.batch_writer() as batch:
        for item in polls_to_write:
            batch.put_item(Item=item)
    with votes_table.batch_writer() as batch:
        for item in votes_to_write:
            batch.put_item(Item=item)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", default="backfill", help="Telethon session file name (default: backfill)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-chats", help="List every chat this account is in, with message counts")
    p_list.set_defaults(func=cmd_list_chats)

    p_check = sub.add_parser("check", help="Verify MTProto poll.id matches the Bot API pollId already in DynamoDB")
    p_check.add_argument("--chat", required=True, help="Chat id or @username")
    p_check.add_argument("--message-id", required=True, type=int, help="Message id of a known poll (Copy Message Link in Telegram Desktop)")
    p_check.set_defaults(func=cmd_check)

    p_scan = sub.add_parser("scan", help="Walk chat history, export polls+votes to a local JSON file")
    p_scan.add_argument("--chat", required=True)
    p_scan.add_argument("--out", required=True)
    p_scan.add_argument("--since", help="YYYY-MM-DD, inclusive")
    p_scan.add_argument("--until", help="YYYY-MM-DD, inclusive")
    p_scan.add_argument(
        "--no-vote-to-unlock",
        dest="vote_to_unlock",
        action="store_false",
        default=True,
        help="Don't auto-vote 'Подивитись відповідь' on polls that need a vote to reveal "
        "results — just skip them and report, like before (default: auto-vote)",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_load = sub.add_parser("load", help="Write a scanned JSON export into DynamoDB (idempotent)")
    p_load.add_argument("--in", dest="in", required=True)
    p_load.add_argument("--polls-table", required=True)
    p_load.add_argument("--votes-table", required=True)
    p_load.add_argument("--region", default="eu-west-2")
    p_load.add_argument("--yes", action="store_true", help="Actually write (default is dry-run)")
    p_load.set_defaults(func=cmd_load)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
