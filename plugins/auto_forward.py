# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import asyncio
import logging
import re
import time
from datetime import datetime

from pyrogram import filters

from config import (
    AUTO_FORWARD_DELAY,
    AUTO_FORWARD_DESTINATION,
    AUTO_FORWARD_ENABLED,
    AUTO_FORWARD_MODE,
    AUTO_FORWARD_PAIRS,
    AUTO_FORWARD_SCHEDULE,
    AUTO_FORWARD_SOURCE,
    AUTO_FORWARD_TYPES,
    OWNER_ID,
)
from shared_client import app
from utils.func import auto_forward_rules_collection

logger = logging.getLogger(__name__)

VALID_MODES = {"copy", "forward"}
MEDIA_TYPES = {"animation", "audio", "document", "photo", "sticker", "video", "video_note", "voice"}
TYPE_ALIASES = {
    "all": {"all"},
    "media": MEDIA_TYPES,
    "text": {"text"},
}
CACHE_SECONDS = 10
_RULE_CACHE = {"expires_at": 0.0, "rules": []}


HELP_TEXT = """**Auto Forward Bot**

Owner commands:
`/autoforward add <source> <destination> [copy|forward] [schedule=HH:MM-HH:MM] [types=all|media|text|photo,video]`
`/autoforward connect <source> <destination>`
`/autoforward follow <source>`
`/autoforward schedule <source> <destination> <always|off|HH:MM-HH:MM>`
`/autoforward filter <source> <destination> <all|media|text|photo,video>`
`/autoforward watch [source]`
`/autoforward stats [source]`
`/autoforward del <source> [destination]`
`/autoforward on <source> [destination]`
`/autoforward off <source> [destination]`
`/autoforward list`

Examples:
`/autoforward add -1001111111111 -1002222222222`
`/autoforward add @sourcechannel @targetchannel forward`
`/autoforward add -1001111111111 -1002222222222/45 schedule=09:00-18:00 types=media`

Schedules use UTC time. Use `copy` to repost without a forwarded header, or `forward` to keep the forwarded header."""


def _is_owner(user_id):
    return user_id in OWNER_ID


def _normalize_chat_ref(value):
    value = str(value or "").strip()
    if not value:
        return ""

    value = value.split("?", 1)[0].strip()
    private_match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)", value, re.I)
    if private_match:
        return f"-100{private_match.group(1)}"

    public_match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/([^/\s]+)", value, re.I)
    if public_match:
        value = public_match.group(1)

    if value.startswith("@"):
        value = value[1:]

    return value.lower()


def _normalize_mode(value):
    mode = str(value or AUTO_FORWARD_MODE or "copy").strip().lower()
    return mode if mode in VALID_MODES else "copy"


def _normalize_schedule(value):
    schedule = str(value or "always").strip().lower()
    if schedule in {"", "always", "on"}:
        return "always"
    if schedule in {"off", "none", "paused"}:
        return "off"
    if re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", schedule):
        start, end = schedule.split("-", 1)
        if _time_to_minutes(start) is not None and _time_to_minutes(end) is not None:
            return schedule
    return None


def _time_to_minutes(value):
    try:
        hours, minutes = [int(part) for part in value.split(":", 1)]
    except (TypeError, ValueError):
        return None

    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return hours * 60 + minutes
    return None


def _is_schedule_active(schedule, now=None):
    normalized = _normalize_schedule(schedule)
    if normalized in {None, "off"}:
        return False
    if normalized == "always":
        return True

    now = now or datetime.utcnow()
    start, end = normalized.split("-", 1)
    start_minutes = _time_to_minutes(start)
    end_minutes = _time_to_minutes(end)
    current_minutes = now.hour * 60 + now.minute

    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def _normalize_types(value):
    if not value:
        return ["all"]

    if isinstance(value, str):
        raw_types = re.split(r"[, ]+", value.strip().lower())
    else:
        raw_types = [str(item).strip().lower() for item in value]

    normalized = set()
    for item in raw_types:
        if not item:
            continue
        normalized.update(TYPE_ALIASES.get(item, {item}))

    allowed = {"all", "text"} | MEDIA_TYPES
    normalized = {item for item in normalized if item in allowed}
    if not normalized or "all" in normalized:
        return ["all"]
    return sorted(normalized)


def _message_type(message):
    if getattr(message, "text", None):
        return "text"
    for media_type in MEDIA_TYPES:
        if getattr(message, media_type, None):
            return media_type
    return "unknown"


def _type_is_allowed(message, allowed_types):
    normalized = set(_normalize_types(allowed_types))
    if "all" in normalized:
        return True
    return _message_type(message) in normalized


