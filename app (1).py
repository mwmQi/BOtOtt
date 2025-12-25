import asyncio

import requests

import re

import phonenumbers

from phonenumbers import geocoder

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update

import json

import os

BOT_TOKEN = "sex:sex-sex-sex-sex"

bot = Bot(token=BOT_TOKEN)

GROUP_IDS = [-sec]

OTP_FILE = "otp_store.json"

# ============================

# API PANELS

# ============================

API_PANELS = {

    "cr": {

        "url": "http://sex/crapi/dgroup/viewstats",

        "token": "sex=",

        "records": 20

    },

    "mait": {

        "url": "http://sex/crapi/mait/viewstats",

        "token": "sex",

        "records": 20

    }

}

# ============================

# CLI FILTER SETTINGS

# ============================

ALLOWED_CLIS = []

BLOCKED_CLIS = []

CLI_FILTER_MODE = "off"

def cli_passes_filter(cli):

    cli_lower = cli.lower()

    if CLI_FILTER_MODE == "allow":

        return any(a.lower() in cli_lower for a in ALLOWED_CLIS)

    elif CLI_FILTER_MODE == "block":

        return not any(b.lower() in cli_lower for b in BLOCKED_CLIS)

    return True

# ============================

# OTP STORAGE

# ============================

def load_otp_store():

    if not os.path.exists(OTP_FILE):

        return {}

    with open(OTP_FILE, "r") as f:

        return json.load(f)

def save_otp_store(data):

    with open(OTP_FILE, "w") as f:

        json.dump(data, f, indent=2)

# ============================

# FETCH FUNCTIONS

# ============================

def fetch_latest(panel):

    cfg = API_PANELS[panel]

    try:

        response = requests.get(cfg["url"], params={

            "token": cfg["token"],

            "records": cfg["records"]

        }, timeout=10)

        data = response.json()

        if data.get("status") != "success":

            print(f"{panel.upper()} API Error:", data)

            return None

        records = data.get("data", [])

        if not records:

            return None

        latest = records[0]

        return {

            "time": latest.get("dt", ""),

            "number": latest.get("num", ""),

            "service": latest.get("cli", ""),

            "message": latest.get("message", "")

        }

    except Exception as e:

        print(f"{panel.upper()} Fetch Error:", e)

        return None

# ============================

# HELPERS

# ============================

def extract_otp(message):

    for pat in [r'\d{6}', r'\d{4}', r'\d{3}-\d{3}']:

        match = re.search(pat, message)

        if match:

            return match.group(0)

    return None

def mask_number(number_str):

    try:

        number_str = f"+{number_str}"

        length = len(number_str)

        show_first = 5 if length >= 10 else 4

        show_last = 4 if length >= 10 else 2

        stars = "*" * (length - show_first - show_last)

        return f"{number_str[:show_first]}{stars}{number_str[-show_last:]}"

    except:

        return f"+{number_str}"

def get_country_info(number_str):

    try:

        if not number_str.startswith("+"):

            number_str = "+" + number_str

        parsed = phonenumbers.parse(number_str)

        country_name = geocoder.description_for_number(parsed, "en")

        region = phonenumbers.region_code_for_number(parsed)

        if region:

            base = 127462 - ord("A")

            flag = chr(base + ord(region[0])) + chr(base + ord(region[1]))

        else:

            flag = "🌍"

        return country_name or "Unknown", flag

    except:

        return "Unknown", "🌍"

def format_message(record):

    raw = record["message"]

    otp = extract_otp(raw)

    clean = raw.replace("<", "&lt;").replace(">", "&gt;")

    country, flag = get_country_info(record["number"])

    masked = mask_number(record["number"])

    return f"""

<b>{flag} New {record['service']} OTP!</b>
<blockquote>🕐 Time: {record['time']}</blockquote>
<blockquote>{flag} Country: {country}</blockquote>
<blockquote>📊 Service: {record['service']}</blockquote>
<blockquote>🔢 Number: {masked}</blockquote>
<blockquote>💠 OTP: <code>{otp}</code></blockquote>
<blockquote>📝 Full Message:</blockquote>
<pre>{clean}</pre>
Powered by ❤️ <b> Prime OTP </b> ❤️ 
Support 👥 <strong>  </strong> 👥

"""

