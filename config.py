# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import os
from dotenv import load_dotenv

load_dotenv()

# VPS --- FILL COOKIES 🍪 in """ ... """ 

def get_env(name, default=""):
    value = os.getenv(name)
    return default if value is None or value == "" else value


def get_int_env(name, default=0):
    try:
        return int(get_env(name, str(default)))
    except (TypeError, ValueError):
        return default


INST_COOKIES = """
# wtite up here insta cookies
"""

YTUB_COOKIES = """
# write here yt cookies
"""

API_ID = get_env("API_ID", "")
API_HASH = get_env("API_HASH", "")
BOT_TOKEN = get_env("BOT_TOKEN", "")
MONGO_DB = get_env("MONGO_DB", "")
OWNER_ID = list(map(int, get_env("OWNER_ID", "").split())) # list seperated via space
DB_NAME = get_env("DB_NAME", "telegram_downloader")
STRING = get_env("STRING", None) # optional
LOG_GROUP = get_int_env("LOG_GROUP", -1001234456) # optional with -100
FORCE_SUB = get_int_env("FORCE_SUB", 0) # optional with -100
MASTER_KEY = get_env("MASTER_KEY", "gK8HzLfT9QpViJcYeB5wRa3DmN7P2xUq") # for session encryption
IV_KEY = get_env("IV_KEY", "s7Yx5CpVmE3F") # for decryption
YT_COOKIES = get_env("YT_COOKIES", YTUB_COOKIES)
INSTA_COOKIES = get_env("INSTA_COOKIES", INST_COOKIES)
FREEMIUM_LIMIT = get_int_env("FREEMIUM_LIMIT", 10000)
PREMIUM_LIMIT = get_int_env("PREMIUM_LIMIT", 10000)
JOIN_LINK = get_env("JOIN_LINK", "https://t.me/team_spy_pro") # this link for start command message
ADMIN_CONTACT = get_env("ADMIN_CONTACT", "https://t.me/username_of_admin")
AUTO_FORWARD_ENABLED = get_env("AUTO_FORWARD_ENABLED", "False").lower() in ("true", "1", "yes", "on")
AUTO_FORWARD_SOURCE = get_env("AUTO_FORWARD_SOURCE", "") # source chat id or username
AUTO_FORWARD_DESTINATION = get_env("AUTO_FORWARD_DESTINATION", "") # destination chat id or username
AUTO_FORWARD_PAIRS = get_env("AUTO_FORWARD_PAIRS", "") # source=destination pairs separated by semicolon
AUTO_FORWARD_MODE = get_env("AUTO_FORWARD_MODE", "copy") # copy or forward
AUTO_FORWARD_DELAY = float(get_env("AUTO_FORWARD_DELAY", "0")) # optional delay between destinations
AUTO_FORWARD_SCHEDULE = get_env("AUTO_FORWARD_SCHEDULE", "always") # always, off, or HH:MM-HH:MM UTC
AUTO_FORWARD_TYPES = get_env("AUTO_FORWARD_TYPES", "all") # all, media, text, or comma-separated media types