def _parse_add_options(options):
    mode = None
    schedule = "always"
    allowed_types = ["all"]

    remaining = list(options)
    if remaining and remaining[0].lower() in VALID_MODES:
        mode = _normalize_mode(remaining.pop(0))

    for option in remaining:
        if "=" not in option:
            continue
        key, value = option.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key in {"schedule", "time", "window"}:
            parsed_schedule = _normalize_schedule(value)
            if parsed_schedule is not None:
                schedule = parsed_schedule
        elif key in {"types", "type", "filter"}:
            allowed_types = _normalize_types(value)

    return mode or _normalize_mode(AUTO_FORWARD_MODE), schedule, allowed_types


def _parse_chat_for_api(value):
    normalized = _normalize_chat_ref(value)
    if not normalized:
        return None
    if re.fullmatch(r"-?\d+", normalized):
        return int(normalized)
    return f"@{normalized}"


def _parse_destination(destination):
    raw_destination = str(destination or "").strip()
    reply_to_message_id = None

    if raw_destination and not raw_destination.startswith(("http://", "https://")) and "/" in raw_destination:
        chat_ref, maybe_topic = raw_destination.rsplit("/", 1)
        if maybe_topic.isdigit():
            raw_destination = chat_ref
            reply_to_message_id = int(maybe_topic)

    normalized = _normalize_chat_ref(raw_destination)
    if not normalized:
        return None, None

    if re.fullmatch(r"-?\d+", normalized):
        chat_id = int(normalized)
    else:
        chat_id = f"@{normalized}"

    return chat_id, reply_to_message_id


def _source_keys(message):
    keys = set()
    if getattr(message, "chat", None):
        keys.add(str(message.chat.id))
        username = getattr(message.chat, "username", None)
        if username:
            keys.add(username.lower())
    return keys


def _parse_env_pair(raw_pair):
    pair = raw_pair.strip()
    if not pair:
        return None

    separator = "->" if "->" in pair else "=" if "=" in pair else None
    if not separator:
        logger.warning("Invalid AUTO_FORWARD_PAIRS entry: %s", raw_pair)
        return None

    source, destination = [part.strip() for part in pair.split(separator, 1)]
    return source, destination


def _env_rules():
    rules = []
    mode = _normalize_mode(AUTO_FORWARD_MODE)
    schedule = _normalize_schedule(AUTO_FORWARD_SCHEDULE) or "always"
    allowed_types = _normalize_types(AUTO_FORWARD_TYPES)

    if AUTO_FORWARD_ENABLED and AUTO_FORWARD_SOURCE and AUTO_FORWARD_DESTINATION:
        rules.append(
            {
                "source": _normalize_chat_ref(AUTO_FORWARD_SOURCE),
                "destination": AUTO_FORWARD_DESTINATION.strip(),
                "mode": mode,
                "enabled": True,
                "schedule": schedule,
                "allowed_types": allowed_types,
                "origin": "env",
            }
        )

    if AUTO_FORWARD_ENABLED and AUTO_FORWARD_PAIRS:
        for raw_pair in AUTO_FORWARD_PAIRS.split(";"):
            parsed = _parse_env_pair(raw_pair)
            if not parsed:
                continue
            source, destination = parsed
            rules.append(
                {
                    "source": _normalize_chat_ref(source),
                    "destination": destination,
                    "mode": mode,
                    "enabled": True,
                    "schedule": schedule,
                    "allowed_types": allowed_types,
                    "origin": "env",
                }
            )

    return [rule for rule in rules if rule["source"] and rule["destination"]]


async def _db_rules():
    rules = []
    try:
        async for rule in auto_forward_rules_collection.find({}):
            rules.append(
                {
                    "_id": rule.get("_id"),
                    "source": _normalize_chat_ref(rule.get("source")),
                    "destination": str(rule.get("destination", "")).strip(),
                    "mode": _normalize_mode(rule.get("mode")),
                    "enabled": bool(rule.get("enabled", True)),
                    "schedule": _normalize_schedule(rule.get("schedule")) or "always",
                    "allowed_types": _normalize_types(rule.get("allowed_types")),
                    "sent_count": int(rule.get("sent_count", 0)),
                    "failed_count": int(rule.get("failed_count", 0)),
                    "last_seen_at": rule.get("last_seen_at"),
                    "last_forwarded_at": rule.get("last_forwarded_at"),
                    "last_message_id": rule.get("last_message_id"),
                    "last_error": rule.get("last_error"),
                    "origin": "db",
                }
            )
    except Exception as exc:
        logger.error("Failed to load auto-forward rules: %s", exc)
    return [rule for rule in rules if rule["source"] and rule["destination"]]


