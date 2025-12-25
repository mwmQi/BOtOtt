import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

import phonenumbers
import requests
from phonenumbers import geocoder
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update

BOT_TOKEN = "sex:sex-sex-sex-sex"

bot = Bot(token=BOT_TOKEN)

GROUP_IDS = [-sec]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

OTP_FILE = "otp_store.json"
DATA_FILE = "data_store.json"
RECYCLE_AFTER = timedelta(hours=1)

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
# DATA MODELS & STORAGE
# ============================
def ensure_data_defaults(data):
    data.setdefault("users", {})
    data.setdefault("number_pools", {})
    data.setdefault("assignments", [])
    data.setdefault("otp_deliveries", [])
    return data


def load_data_store():
    if not os.path.exists(DATA_FILE):
        return ensure_data_defaults({})
    with open(DATA_FILE, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}
    return ensure_data_defaults(data)


def save_data_store(data):
    ensure_data_defaults(data)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_pool_id(country_code: str, label: str) -> str:
    return f"{country_code.upper()}::{label}"


def add_number_list(country_code: str, label: str, numbers):
    data = load_data_store()
    pool_id = get_pool_id(country_code, label)
    normalized_numbers = []
    for raw in numbers:
        clean = re.sub(r"\D", "", raw)
        if clean:
            normalized_numbers.append(clean)
    unique_numbers = sorted(set(normalized_numbers))
    data["number_pools"][pool_id] = {
        "country": country_code.upper(),
        "label": label,
        "numbers": unique_numbers,
        "available": unique_numbers.copy(),
        "used": [],
    }
    save_data_store(data)
    return pool_id, len(unique_numbers)


def remove_number_list(country_code: str, label: str) -> bool:
    data = load_data_store()
    pool_id = get_pool_id(country_code, label)
    if pool_id in data["number_pools"]:
        data["number_pools"].pop(pool_id)
        data["assignments"] = [a for a in data["assignments"] if a.get("pool_id") != pool_id]
        save_data_store(data)
        return True
    return False


def recycle_expired_assignments(now=None):
    now = now or datetime.now(timezone.utc)
    data = load_data_store()
    changed = False
    for assignment in data["assignments"]:
        if assignment.get("status") == "assigned":
            ts = datetime.fromisoformat(assignment["assigned_at"])
            if now - ts >= RECYCLE_AFTER:
                pool = data["number_pools"].get(assignment["pool_id"])
                if pool and assignment["number"] not in pool["available"]:
                    pool["available"].append(assignment["number"])
                assignment["status"] = "expired"
                assignment["expired_at"] = now.isoformat()
                changed = True
    if changed:
        save_data_store(data)
    return changed


def assign_number_to_user(user_id: int, username: str, pool_id: str):
    recycle_expired_assignments()
    data = load_data_store()
    pool = data["number_pools"].get(pool_id)
    if not pool:
        return None, "❌ List not found."
    # Prevent multiple active assignments per user
    for assignment in data["assignments"]:
        if assignment["user_id"] == user_id and assignment.get("status") == "assigned":
            return assignment["number"], "ℹ️ You already have an active number."
    if not pool["available"]:
        return None, "🚫 No numbers available in this list."
    number = pool["available"].pop(0)
    assignment = {
        "user_id": user_id,
        "username": username,
        "number": number,
        "pool_id": pool_id,
        "assigned_at": datetime.now(timezone.utc).isoformat(),
        "status": "assigned",
    }
    data["assignments"].append(assignment)
    user_entry = data["users"].setdefault(str(user_id), {
        "user_id": user_id,
        "username": username,
        "last_pool": None,
        "assignments": 0,
        "used": 0,
    })
    user_entry["last_pool"] = pool_id
    user_entry["assignments"] += 1
    save_data_store(data)
    return number, "✅ Number assigned successfully!"


def mark_assignment_used(number: str, otp: str, panel: str, message: str):
    data = load_data_store()
    now = datetime.now(timezone.utc).isoformat()
    for assignment in sorted(data["assignments"], key=lambda a: a["assigned_at"], reverse=True):
        if assignment["number"] == number and assignment.get("status") == "assigned":
            assignment["status"] = "used"
            assignment["used_at"] = now
            user_entry = data["users"].get(str(assignment["user_id"]))
            if user_entry:
                user_entry["used"] = user_entry.get("used", 0) + 1
            pool = data["number_pools"].get(assignment["pool_id"])
            if pool and number not in pool.get("used", []):
                pool.setdefault("used", []).append(number)
            break
    delivery = {
        "number": number,
        "otp": otp,
        "panel": panel,
        "message": message,
        "delivered_at": now,
    }
    data.setdefault("otp_deliveries", []).append(delivery)
    save_data_store(data)