async def send_to_all_groups(msg):

    keyboard = InlineKeyboardMarkup([

        [

            InlineKeyboardButton("🧮 Numbers", url="https://t.me/sex"),

            InlineKeyboardButton("💌 Discussion", url="https://t.me/sex")

        ],

        [

            InlineKeyboardButton("👨‍💻 Developer", url="https://t.me/sex"),

            InlineKeyboardButton("✅ OTP", url="https://t.me/sex")

        ]

    ])

    for gid in GROUP_IDS:

        try:

            await bot.send_message(chat_id=gid, text=msg, parse_mode="HTML", reply_markup=keyboard)

        except Exception as e:

            print(f"Send Error -> {gid}: {e}")

# ============================

# COMMAND HANDLER LOOP

# ============================

async def command_listener():

    offset = 0

    while True:

        try:

            updates = await bot.get_updates(offset=offset, timeout=10)

            for update in updates:

                offset = update.update_id + 1

                if update.message and update.message.text:

                    text = update.message.text

                    chat_id = update.message.chat_id

                    if text.startswith("/start"):

                        keyboard = InlineKeyboardMarkup([

                            [

                                InlineKeyboardButton("Join Group", url="https://t.me/sex"),

                                InlineKeyboardButton("Join Channel", url="https://t.me/sex")

                            ],

                            [

                                InlineKeyboardButton("Developer", url="https://t.me/sex")

                            ]

                        ])

                        await bot.send_message(

                            chat_id=chat_id,

                            text="✅ Bot is working and active\nFor more details: @sex",

                            reply_markup=keyboard

                        )

                    elif text.startswith("/otpfor"):

                        parts = text.split()

                        if len(parts) < 2:

                            await bot.send_message(chat_id=chat_id, text="Usage: /otpfor <number>")

                            continue

                        number = parts[1]

                        store = load_otp_store()

                        if number in store:

                            await bot.send_message(

                                chat_id=chat_id,

                                text=f"🔐 OTP for {number}:\n<code>{store[number]}</code>",

                                parse_mode="HTML"

                            )

                        else:

                            found = False

                            for panel in API_PANELS:

                                data = fetch_latest(panel)

                                if data and number in data["number"]:

                                    otp = extract_otp(data["message"])

                                    if otp:

                                        store[number] = otp

                                        save_otp_store(store)

                                        await bot.send_message(

                                            chat_id=chat_id,

                                            text=f"✅ OTP Found & Saved:\n<code>{otp}</code>",

                                            parse_mode="HTML"

                                        )

                                        found = True

                                        break

                            if not found:

                                await bot.send_message(chat_id=chat_id, text="❌ No OTP found for this number.")

        except Exception as e:

            print("Command Listener Error:", e)

        await asyncio.sleep(1)

# ============================

# API WORKERS

# ============================

async def api_worker(panel):

    print(f"[STARTED] {panel.upper()} Worker")

    last = None

    while True:

        data = fetch_latest(panel)

        if data:

            if not cli_passes_filter(data["service"]):

                await asyncio.sleep(3)

                continue

            uniq = data["number"] + data["message"]

            if uniq != last:

                last = uniq

                otp = extract_otp(data["message"])

                if otp:

                    store = load_otp_store()

                    store[data["number"]] = otp

                    save_otp_store(store)

                msg = format_message(data)

                await send_to_all_groups(msg)

                print(f"[{panel.upper()}] Sent: {data['service']} | {data['number']}")

        await asyncio.sleep(3)

# ============================

# MAIN

# ============================

async def main():

    tasks = [api_worker(panel) for panel in API_PANELS]

    tasks.append(command_listener())

    await asyncio.gather(*tasks)

if __name__ == "__main__":

    asyncio.run(main())