async def _get_rules(force_refresh=False):
    now = time.monotonic()
    if not force_refresh and now < _RULE_CACHE["expires_at"]:
        return _RULE_CACHE["rules"]

    rules = _env_rules()
    rules.extend(await _db_rules())
    _RULE_CACHE["rules"] = rules
    _RULE_CACHE["expires_at"] = now + CACHE_SECONDS
    return rules


def _clear_rule_cache():
    _RULE_CACHE["expires_at"] = 0.0


def _rule_query(rule):
    if rule.get("_id") is not None:
        return {"_id": rule["_id"]}
    return {"source": rule["source"], "destination": rule["destination"]}


async def _mark_rule_seen(rule, message):
    if rule.get("origin") != "db":
        return

    try:
        await auto_forward_rules_collection.update_one(
            _rule_query(rule),
            {
                "$set": {
                    "last_seen_at": datetime.utcnow(),
                    "last_message_id": message.id,
                }
            },
        )
    except Exception as exc:
        logger.warning("Could not update auto-forward watch stats: %s", exc)


async def _mark_rule_delivery(rule, success, error=None):
    if rule.get("origin") != "db":
        return

    update = {
        "$inc": {"sent_count" if success else "failed_count": 1},
        "$set": {"updated_at": datetime.utcnow()},
    }
    if success:
        update["$set"]["last_forwarded_at"] = datetime.utcnow()
        update["$unset"] = {"last_error": ""}
    elif error:
        update["$set"]["last_error"] = str(error)[:500]

    try:
        await auto_forward_rules_collection.update_one(_rule_query(rule), update)
    except Exception as exc:
        logger.warning("Could not update auto-forward delivery stats: %s", exc)


def _format_dt(value):
    if not value:
        return "never"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value)