def get_usage_stats():
    data = load_data_store()
    stats = {}
    total_assigned = 0
    total_used = 0
    total_remaining = 0
    for pool_id, pool in data["number_pools"].items():
        remaining = len(pool.get("available", []))
        used = len(pool.get("used", []))
        assigned = sum(1 for a in data["assignments"] if a.get("pool_id") == pool_id and a.get("status") == "assigned")
        stats[pool_id] = {
            "country": pool.get("country"),
            "label": pool.get("label"),
            "remaining": remaining,
            "used": used,
            "assigned": assigned,
            "total": len(pool.get("numbers", [])),
        }
        total_assigned += assigned
        total_used += used
        total_remaining += remaining
    return stats, {"assigned": total_assigned, "used": total_used, "remaining": total_remaining}


def list_countries():
    data = load_data_store()
    countries = {}
    for pool_id, pool in data["number_pools"].items():
        country = pool.get("country")
        countries.setdefault(country, []).append(pool_id)
    return countries

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


def flag_for_country(country_code: str):
    try:
        if not country_code or len(country_code) != 2:
            return "🌍"
        base = 127462 - ord("A")
        return chr(base + ord(country_code[0])) + chr(base + ord(country_code[1]))
    except Exception:
        return "🌍"

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


def build_countries_keyboard():

    countries = list_countries()

    rows = []

    for country_code, pools in sorted(countries.items()):

        flag = flag_for_country(country_code)

        rows.append([

            InlineKeyboardButton(

                f"{flag} {country_code} • {len(pools)} list(s)",

                callback_data=f"country:{country_code}",

            )

        ])

    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="select_country")])

    return InlineKeyboardMarkup(rows)


def build_pools_keyboard(country_code: str):

    data = load_data_store()

    rows = []

    for pool_id, pool in data.get("number_pools", {}).items():

        if pool.get("country") != country_code:

            continue

        remaining = len(pool.get("available", []))

        text = f"📋 {pool['label']} • {remaining} left"

        rows.append([InlineKeyboardButton(text, callback_data=f"assign:{pool_id}")])

    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="select_country")])

    return InlineKeyboardMarkup(rows)


def main_menu_keyboard(is_admin: bool = False):

    buttons = [

        [InlineKeyboardButton("🌐 Choose Country", callback_data="select_country")],

        [InlineKeyboardButton("📊 Usage Stats", callback_data="usage_stats")],

    ]

    if is_admin:

        buttons.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])

    return InlineKeyboardMarkup(buttons)


