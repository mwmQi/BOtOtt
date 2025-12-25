import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_IDS = {
    int(uid)
    for uid in os.getenv("ADMIN_IDS", "").replace("[", "").replace("]", "").split(",")
    if uid.strip()
}
OTP_LOG_GROUPS = {
    int(gid)
    for gid in os.getenv("OTP_LOG_GROUPS", "").replace("[", "").replace("]", "").split(",")
    if gid.strip()
}
ASSIGNMENT_TTL = int(os.getenv("ASSIGNMENT_TTL", "3600"))
DATA_FILE = Path(os.getenv("DATA_FILE", "data/store.json"))


@dataclass
class NumberEntry:
    value: str
    status: str = "available"  # available | assigned | used
    assigned_to: Optional[int] = None
    assigned_at: Optional[float] = None
    last_otp: Optional[str] = None
    otp_history: List[Dict[str, str]] = field(default_factory=list)

    def mark_assigned(self, user_id: int):
        self.status = "assigned"
        self.assigned_to = user_id
        self.assigned_at = time.time()

    def mark_available(self):
        self.status = "available"
        self.assigned_to = None
        self.assigned_at = None

    def mark_used(self, otp: str, via: str):
        self.status = "used"
        self.last_otp = otp
        self.otp_history.append({
            "otp": otp,
            "timestamp": str(int(time.time())),
            "via": via,
        })