async def _send_by_file_id(client, message, chat_id, reply_to_message_id=None):
    caption = getattr(message, "caption", None)
    text = getattr(message, "text", None)

    if getattr(message, "video", None):
        video = message.video
        await client.send_video(
            chat_id,
            video.file_id,
            caption=caption,
            duration=getattr(video, "duration", None),
            width=getattr(video, "width", None),
            height=getattr(video, "height", None),
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "animation", None):
        animation = message.animation
        await client.send_animation(
            chat_id,
            animation.file_id,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "video_note", None):
        await client.send_video_note(
            chat_id,
            message.video_note.file_id,
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "voice", None):
        await client.send_voice(
            chat_id,
            message.voice.file_id,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "sticker", None):
        await client.send_sticker(
            chat_id,
            message.sticker.file_id,
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "audio", None):
        audio = message.audio
        await client.send_audio(
            chat_id,
            audio.file_id,
            caption=caption,
            duration=getattr(audio, "duration", None),
            performer=getattr(audio, "performer", None),
            title=getattr(audio, "title", None),
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "photo", None):
        await client.send_photo(
            chat_id,
            message.photo.file_id,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
    elif getattr(message, "document", None):
        document = message.document
        await client.send_document(
            chat_id,
            document.file_id,
            caption=caption,
            file_name=getattr(document, "file_name", None),
            reply_to_message_id=reply_to_message_id,
        )
    elif text:
        await client.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
    else:
        return False

    return True


async def _deliver_message(client, message, rule):
    destination, reply_to_message_id = _parse_destination(rule["destination"])
    if destination is None:
        logger.warning("Skipping invalid auto-forward destination: %s", rule["destination"])
        return False, "invalid destination"

    first_error = None
    try:
        if rule["mode"] == "forward" and reply_to_message_id is None:
            await client.forward_messages(destination, message.chat.id, message.id)
        else:
            await client.copy_message(
                destination,
                message.chat.id,
                message.id,
                reply_to_message_id=reply_to_message_id,
            )
        return True, None
    except Exception as exc:
        first_error = exc
        logger.warning(
            "Auto-forward %s failed from %s to %s: %s",
            rule["mode"],
            message.chat.id,
            rule["destination"],
            exc,
        )

    try:
        delivered = await _send_by_file_id(client, message, destination, reply_to_message_id)
        return delivered, None if delivered else "unsupported message type"
    except Exception as exc:
        logger.error(
            "Auto-forward fallback failed from %s to %s: %s",
            message.chat.id,
            rule["destination"],
            exc,
        )
        return False, exc or first_error


@app.on_message(filters.command("autoforward"))
async def auto_forward_command(client, message):
    user_id = message.from_user.id if message.from_user else None
    if not _is_owner(user_id):
        await message.reply_text("You are not authorized to use this command.")
        return

    args = message.text.split()[1:] if message.text else []
    if not args:
        await message.reply_text(HELP_TEXT)
        return

    action = args[0].lower()

    if action == "list":
        rules = await _get_rules(force_refresh=True)
        if not rules:
            await message.reply_text("No auto-forward rules configured.")
            return

        lines = ["**Auto-forward rules:**"]
        for index, rule in enumerate(rules, start=1):
            status = "on" if rule.get("enabled", True) else "off"
            schedule = rule.get("schedule", "always")
            schedule_state = "active" if _is_schedule_active(schedule) else "inactive"
            types = ",".join(rule.get("allowed_types", ["all"]))
            lines.append(
                f"{index}. `{rule['source']}` -> `{rule['destination']}` "
                f"({rule['mode']}, {status}, {rule['origin']}, schedule={schedule}/{schedule_state}, types={types})"
            )
        await message.reply_text("\n".join(lines))
        return

    if action in {"watch", "stats", "live"}:
        rules = await _get_rules(force_refresh=True)
        source_filter = _normalize_chat_ref(args[1]) if len(args) > 1 else None
        if source_filter:
            rules = [rule for rule in rules if rule["source"] == source_filter]
        if not rules:
            await message.reply_text("No matching auto-forward rules found.")
            return

        lines = ["**Auto-forward live watch:**"]
        for index, rule in enumerate(rules, start=1):
            schedule = rule.get("schedule", "always")
            schedule_state = "active" if _is_schedule_active(schedule) else "inactive"
            lines.extend(
                [
                    f"{index}. `{rule['source']}` -> `{rule['destination']}`",
                    f"   mode: `{rule['mode']}` | enabled: `{rule.get('enabled', True)}` | schedule: `{schedule}` ({schedule_state})",
                    f"   sent: `{rule.get('sent_count', 0)}` | failed: `{rule.get('failed_count', 0)}` | last msg: `{rule.get('last_message_id') or 'none'}`",
                    f"   last seen: `{_format_dt(rule.get('last_seen_at'))}`",
                    f"   last forwarded: `{_format_dt(rule.get('last_forwarded_at'))}`",
                ]
            )
            if rule.get("last_error"):
                lines.append(f"   last error: `{rule['last_error']}`")
        await message.reply_text("\n".join(lines))
        return

    if action == "follow":
        if len(args) < 2:
            await message.reply_text("Usage: `/autoforward follow <source>`")
            return

        chat_ref = _parse_chat_for_api(args[1])
        if chat_ref is None:
            await message.reply_text("Invalid source chat.")
            return

        try:
            chat = await client.join_chat(chat_ref)
            await message.reply_text(f"Followed `{getattr(chat, 'title', chat_ref)}` successfully.")
        except Exception as exc:
            await message.reply_text(
                "Could not follow that chat automatically. "
                f"Add the bot manually or make it admin, then try again. Error: `{str(exc)[:120]}`"
            )
        return

    if action in {"connect", "test"}:
        if len(args) < 3:
            await message.reply_text("Usage: `/autoforward connect <source> <destination>`")
            return

        source_ref = _parse_chat_for_api(args[1])
        destination_ref, _ = _parse_destination(args[2])
        checks = []
        for label, chat_ref in (("source", source_ref), ("destination", destination_ref)):
            if chat_ref is None:
                checks.append(f"{label}: invalid")
                continue
            try:
                chat = await client.get_chat(chat_ref)
                checks.append(f"{label}: ok (`{getattr(chat, 'title', chat_ref)}`)")
            except Exception as exc:
                checks.append(f"{label}: failed (`{str(exc)[:120]}`)")
        await message.reply_text("**Connection check:**\n" + "\n".join(checks))
        return

    if action == "add":
        if len(args) < 3:
            await message.reply_text(
                "Usage: `/autoforward add <source> <destination> [copy|forward] "
                "[schedule=HH:MM-HH:MM] [types=all|media|text|photo,video]`"
            )
            return

        source = _normalize_chat_ref(args[1])
        destination = args[2].strip()
        mode, schedule, allowed_types = _parse_add_options(args[3:])

        if not source or not destination:
            await message.reply_text("Invalid source or destination.")
            return

        await auto_forward_rules_collection.update_one(
            {"source": source, "destination": destination},
            {
                "$set": {
                    "source": source,
                    "destination": destination,
                    "mode": mode,
                    "enabled": True,
                    "schedule": schedule,
                    "allowed_types": allowed_types,
                    "created_by": user_id,
                    "updated_at": datetime.now(),
                }
            },
            upsert=True,
        )
        _clear_rule_cache()
        await message.reply_text(
            f"Auto-forward enabled: `{source}` -> `{destination}` "
            f"({mode}, schedule={schedule}, types={','.join(allowed_types)})."
        )
        return

    if action == "schedule":
        if len(args) < 4:
            await message.reply_text("Usage: `/autoforward schedule <source> <destination> <always|off|HH:MM-HH:MM>`")
            return

        schedule = _normalize_schedule(args[3])
        if schedule is None:
            await message.reply_text("Invalid schedule. Use `always`, `off`, or `HH:MM-HH:MM` in UTC.")
            return

        result = await auto_forward_rules_collection.update_many(
            {"source": _normalize_chat_ref(args[1]), "destination": args[2].strip()},
            {"$set": {"schedule": schedule, "updated_at": datetime.utcnow()}},
        )
        _clear_rule_cache()
        await message.reply_text(f"Updated schedule on {result.modified_count} rule(s) to `{schedule}`.")
        return

    if action in {"filter", "types"}:
        if len(args) < 4:
            await message.reply_text("Usage: `/autoforward filter <source> <destination> <all|media|text|photo,video>`")
            return

        allowed_types = _normalize_types(args[3])
        result = await auto_forward_rules_collection.update_many(
            {"source": _normalize_chat_ref(args[1]), "destination": args[2].strip()},
            {"$set": {"allowed_types": allowed_types, "updated_at": datetime.utcnow()}},
        )
        _clear_rule_cache()
        await message.reply_text(f"Updated filters on {result.modified_count} rule(s) to `{','.join(allowed_types)}`.")
        return

    if action in {"resetstats", "clearstats"}:
        query = {}
        if len(args) > 1:
            query["source"] = _normalize_chat_ref(args[1])
        if len(args) > 2:
            query["destination"] = args[2].strip()

        result = await auto_forward_rules_collection.update_many(
            query,
            {
                "$set": {
                    "sent_count": 0,
                    "failed_count": 0,
                    "updated_at": datetime.utcnow(),
                },
                "$unset": {
                    "last_seen_at": "",
                    "last_forwarded_at": "",
                    "last_message_id": "",
                    "last_error": "",
                },
            },
        )
        _clear_rule_cache()
        await message.reply_text(f"Reset stats on {result.modified_count} rule(s).")
        return

    if action in {"del", "delete", "remove"}:
        if len(args) < 2:
            await message.reply_text("Usage: `/autoforward del <source> [destination]`")
            return

        query = {"source": _normalize_chat_ref(args[1])}
        if len(args) > 2:
            query["destination"] = args[2].strip()

        result = await auto_forward_rules_collection.delete_many(query)
        _clear_rule_cache()
        await message.reply_text(f"Removed {result.deleted_count} auto-forward rule(s).")
        return

    if action in {"on", "off"}:
        if len(args) < 2:
            await message.reply_text(f"Usage: `/autoforward {action} <source> [destination]`")
            return

        query = {"source": _normalize_chat_ref(args[1])}
        if len(args) > 2:
            query["destination"] = args[2].strip()

        result = await auto_forward_rules_collection.update_many(
            query,
            {"$set": {"enabled": action == "on", "updated_at": datetime.now()}},
        )
        _clear_rule_cache()
        await message.reply_text(f"Updated {result.modified_count} auto-forward rule(s).")
        return

    await message.reply_text(HELP_TEXT)


@app.on_message(filters.all, group=1)
async def auto_forward_handler(client, message):
    if not getattr(message, "chat", None):
        return
    if getattr(message, "service", None) or getattr(message, "outgoing", False):
        return

    rules = await _get_rules()
    if not rules:
        return

    keys = _source_keys(message)
    source_rules = [
        rule for rule in rules
        if rule["source"] in keys
    ]
    if not source_rules:
        return

    for rule in source_rules:
        await _mark_rule_seen(rule, message)
        if not rule.get("enabled", True):
            continue
        if not _is_schedule_active(rule.get("schedule", "always")):
            continue
        if not _type_is_allowed(message, rule.get("allowed_types", ["all"])):
            continue

        success, error = await _deliver_message(client, message, rule)
        await _mark_rule_delivery(rule, success, error)
        if AUTO_FORWARD_DELAY > 0:
            await asyncio.sleep(AUTO_FORWARD_DELAY)