def format_usage_stats():

    pool_stats, totals = get_usage_stats()

    lines = ["📈 <b>Usage Overview</b>"]

    for pool_id, info in pool_stats.items():

        flag = flag_for_country(info.get("country", ""))

        lines.append(

            f"{flag} <b>{info['country']} - {info['label']}</b>\n"

            f" • Assigned: <b>{info['assigned']}</b>\n"

            f" • Used: <b>{info['used']}</b>\n"

            f" • Remaining: <b>{info['remaining']}</b> / {info['total']}"

        )

    lines.append(

        f"\n📦 Totals -> Assigned: <b>{totals['assigned']}</b> | Used: <b>{totals['used']}</b> | Remaining: <b>{totals['remaining']}</b>"

    )

    return "\n\n".join(lines)

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

            recycle_expired_assignments()

            updates = await bot.get_updates(offset=offset, timeout=10)

            for update in updates:

                offset = update.update_id + 1

                if update.callback_query:

                    query = update.callback_query

                    chat_id = query.message.chat_id

                    user = query.from_user

                    data = query.data or ""

                    await bot.answer_callback_query(callback_query_id=query.id)

                    if data == "select_country":

                        await bot.send_message(chat_id=chat_id, text="🌐 Select a country list:", reply_markup=build_countries_keyboard())

                    elif data.startswith("country:"):

                        country = data.split(":", 1)[1]

                        await bot.send_message(

                            chat_id=chat_id,

                            text=f"{flag_for_country(country)} Choose a list for {country}",

                            reply_markup=build_pools_keyboard(country),

                        )

                    elif data.startswith("assign:"):

                        pool_id = data.split(":", 1)[1]

                        number, message = assign_number_to_user(user.id, user.username or "anonymous", pool_id)

                        if number:

                            await bot.send_message(

                                chat_id=chat_id,

                                text=f"🎯 <b>Your Number</b>\n<code>{number}</code>\n{message}\n⏳ Auto-recycles after 1 hour of inactivity.",

                                parse_mode="HTML",

                            )

                        else:

                            await bot.send_message(chat_id=chat_id, text=message)

                    elif data == "usage_stats":

                        await bot.send_message(chat_id=chat_id, text=format_usage_stats(), parse_mode="HTML")

                    elif data == "admin_panel":

                        if is_admin(user.id):

                            admin_text = (

                                "🛠 <b>Admin Panel</b>\n"
                                "• /uploadlist <country> <label> <numbers...>\n"
                                "• /removelist <country> <label>\n"
                                "• /stats — view allocations\n"
                                "• /recycle — force recycle expired assignments"

                            )

                            await bot.send_message(chat_id=chat_id, text=admin_text, parse_mode="HTML")

                        else:

                            await bot.send_message(chat_id=chat_id, text="🚫 Admins only.")

                if update.message and update.message.text:

                    text = update.message.text

                    chat_id = update.message.chat_id

                    user = update.message.from_user

                    username = user.username or "anonymous"

                    if text.startswith("/start"):

                        await bot.send_message(

                            chat_id=chat_id,

                            text="🤖 Welcome! Use the buttons below to pick a country list and get a number.",

                            reply_markup=main_menu_keyboard(is_admin=is_admin(user.id)),

                        )

                    elif text.startswith("/lists"):

                        await bot.send_message(chat_id=chat_id, text="📂 Available countries", reply_markup=build_countries_keyboard())

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

                                            parse_mode="HTML",

                                        )

                                        found = True

                                        break

                            if not found:

                                await bot.send_message(chat_id=chat_id, text="❌ No OTP found for this number.")

                    elif text.startswith("/uploadlist"):

                        if not is_admin(user.id):

                            await bot.send_message(chat_id=chat_id, text="🚫 You are not allowed to upload lists.")

                            continue

                        parts = text.split(maxsplit=3)

                        if len(parts) < 4:

                            await bot.send_message(chat_id=chat_id, text="Usage: /uploadlist <country> <label> <numbers separated by space or comma>")

                            continue

                        country = parts[1]

                        label = parts[2]

                        numbers = re.findall(r"\d+", parts[3])

                        pool_id, count = add_number_list(country, label, numbers)

                        await bot.send_message(chat_id=chat_id, text=f"📥 Added {count} numbers to {pool_id}.")

                    elif text.startswith("/removelist"):

                        if not is_admin(user.id):

                            await bot.send_message(chat_id=chat_id, text="🚫 You are not allowed to remove lists.")

                            continue

                        parts = text.split(maxsplit=2)

                        if len(parts) < 3:

                            await bot.send_message(chat_id=chat_id, text="Usage: /removelist <country> <label>")

                            continue

                        country = parts[1]

                        label = parts[2]

                        if remove_number_list(country, label):

                            await bot.send_message(chat_id=chat_id, text="🗑 List removed.")

                        else:

                            await bot.send_message(chat_id=chat_id, text="❌ List not found.")

                    elif text.startswith("/stats"):

                        await bot.send_message(chat_id=chat_id, text=format_usage_stats(), parse_mode="HTML")

                    elif text.startswith("/recycle"):

                        if not is_admin(user.id):

                            await bot.send_message(chat_id=chat_id, text="🚫 Admins only.")

                            continue

                        changed = recycle_expired_assignments()

                        await bot.send_message(chat_id=chat_id, text="♻️ Recycled." if changed else "♻️ Nothing to recycle.")

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

                    mark_assignment_used(data["number"], otp, panel, data["message"])

                msg = format_message(data)

                await send_to_all_groups(msg)

                print(f"[{panel.upper()}] Sent: {data['service']} | {data['number']}")

        await asyncio.sleep(3)


async def recycler_worker():

    while True:

        recycle_expired_assignments()

        await asyncio.sleep(60)

# ============================

# MAIN

# ============================

async def main():

    tasks = [api_worker(panel) for panel in API_PANELS]

    tasks.append(command_listener())

    tasks.append(recycler_worker())

    await asyncio.gather(*tasks)

if __name__ == "__main__":

    asyncio.run(main())