class DataStore:
    def __init__(self, file_path: Path, ttl_seconds: int):
        self.file_path = file_path
        self.ttl_seconds = ttl_seconds
        self.data = {"lists": {}, "users": {}}
        self._load()

    def _load(self):
        if self.file_path.exists():
            with self.file_path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self._save()

    def _save(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def _normalize(self, number: str) -> str:
        return re.sub(r"\D", "", number)

    def _iter_entries(self):
        for list_name, payload in self.data.get("lists", {}).items():
            for entry in payload.get("numbers", []):
                yield list_name, entry

    def add_list(self, name: str, numbers: List[str]) -> Tuple[int, int]:
        normalized = [self._normalize(num) for num in numbers if self._normalize(num)]
        unique_numbers = []
        seen = set()
        for num in normalized:
            if num not in seen:
                seen.add(num)
                unique_numbers.append(num)

        added = 0
        skipped = 0
        numbers_payload = [NumberEntry(value=num).__dict__ for num in unique_numbers]
        if name in self.data["lists"]:
            existing = {entry["value"] for entry in self.data["lists"][name]["numbers"]}
            for entry in numbers_payload:
                if entry["value"] in existing:
                    skipped += 1
                    continue
                self.data["lists"][name]["numbers"].append(entry)
                added += 1
        else:
            self.data["lists"][name] = {"numbers": numbers_payload}
            added = len(numbers_payload)
        self._save()
        return added, skipped

    def list_names(self) -> List[str]:
        return sorted(self.data.get("lists", {}).keys())

    def user_assignment(self, user_id: int) -> Optional[Dict]:
        return self.data.get("users", {}).get(str(user_id))

    def _release_expired(self):
        now = time.time()
        for list_name, payload in self.data.get("lists", {}).items():
            for entry in payload.get("numbers", []):
                if (
                    entry.get("status") == "assigned"
                    and entry.get("assigned_at")
                    and now - float(entry["assigned_at"]) > self.ttl_seconds
                ):
                    entry["status"] = "available"
                    entry["assigned_to"] = None
                    entry["assigned_at"] = None
                    user_entry = self.data.get("users", {}).get(str(entry.get("assigned_to")))
                    if user_entry and user_entry.get("number") == entry.get("value"):
                        self.data["users"].pop(str(entry.get("assigned_to")), None)
        self._save()

    def assign_number(self, list_name: str, user_id: int, username: Optional[str]) -> Optional[NumberEntry]:
        self._release_expired()
        lists = self.data.get("lists", {})
        if list_name not in lists:
            return None
        current = self.user_assignment(user_id)
        if current:
            if current.get("list") == list_name:
                return self._entry_by_value(list_name, current.get("number"))
            self.release_number(user_id)
        for entry in lists[list_name].get("numbers", []):
            if entry.get("status") == "available":
                entry_obj = NumberEntry(**entry)
                entry_obj.mark_assigned(user_id)
                updated = entry_obj.__dict__
                self._update_entry(list_name, updated)
                self.data.setdefault("users", {})[str(user_id)] = {
                    "list": list_name,
                    "number": entry_obj.value,
                    "assigned_at": entry_obj.assigned_at,
                    "username": username,
                }
                self._save()
                return entry_obj
        return None

    def release_number(self, user_id: int) -> bool:
        current = self.user_assignment(user_id)
        if not current:
            return False
        list_name = current["list"]
        number = current["number"]
        entry = self._entry_by_value(list_name, number)
        if not entry:
            return False
        entry.mark_available()
        self._update_entry(list_name, entry.__dict__)
        self.data.get("users", {}).pop(str(user_id), None)
        self._save()
        return True

    def _entry_by_value(self, list_name: str, number: str) -> Optional[NumberEntry]:
        payload = self.data.get("lists", {}).get(list_name, {})
        for entry in payload.get("numbers", []):
            if entry.get("value") == number:
                return NumberEntry(**entry)
        return None

    def _update_entry(self, list_name: str, updated_entry: Dict):
        payload = self.data.get("lists", {}).get(list_name, {})
        for idx, entry in enumerate(payload.get("numbers", [])):
            if entry.get("value") == updated_entry.get("value"):
                payload["numbers"][idx] = updated_entry
                return

    def assign_by_number(self, number: str) -> Optional[Tuple[str, NumberEntry]]:
        number = self._normalize(number)
        for list_name, entry in self._iter_entries():
            if entry.get("value") == number:
                return list_name, NumberEntry(**entry)
        return None

    def deliver_otp(self, number: str, otp: str, source: str) -> Optional[int]:
        number = self._normalize(number)
        found = self.assign_by_number(number)
        if not found:
            return None
        list_name, entry = found
        entry.mark_used(otp, source)
        entry.status = "used"
        self._update_entry(list_name, entry.__dict__)
        user_id = entry.assigned_to
        if user_id:
            self.data.get("users", {}).pop(str(user_id), None)
        self._save()
        return user_id

    def stats(self) -> Dict[str, int]:
        self._release_expired()
        total = available = assigned = used = 0
        for _list, entry in self._iter_entries():
            total += 1
            status = entry.get("status")
            if status == "available":
                available += 1
            elif status == "assigned":
                assigned += 1
            elif status == "used":
                used += 1
        return {
            "total": total,
            "available": available,
            "assigned": assigned,
            "used": used,
            "active_users": len(self.data.get("users", {})),
        }


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and (user_id in ADMIN_IDS)


data_store = DataStore(DATA_FILE, ASSIGNMENT_TTL)


# =============================
# UI HELPERS
# =============================

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📞 Get Number", callback_data="menu:get")],
            [InlineKeyboardButton("🔄 Change Number", callback_data="menu:change")],
            [InlineKeyboardButton("📋 My Number", callback_data="menu:mine")],
            [InlineKeyboardButton("🧹 Release Number", callback_data="menu:release")],
        ]
    )


def list_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"📂 {name}", callback_data=f"list:{name}")]
        for name in data_store.list_names()
    ]
    if not buttons:
        buttons = [[InlineKeyboardButton("No lists yet", callback_data="noop")]]
    return InlineKeyboardMarkup(buttons)


