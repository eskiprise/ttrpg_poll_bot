#!/usr/bin/env python3
"""
Telegram Poll Bot - Lambda Handler
Simple implementation using Telegram Bot API directly
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import requests
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Poll options
RATING_OPTIONS = [
    "Подивитись відповідь",
    "1 / 10 🤬",
    "2 / 10 😡",
    "3 / 10 🥴",
    "4 / 10 😞",
    "5 / 10 🤔",
    "6 / 10 🙂",
    "7 / 10 😀",
    "8 / 10 ☺️",
    "9 / 10 🤩",
    "10 / 10 🌟🌟🌟"
]

# Gamification — XP, levels, achievements. See TelegramUserStats/gamification.ts in
# ttrpg_website2/shared for the display-side mirror (titles, emoji, the ACHIEVEMENTS
# catalog) — this file only ever needs the numeric thresholds, since it's the only place
# XP/achievements actually get awarded; keep LEVEL_THRESHOLDS and the two achievement
# threshold lists below in sync with that file if either changes.
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
    """XP needed for level N -> N+1; index 0 = 1->2 ... index 8 = 9->10."""
    thresholds = [LEVEL_UP_BASE_XP]
    while len(thresholds) < MAX_PLAYER_LEVEL - 1:
        thresholds.append(int((thresholds[-1] * LEVEL_UP_MULTIPLIER) // 10) * 10)
    return thresholds


LEVEL_THRESHOLDS = _compute_level_thresholds()


def apply_xp(level: int, current_xp: int, gained_xp: int) -> tuple:
    """Applies gained XP, leveling up (possibly more than once) with overflow carried
    forward. Uncapped past MAX_PLAYER_LEVEL — no further leveling, XP just accumulates."""
    xp = current_xp + gained_xp
    while level < MAX_PLAYER_LEVEL:
        threshold = LEVEL_THRESHOLDS[level - 1]
        if xp < threshold:
            break
        xp -= threshold
        level += 1
    return level, xp


class User:
    def __init__(self, user: dict) -> None:
        self.id: int = user.get('id')
        self.is_bot: bool = user.get('is_bot', False)
        self.first_name: str = user.get('first_name', '')
        self.last_name: str = user.get('last_name', '')
        self.username: str = user.get('username', '')
        self.language_code: str = user.get('language_code', '')

    def __repr__(self) -> str:
        return str(self.__dict__)


class Chat:
    def __init__(self, chat: dict) -> None:
        self.id: int = chat.get('id')
        self.first_name: str = chat.get('first_name', '')
        self.last_name: str = chat.get('last_name', '')
        self.username: str = chat.get('username', '')
        self.type: str = chat.get('type', '')

    def __repr__(self) -> str:
        return str(self.__dict__)


class Message:
    def __init__(self, message: dict) -> None:
        self.from_user = User(message.get('from', {}))
        self.chat = Chat(message.get('chat', {}))
        self.date: int = message.get('date')
        self.text: str = message.get('text', '')
        self.entities: list = message.get('entities', [])
        self.message_thread_id: int = message.get('message_thread_id')

    def get_command(self) -> str:
        """Extract command from message if present."""
        if self.entities and self.entities[0].get('type') == 'bot_command':
            command = self.text.split()[0]
            for suffix in ('@ttrpgpollbot', '@ttrpgpolltestbot'):
                if command.endswith(suffix):
                    command = command[: -len(suffix)]
                    break
            return command
        return ''

    def get_command_args(self) -> str:
        """Get text after command."""
        if self.get_command():
            parts = self.text.split(maxsplit=1)
            return parts[1] if len(parts) > 1 else ''
        return ''

    def __repr__(self) -> str:
        return str(self.__dict__)


class PollAnswer:
    def __init__(self, poll_answer: dict) -> None:
        self.poll_id: str = poll_answer.get('poll_id')
        self.user = User(poll_answer.get('user', {})) if poll_answer.get('user') else None
        self.option_ids: list = poll_answer.get('option_ids', [])

    def __repr__(self) -> str:
        return str(self.__dict__)


class ChatMemberUpdated:
    """The bot's own membership in a chat changed — added, removed, promoted, etc.
    For private chats this only fires on block/unblock (see Telegram's docs), never a
    real "join" — callers must check chat.type before treating this as a group-add."""
    def __init__(self, data: dict) -> None:
        self.chat = Chat(data.get('chat', {}))
        self.old_status: str = data.get('old_chat_member', {}).get('status', '')
        self.new_status: str = data.get('new_chat_member', {}).get('status', '')

    def __repr__(self) -> str:
        return str(self.__dict__)


class Update:
    def __init__(self, update: dict) -> None:
        self.update_id = update.get('update_id')
        self.message = Message(update.get('message', {})) if 'message' in update else None
        self.poll_answer = (
            PollAnswer(update.get('poll_answer', {})) if 'poll_answer' in update else None
        )
        self.my_chat_member = (
            ChatMemberUpdated(update['my_chat_member']) if 'my_chat_member' in update else None
        )

    def __repr__(self) -> str:
        return str(self.__dict__)


class Bot:
    """Simple Telegram Bot API client."""
    
    def __init__(self, token: str = None) -> None:
        if token is None:
            token = self._get_token_from_ssm()
        
        self.token = token
        self.session = requests.Session()
        self.api_url = f'https://api.telegram.org/bot{self.token}/'

    @staticmethod
    def _get_token_from_ssm() -> str:
        """Fetch bot token from SSM Parameter Store."""
        ssm = boto3.client('ssm', region_name=os.environ.get('AWS_REGION', 'eu-west-2'))
        parameter_name = os.environ.get('TELEGRAM_TOKEN_SSM_PARAMETER', '/telegram/poll_bot/token')

        try:
            response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
            token = response['Parameter']['Value']
            logger.info(f"Successfully retrieved token from SSM: {parameter_name}")
            return token
        except Exception as e:
            logger.error(f"Failed to get SSM parameter {parameter_name}: {e}")
            raise

    def send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int = None,
        reply_markup: dict = None,
    ) -> int:
        """Send text message to chat."""
        url = self.api_url + 'sendMessage'
        json_data = {
            'chat_id': chat_id,
            'text': text,
        }
        if message_thread_id is not None:
            json_data['message_thread_id'] = message_thread_id
        if reply_markup is not None:
            json_data['reply_markup'] = reply_markup
        logger.debug(f'Sending message: {json_data}')
        response = self.session.post(url=url, json=json_data)
        logger.info(f'Sent message to {chat_id}')
        logger.debug(f'{response.status_code=} {response.text=}')

        return response.status_code

    def send_poll(
        self,
        chat_id: int,
        question: str,
        options: list,
        is_anonymous: bool = False,
        allows_multiple_answers: bool = False,
        message_thread_id: int = None
    ) -> dict:
        """Send poll to chat. Returns the parsed API response body (has result.poll.id)."""
        url = self.api_url + 'sendPoll'
        json_data = {
            'chat_id': chat_id,
            'question': question,
            'options': options,
            'is_anonymous': is_anonymous,
            'allows_multiple_answers': allows_multiple_answers,
        }
        if message_thread_id is not None:
            json_data['message_thread_id'] = message_thread_id
        logger.debug(f'Sending poll: {json_data}')
        response = self.session.post(url=url, json=json_data)
        logger.info(f'Sent poll to {chat_id}: {question}')
        logger.debug(f'{response.status_code=} {response.text=}')

        return response.json()

    def leave_chat(self, chat_id: int) -> int:
        """Remove the bot from a chat — used to eject itself from unauthorized groups."""
        url = self.api_url + 'leaveChat'
        response = self.session.post(url=url, json={'chat_id': chat_id})
        logger.info(f'Left chat {chat_id}')
        logger.debug(f'{response.status_code=} {response.text=}')

        return response.status_code


_ssm_parameter_cache: dict = {}


def _get_ssm_parameter(parameter_name: str) -> str:
    """Fetch and cache an SSM parameter value for the lifetime of this warm container —
    unlike Bot._get_token_from_ssm (called fresh per Bot()), this backs chat IDs that get
    read on nearly every invocation (e.g. update_handler's chat-restriction check), so an
    uncached fetch would mean an SSM call per webhook request."""
    if parameter_name not in _ssm_parameter_cache:
        ssm = boto3.client('ssm', region_name=os.environ.get('AWS_REGION', 'eu-west-2'))
        response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
        _ssm_parameter_cache[parameter_name] = response['Parameter']['Value']
    return _ssm_parameter_cache[parameter_name]


_dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'eu-west-2'))


def _rating_polls_table():
    return _dynamodb.Table(os.environ['TELEGRAM_RATING_POLLS_TABLE'])


def _rating_votes_table():
    return _dynamodb.Table(os.environ['TELEGRAM_RATING_VOTES_TABLE'])


def _xp_ledger_table():
    return _dynamodb.Table(os.environ['TELEGRAM_XP_LEDGER_TABLE'])


def _player_level_table():
    return _dynamodb.Table(os.environ['TELEGRAM_PLAYER_LEVEL_TABLE'])


def _achievements_table():
    return _dynamodb.Table(os.environ['TELEGRAM_ACHIEVEMENTS_TABLE'])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_week_key(iso_timestamp: str) -> str:
    year, week, _ = datetime.fromisoformat(iso_timestamp).isocalendar()
    return f"{year}-W{week:02d}"


def _award_xp(telegram_user_id: int, source_id: str, xp: int, xp_type: str, counter_field: str = None) -> bool:
    """Idempotently award XP once per source_id. Conditional put on the append-only
    ledger is the sole source of truth for "was this already awarded" — so this
    correctly no-ops on retract+revote, a rating value change, or a duplicate webhook
    delivery, regardless of what happens to the mutable votes table. Returns True only
    when this call newly awarded XP (used to gate the weekly-bonus check on a real vote,
    not a harmless re-delivery)."""
    try:
        _xp_ledger_table().put_item(
            Item={
                'telegramUserId': telegram_user_id,
                'sourceId': source_id,
                'xp': xp,
                'type': xp_type,
                'awardedAt': _now_iso(),
            },
            ConditionExpression='attribute_not_exists(sourceId)',
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise
    _apply_xp_to_level(telegram_user_id, xp, counter_field)
    return True


def _apply_xp_to_level(telegram_user_id: int, gained_xp: int, counter_field: str = None) -> None:
    """Read-modify-write telegram_player_level with an optimistic lock (version) — needed
    because vote XP (webhook) and feedback XP (notify_new_feedback) are separate Lambda
    invocations that could race on the same user's row. Up to 3 attempts; on exhaustion,
    logs and gives up — the ledger entry itself is never lost, so any rare drift here is
    fixable later by re-running scripts/backfill_gamification.py in reconciliation mode."""
    table = _player_level_table()
    new_level = old_level = None
    games_played = feedback_given = 0

    for attempt in range(3):
        item = table.get_item(Key={'telegramUserId': telegram_user_id}).get('Item') or {}
        old_level = int(item.get('level', 1))
        current_xp = int(item.get('currentXp', 0))
        total_xp = int(item.get('totalXp', 0)) + gained_xp
        games_played = int(item.get('gamesPlayed', 0)) + (1 if counter_field == 'gamesPlayed' else 0)
        feedback_given = int(item.get('feedbackGiven', 0)) + (1 if counter_field == 'feedbackGiven' else 0)
        version = int(item.get('version', 0))

        new_level, new_current_xp = apply_xp(old_level, current_xp, gained_xp)

        try:
            table.put_item(
                Item={
                    'telegramUserId': telegram_user_id,
                    'level': new_level,
                    'currentXp': new_current_xp,
                    'totalXp': total_xp,
                    'gamesPlayed': games_played,
                    'feedbackGiven': feedback_given,
                    'updatedAt': _now_iso(),
                    'version': version + 1,
                },
                ConditionExpression='attribute_not_exists(version) OR version = :expected',
                ExpressionAttributeValues={':expected': version},
            )
            break
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
            if attempt == 2:
                logger.error(
                    f"Gave up updating telegram_player_level for user {telegram_user_id} "
                    "after 3 attempts (concurrent update)"
                )
                return

    if counter_field == 'gamesPlayed':
        _check_and_award_tier_achievements(telegram_user_id, 'games_played', games_played, GAMES_PLAYED_ACHIEVEMENT_THRESHOLDS)
    elif counter_field == 'feedbackGiven':
        _check_and_award_tier_achievements(telegram_user_id, 'feedback_given', feedback_given, FEEDBACK_GIVEN_ACHIEVEMENT_THRESHOLDS)

    if new_level >= MAX_PLAYER_LEVEL > old_level:
        _award_achievement(telegram_user_id, 'max_level')


def _check_and_award_tier_achievements(telegram_user_id: int, id_prefix: str, value: int, thresholds: list) -> None:
    """Idempotent via the achievements table's own conditional put — safe to re-check
    already-crossed thresholds on every subsequent vote/feedback."""
    for threshold in thresholds:
        if value >= threshold:
            _award_achievement(telegram_user_id, f'{id_prefix}_{threshold}')


def _award_achievement(telegram_user_id: int, achievement_id: str) -> None:
    try:
        _achievements_table().put_item(
            Item={
                'telegramUserId': telegram_user_id,
                'achievementId': achievement_id,
                'unlockedAt': _now_iso(),
            },
            ConditionExpression='attribute_not_exists(achievementId)',
        )
        logger.info(f"Awarded achievement {achievement_id} to user {telegram_user_id}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def _maybe_award_weekly_bonus(telegram_user_id: int) -> None:
    """Awarded once per ISO calendar week (Mon-Sun UTC) on a user's 2nd vote-XP-earning
    poll that week — counts ledger 'vote' entries by their real awardedAt, not "now", so
    this stays correct however late a batch of webhook updates gets processed."""
    week_key = _iso_week_key(_now_iso())
    result = _xp_ledger_table().query(KeyConditionExpression=Key('telegramUserId').eq(telegram_user_id))
    votes_this_week = sum(
        1 for item in result.get('Items', [])
        if item.get('type') == 'vote' and _iso_week_key(item.get('awardedAt', '')) == week_key
    )
    if votes_this_week >= WEEKLY_BONUS_VOTES_REQUIRED:
        _award_xp(telegram_user_id, f"weekly_bonus#{week_key}", XP_WEEKLY_BONUS, 'weekly_bonus')


_deserializer = TypeDeserializer()


def _deserialize_dynamodb_item(item: dict) -> dict:
    """Convert a raw DynamoDB Streams AttributeValue map into plain Python values."""
    return {key: _deserializer.deserialize(value) for key, value in item.items()}


def notify_new_signup(event, context):
    """
    Lambda handler triggered directly by the ttrpg_club_signup_requests DynamoDB Stream
    (see ttrpg_website's aws_infra Terraform). Posts a message to ADMIN_CHAT_ID for every
    newly-inserted pending signup request, reusing this bot's existing token/session.
    """
    bot = Bot()
    admin_chat_id = _get_ssm_parameter(os.environ['ADMIN_CHAT_ID_SSM_PARAMETER'])
    records = event.get('Records', [])
    logger.info(f"notify_new_signup received {len(records)} stream record(s)")

    for record in records:
        if record.get('eventName') != 'INSERT':
            continue

        new_image = record.get('dynamodb', {}).get('NewImage')
        if not new_image:
            continue

        request = _deserialize_dynamodb_item(new_image)
        if request.get('status') != 'PENDING':
            continue

        # Only firstName is always present (plus at least one of contact/phone) — list
        # just the fields that were actually given.
        name = " ".join(filter(None, [request.get('firstName'), request.get('lastName')]))
        lines = ["🆕 New club signup request", f"Name: {name}"]
        for label, key in (("Contact", 'telegramOrViberContact'), ("Phone", 'phone'), ("Email", 'email')):
            if request.get(key):
                lines.append(f"{label}: {request[key]}")
        text = "\n".join(lines) + "\n\nReview it in the admin panel to approve or reject."
        bot.send_message(chat_id=admin_chat_id, text=text)
        logger.info(f"Notified admin about signup request {request.get('requestId')}")

    return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}


def _feedback_message_text(feedback: dict) -> str:
    """Format one ttrpg_club_telegram_feedback item into the DM sent to the GM."""
    if feedback.get('revealIdentity'):
        first = feedback.get('submitterFirstName', '') or ''
        last = feedback.get('submitterLastName', '') or ''
        username = feedback.get('submitterUsername', '')
        who = f"{first} {last}".strip() or 'Учасник'
        if username:
            who += f" (@{username})"
    else:
        who = 'Анонімно'

    lines = [
        "📝 Новий фідбек про сесію",
        f"Від: {who}",
        "",
        f"Пригода/історія: {feedback.get('adventureRating', '—')}/10",
        f"Стіл: {feedback.get('tableRating', '—')}/10",
        f"Майстер: {feedback.get('gmRating', '—')}/10",
        f"Себе: {feedback.get('selfRating', '—')}/10",
    ]

    text = feedback.get('feedbackText', '')
    if text:
        lines += ["", text]

    return "\n".join(lines)


def notify_new_feedback(event, context):
    """
    Lambda handler triggered by the ttrpg_club_telegram_feedback DynamoDB Stream — DMs
    the session's GM with each new feedback submission. The submitter's identity is
    only included if they checked "reveal" in the Mini App form; otherwise the message
    says "Анонімно". A Telegram bot can only DM a user who has previously started a
    private chat with it — if the GM never has, sendMessage comes back with a non-2xx
    status rather than raising, so that's logged as a warning and the batch continues
    rather than failing outright.
    """
    bot = Bot()
    records = event.get('Records', [])
    logger.info(f"notify_new_feedback received {len(records)} stream record(s)")

    for record in records:
        if record.get('eventName') != 'INSERT':
            continue

        new_image = record.get('dynamodb', {}).get('NewImage')
        if not new_image:
            continue

        feedback = _deserialize_dynamodb_item(new_image)
        poll_id = feedback.get('pollId')
        poll = _rating_polls_table().get_item(Key={'pollId': poll_id}).get('Item') if poll_id else None
        if not poll:
            logger.warning(f"Feedback for unknown poll {poll_id} — cannot find GM to notify")
            continue

        gm_user_id = poll.get('creatorUserId')
        if not gm_user_id:
            logger.warning(f"Poll {poll_id} has no recorded creatorUserId — cannot DM the GM")
            continue

        # Isolated from the DM-send below: an XP failure must not affect the GM
        # notification, and vice versa — a raised exception here would otherwise abort
        # the DM too and cause the whole batch to be retried by the stream's
        # event-source-mapping (redelivering DMs that already succeeded).
        if feedback.get('telegramUserId') != gm_user_id:
            try:
                _award_xp(
                    int(feedback['telegramUserId']),
                    f"feedback_bonus#{poll_id}",
                    XP_FEEDBACK_BONUS,
                    'feedback_bonus',
                    counter_field='feedbackGiven',
                )
            except Exception as e:
                logger.error(f"Failed to award feedback XP for poll {poll_id}: {e}")

        text = f"{poll.get('questionText', '')}\n\n{_feedback_message_text(feedback)}"
        try:
            status = bot.send_message(chat_id=int(gm_user_id), text=text)
            if status >= 300:
                logger.warning(
                    f"Could not DM GM {gm_user_id} for poll {poll_id} (status {status}) — "
                    "they may never have started a private chat with the bot"
                )
            else:
                logger.info(f"DMed GM {gm_user_id} with feedback for poll {poll_id}")
        except Exception as e:
            logger.error(f"Failed to DM GM {gm_user_id} for poll {poll_id}: {e}")

    return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}


def handle_start_command(bot: Bot, update: Update):
    """Handle /start command."""
    text = (
        'Привіт! Цей бот створює опитування та збирає статистику для TTRPG клубу '
        'Dice & Adventures.\n\n'
        'Цей бот не призначений для використання будь-ким іншим.\n\n'
        'Але якщо вам цікаво дізнатись більше про наш клуб — завітайте на наш сайт. '
        'Приєднатися можна за контактами, вказаними там.\n\n'
        'Гарного дня!'
    )
    website_url = os.environ.get('CLUB_WEBSITE_URL')
    reply_markup = None
    if website_url:
        reply_markup = {
            'inline_keyboard': [[
                {'text': '🌐 Наш сайт', 'url': website_url}
            ]]
        }
    else:
        logger.warning("CLUB_WEBSITE_URL is not configured — /start sent without a website button")

    bot.send_message(
        update.message.chat.id,
        text,
        message_thread_id=update.message.message_thread_id,
        reply_markup=reply_markup,
    )


def handle_stats_command(bot: Bot, update: Update):
    """
    Handle /stats — opens the club's Telegram Mini App with the user's own rating stats.

    Uses a plain `url` button with a t.me/<bot>/<app> deep link rather than an inline
    `web_app` button: Telegram only allows `web_app` buttons in private chats with the
    bot, and this bot operates in a group chat (with topics), so `web_app` buttons here
    would silently fail. The t.me deep link works everywhere and requires the Mini App
    to be registered once via @BotFather's /newapp (see README_LAMBDA.md).
    """
    deep_link = os.environ.get('MINI_APP_DEEP_LINK')
    if not deep_link:
        logger.warning("MINI_APP_DEEP_LINK is not configured — cannot open the mini app")
        bot.send_message(
            update.message.chat.id,
            'Статистика тимчасово недоступна.',
            message_thread_id=update.message.message_thread_id
        )
        return

    bot.send_message(
        update.message.chat.id,
        'Натисніть, щоб побачити свою статистику оцінок:',
        message_thread_id=update.message.message_thread_id,
        reply_markup={
            'inline_keyboard': [[
                {'text': '📊 Моя статистика', 'url': deep_link}
            ]]
        }
    )


def handle_poll_command(bot: Bot, update: Update):
    """Handle /rate command."""
    args = update.message.get_command_args()

    if not args:
        bot.send_message(
            update.message.chat.id,
            'Будь ласка, додайте текст після команди. Наприклад: /rate oblivion',
            message_thread_id=update.message.message_thread_id
        )
        return

    # Create poll question
    poll_question = f"Оцінка ({args})"

    # Send poll
    response = bot.send_poll(
        chat_id=update.message.chat.id,
        question=poll_question,
        options=RATING_OPTIONS,
        is_anonymous=False,
        allows_multiple_answers=False,
        message_thread_id=update.message.message_thread_id
    )

    # Remember what this poll_id was rating, so a later poll_answer webhook update
    # (which only carries poll_id + option_ids, not the question) can make sense of it.
    # The creator is remembered too: they're the session's GM for stats ("My Games
    # Conducted") and the one who gets DMed detailed feedback.
    poll = (response or {}).get('result', {}).get('poll', {})
    poll_id = poll.get('id')
    if poll_id:
        creator = update.message.from_user
        item = {
            'pollId': poll_id,
            'questionText': poll_question,
            'chatId': update.message.chat.id,
            'createdAt': _now_iso(),
            'creatorUserId': creator.id,
            'creatorFirstName': creator.first_name,
            'creatorLastName': creator.last_name,
            'creatorUsername': creator.username,
        }
        if update.message.message_thread_id is not None:
            item['messageThreadId'] = update.message.message_thread_id
        _rating_polls_table().put_item(Item=item)

        _send_feedback_link(bot, update, poll_id)
    else:
        logger.warning(f"sendPoll response had no poll id: {response}")

    logger.info(f"Poll created by {update.message.from_user.username}: {poll_question}")


def _send_feedback_link(bot: Bot, update: Update, poll_id: str):
    """
    Alongside the quick 1-10 poll, send a button opening the Mini App straight to this
    session's detailed feedback form (four 1-10 questions + optional free text). This is
    a separate, private channel from the poll's own rating/player-list bookkeeping — see
    README_LAMBDA.md. Uses `startapp=feedback_<pollId>` so the Mini App knows which
    session to render the form for. Degrades silently if the Mini App isn't registered
    yet, same as /stats.
    """
    base_deep_link = os.environ.get('MINI_APP_DEEP_LINK')
    if not base_deep_link:
        logger.warning("MINI_APP_DEEP_LINK is not configured — skipping feedback link")
        return

    feedback_link = f"{base_deep_link}?startapp=feedback_{poll_id}"
    bot.send_message(
        update.message.chat.id,
        'Залиште розгорнутий фідбек про цю сесію:',
        message_thread_id=update.message.message_thread_id,
        reply_markup={
            'inline_keyboard': [[
                {'text': '📝 Залишити фідбек', 'url': feedback_link}
            ]]
        }
    )


def handle_poll_answer(update: Update):
    """
    Record a vote on a /rate poll (option_ids[0] == 0 is "Подивитись відповідь" —
    not a real rating, so it's not stored). A retracted vote (Telegram sends an empty
    option_ids when a user deselects their answer on a single-choice poll) or a switch
    to "view results" clears any previously stored rating for that user — otherwise
    someone who voted an 8, then realized they hadn't actually played and retracted,
    would stay counted as a player forever. Silently ignored for polls we didn't create
    via /rate since they're never in the polls table.
    """
    answer = update.poll_answer
    if not answer.user:
        return

    poll = _rating_polls_table().get_item(Key={'pollId': answer.poll_id}).get('Item')
    if not poll:
        logger.info(f"poll_answer for untracked poll {answer.poll_id} — ignoring")
        return

    def clear_vote(reason: str):
        _rating_votes_table().delete_item(
            Key={'pollId': answer.poll_id, 'telegramUserId': answer.user.id}
        )
        logger.info(f"Cleared any stored rating for user {answer.user.id} on poll {answer.poll_id} ({reason})")

    if not answer.option_ids:
        clear_vote("retracted vote")
        return

    rating = answer.option_ids[0]
    if rating == 0:
        clear_vote("switched to 'view results'")
        return
    if rating < 1 or rating > 10:
        logger.warning(f"Unexpected option_id {rating} for poll {answer.poll_id}")
        return

    _rating_votes_table().put_item(Item={
        'pollId': answer.poll_id,
        'telegramUserId': answer.user.id,
        'username': answer.user.username,
        'firstName': answer.user.first_name,
        'lastName': answer.user.last_name,
        'rating': rating,
        'questionText': poll.get('questionText', ''),
        'answeredAt': _now_iso(),
    })
    logger.info(f"Recorded rating {rating} from user {answer.user.id} for poll {answer.poll_id}")

    if answer.user.id != poll.get('creatorUserId'):
        if _award_xp(answer.user.id, f"vote#{answer.poll_id}", XP_VOTE, 'vote', counter_field='gamesPlayed'):
            _maybe_award_weekly_bonus(answer.user.id)


GROUP_CHAT_TYPES = ('group', 'supergroup')


def _allowed_chat_id() -> int:
    return int(_get_ssm_parameter(os.environ['ALLOWED_CHAT_ID_SSM_PARAMETER']))


def _reject_unauthorized_chat(bot: Bot, chat_id: int, message_thread_id: int = None) -> None:
    """Tell whoever added the bot to a group it doesn't belong in, then leave. Never
    call this for a private chat — leaveChat isn't a meaningful operation on a DM, and
    my_chat_member fires there too (on block/unblock), which this must not react to."""
    bot_username = os.environ.get('BOT_USERNAME', '')
    text = (
        "Привіт! Цей бот працює лише в чаті нашого TTRPG клубу.\n"
        f"Щоб дізнатись більше, напишіть мені особисто: @{bot_username}."
    )
    bot.send_message(chat_id, text, message_thread_id=message_thread_id)
    bot.leave_chat(chat_id)
    logger.warning(f"Left unauthorized chat {chat_id}")


def _handle_my_chat_member(update: Update) -> None:
    """React to the bot's own membership changing in a chat. Only acts on "freshly
    added to a group/supergroup" — ignores private-chat block/unblock events (which
    also arrive as my_chat_member) and ignores removals/promotions in an already-known
    chat, since those aren't "somebody just added me somewhere new"."""
    member_update = update.my_chat_member
    chat = member_update.chat
    if chat.type not in GROUP_CHAT_TYPES:
        return

    was_member = member_update.old_status in ('member', 'administrator', 'creator')
    is_member_now = member_update.new_status in ('member', 'administrator')
    if was_member or not is_member_now:
        return

    if chat.id == _allowed_chat_id():
        logger.info(f"Bot added to the allowed chat {chat.id}")
        return

    _reject_unauthorized_chat(Bot(), chat.id)


def update_handler(update: Update):
    """Process incoming update."""
    if update.poll_answer:
        handle_poll_answer(update)
        return

    if update.my_chat_member:
        _handle_my_chat_member(update)
        return

    if not update.message:
        logger.warning("Update without message, poll_answer, or my_chat_member received")
        return

    chat = update.message.chat
    if chat.type in GROUP_CHAT_TYPES and chat.id != _allowed_chat_id():
        # Belt-and-suspenders for a chat the bot was already in before this restriction
        # existed, or where leaveChat above didn't take effect for some reason.
        _reject_unauthorized_chat(Bot(), chat.id, update.message.message_thread_id)
        return

    bot = Bot()
    command = update.message.get_command()

    logger.info(f"Received command: {command} from user {update.message.from_user.username}")

    if command == '/start':
        handle_start_command(bot, update)
    elif command == '/rate':
        # /start and /stats are fine from a DM (that's the whole point of the chat
        # restriction above pointing people there) — but a poll created outside the
        # club's own chat would pollute every stat/leaderboard/XP calculation that reads
        # telegram_rating_polls, since those all trust chatId without re-checking it.
        # This is a narrower, unconditional check (unlike the group-only gate above) —
        # it also blocks poll creation in a DM, which that gate deliberately allows.
        if chat.id != _allowed_chat_id():
            bot.send_message(
                chat.id,
                'Цю команду можна використовувати лише в чаті клубу.',
                message_thread_id=update.message.message_thread_id
            )
        else:
            handle_poll_command(bot, update)
    elif command == '/stats':
        handle_stats_command(bot, update)
    else:
        # Unknown command or regular message - ignore
        logger.info(f"Ignoring message: {update.message.text}")


def lambda_handler(event, context):
    """AWS Lambda handler function."""
    logger.info(f"Received event: {json.dumps(event)}")
    
    # Handle webhook verification (optional)
    if event.get('httpMethod') == 'GET':
        return {
            'statusCode': 200,
            'body': json.dumps({'status': 'Bot is running'})
        }
    
    try:
        update = Update(json.loads(event['body']))
        update_handler(update)
    except Exception as e:
        logger.error(f"Error processing update: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
    
    return {
        'statusCode': 200,
        'body': json.dumps({'status': 'ok'})
    }


# For setting webhook (run locally)
if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python lambda_handler.py <webhook_url>")
        sys.exit(1)
    
    webhook_url = sys.argv[1]
    bot = Bot()
    
    # Set webhook
    url = bot.api_url + 'setWebhook'
    response = bot.session.post(url=url, json={'url': webhook_url})
    print(f"Webhook set: {response.status_code} - {response.text}")
    
    # Get webhook info
    url = bot.api_url + 'getWebhookInfo'
    response = bot.session.get(url=url)
    print(f"Webhook info: {response.text}")
