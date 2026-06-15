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
    AUTO_FORWARD_SOURCE,
    OWNER_ID,
)
from shared_client import app
from utils.func import auto_forward_rules_collection

logger = logging.getLogger(__name__)

VALID_MODES = {"copy", "forward"}
CACHE_SECONDS = 10
_RULE_CACHE = {"expires_at": 0.0, "rules": []}


HELP_TEXT = """**Auto Forward Bot**

Owner commands:
`/autoforward add <source> <destination> [copy|forward]`
`/autoforward del <source> [destination]`
`/autoforward on <source> [destination]`
`/autoforward off <source> [destination]`
`/autoforward list`

Examples:
`/autoforward add -1001111111111 -1002222222222`
`/autoforward add @sourcechannel @targetchannel forward`
`/autoforward add -1001111111111 -1002222222222/45`

Use `copy` to repost without a forwarded header, or `forward` to keep the forwarded header."""


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

    if AUTO_FORWARD_ENABLED and AUTO_FORWARD_SOURCE and AUTO_FORWARD_DESTINATION:
        rules.append(
            {
                "source": _normalize_chat_ref(AUTO_FORWARD_SOURCE),
                "destination": AUTO_FORWARD_DESTINATION.strip(),
                "mode": mode,
                "enabled": True,
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
                    "source": _normalize_chat_ref(rule.get("source")),
                    "destination": str(rule.get("destination", "")).strip(),
                    "mode": _normalize_mode(rule.get("mode")),
                    "enabled": bool(rule.get("enabled", True)),
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
        return False

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
        return True
    except Exception as exc:
        logger.warning(
            "Auto-forward %s failed from %s to %s: %s",
            rule["mode"],
            message.chat.id,
            rule["destination"],
            exc,
        )

    try:
        return await _send_by_file_id(client, message, destination, reply_to_message_id)
    except Exception as exc:
        logger.error(
            "Auto-forward fallback failed from %s to %s: %s",
            message.chat.id,
            rule["destination"],
            exc,
        )
        return False


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
            lines.append(
                f"{index}. `{rule['source']}` -> `{rule['destination']}` "
                f"({rule['mode']}, {status}, {rule['origin']})"
            )
        await message.reply_text("\n".join(lines))
        return

    if action == "add":
        if len(args) < 3:
            await message.reply_text("Usage: `/autoforward add <source> <destination> [copy|forward]`")
            return

        source = _normalize_chat_ref(args[1])
        destination = args[2].strip()
        mode = _normalize_mode(args[3] if len(args) > 3 else AUTO_FORWARD_MODE)

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
                    "created_by": user_id,
                    "updated_at": datetime.now(),
                }
            },
            upsert=True,
        )
        _clear_rule_cache()
        await message.reply_text(f"Auto-forward enabled: `{source}` -> `{destination}` ({mode}).")
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
    matching_rules = [
        rule for rule in rules
        if rule.get("enabled", True) and rule["source"] in keys
    ]
    if not matching_rules:
        return

    for rule in matching_rules:
        await _deliver_message(client, message, rule)
        if AUTO_FORWARD_DELAY > 0:
            await asyncio.sleep(AUTO_FORWARD_DELAY)