# =============================
# HANDLERS
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        "<b>OTP Manager Bot</b>\n"
        "Assigns unique numbers to each user, captures OTPs, and frees numbers automatically."\
        "\nSelect an option below."
    )
    await update.effective_message.reply_html(text, reply_markup=main_menu())


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user_id = query.from_user.id

    if action in {"get", "change"}:
        await query.edit_message_text(
            "Choose a list to get a number from:",
            reply_markup=list_keyboard(),
        )
        return

    if action == "mine":
        assignment = data_store.user_assignment(user_id)
        if not assignment:
            await query.edit_message_text(
                "You do not have a number yet. Use <b>Get Number</b> to request one.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )
            return
        msg = (
            f"<b>Current Number</b>\n"
            f"List: <code>{assignment['list']}</code>\n"
            f"Number: <code>{assignment['number']}</code>\n"
            f"Assigned: <code>{time.ctime(float(assignment['assigned_at']))}</code>"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=main_menu())
        return

    if action == "release":
        if data_store.release_number(user_id):
            await query.edit_message_text(
                "Number released. You can request another anytime.",
                reply_markup=main_menu(),
            )
        else:
            await query.edit_message_text(
                "No active number to release.", reply_markup=main_menu()
            )


async def handle_list_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, list_name = query.data.split(":", 1)
    user = query.from_user
    assigned = data_store.assign_number(list_name, user.id, user.username)
    if not assigned:
        await query.edit_message_text(
            f"No available numbers in <b>{list_name}</b>. Try another list.",
            parse_mode=ParseMode.HTML,
            reply_markup=list_keyboard(),
        )
        return
    msg = (
        f"<b>{list_name} number assigned!</b>\n"
        f"Number: <code>{assigned.value}</code>\n"
        "OTP notifications will be sent here automatically."
    )
    await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stats = data_store.stats()
    lines = [
        "<b>Admin Dashboard</b>",
        f"Total numbers: <code>{stats['total']}</code>",
        f"Available: <code>{stats['available']}</code>",
        f"Assigned: <code>{stats['assigned']}</code>",
        f"Used: <code>{stats['used']}</code>",
        f"Active users: <code>{stats['active_users']}</code>",
    ]
    await update.effective_message.reply_html("\n".join(lines))


async def list_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    names = data_store.list_names()
    if not names:
        await update.effective_message.reply_text("No lists available yet.")
        return
    lines = []
    for name in names:
        payload = data_store.data.get("lists", {}).get(name, {})
        total = len(payload.get("numbers", []))
        available = len([n for n in payload.get("numbers", []) if n.get("status") == "available"])
        assigned = len([n for n in payload.get("numbers", []) if n.get("status") == "assigned"])
        used = len([n for n in payload.get("numbers", []) if n.get("status") == "used"])
        lines.append(
            f"<b>{name}</b> — total: <code>{total}</code> | available: <code>{available}</code> | "
            f"assigned: <code>{assigned}</code> | used: <code>{used}</code>"
        )
    await update.effective_message.reply_html("\n".join(lines))


async def upload_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message: Message = update.effective_message
    if not message.document:
        await message.reply_text("Attach a .txt file with numbers. Caption = list name.")
        return
    list_name = message.caption or "default"
    file = await message.document.get_file()
    file_path = await file.download_to_drive()
    with open(file_path, "r", encoding="utf-8") as f:
        numbers = [line.strip() for line in f if line.strip()]
    added, skipped = data_store.add_list(list_name, numbers)
    os.remove(file_path)
    await message.reply_html(
        f"<b>{list_name}</b> uploaded. Added <code>{added}</code> numbers. Skipped <code>{skipped}</code> duplicates."
    )


async def manual_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /logotp <number> <otp>")
        return
    number = context.args[0]
    otp = context.args[1]
    user_id = data_store.deliver_otp(number, otp, source="manual")
    if user_id:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"OTP for <code>{number}</code>: <b>{otp}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await update.effective_message.reply_text("Delivered to assigned user.")
    else:
        await update.effective_message.reply_text("Number not found or not assigned.")


async def otp_from_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if OTP_LOG_GROUPS and message.chat_id not in OTP_LOG_GROUPS:
        return
    text = message.text or message.caption or ""
    number_match = re.findall(r"\d{7,15}", text)
    otp_match = re.findall(r"\b\d{4,8}\b", text)
    if not number_match or not otp_match:
        return
    number = number_match[0]
    otp = otp_match[0]
    user_id = data_store.deliver_otp(number, otp, source="auto")
    if user_id:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"<b>OTP received!</b>\nNumber: <code>{number}</code>\nOTP: <b>{otp}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            "Use the menu buttons to manage your number."
        )


async def periodic_cleanup(context: ContextTypes.DEFAULT_TYPE):
    data_store._release_expired()


def build_application() -> Application:
    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Please configure BOT_TOKEN env var.")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_dashboard))
    app.add_handler(CommandHandler("lists", list_overview))
    app.add_handler(CommandHandler("logotp", manual_otp))
    app.add_handler(CallbackQueryHandler(handle_list_selection, pattern=r"^list:"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.Document.ALL, upload_numbers))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), otp_from_group))
    app.add_handler(MessageHandler(filters.ALL, fallback))

    job_queue = app.job_queue
    job_queue.run_repeating(periodic_cleanup, interval=600, first=30)
    return app


def main():
    app = build_application()
    print("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
