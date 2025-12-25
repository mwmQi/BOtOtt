"""
Telegram OTP forwarding bot.

Usage instructions:
1) Install dependencies (Python 3.10+):
   python -m pip install -r requirements.txt

2) Provide configuration via environment variables or config.json (see examples below):
   export BOT_TOKEN="123:ABC"
   export ADMIN_IDS="12345,67890"
   export GROUP_IDS_LOG="-1001234"

3) Run the bot:
   python bot.py

Example config.json structure:
{
  "bot_token": "123:ABC",
  "admin_ids": [123456789],
  "group_ids_log": [-100123456789],
  "panels": [
    {"name": "cr", "url": "https://example/api", "token": "secret", "records": 10}
  ],
  "release_timeout_minutes": 15,
  "keep_used_locked": true,
  "numbers_file": "data/numbers.json",
  "assignments_file": "data/assignments.json",
  "otp_history_file": "data/otp_history.json"
}

Example numbers.json structure:
[
  {"number": "+15550000001", "country": "US", "status": "available", "notes": "batch-1"},
  {"number": "+15550000002", "status": "available"}
]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import phonenumbers
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_NUMBERS_FILE = Path("data/numbers.json")
DEFAULT_ASSIGNMENTS_FILE = Path("data/assignments.json")
DEFAULT_OTP_HISTORY_FILE = Path("data/otp_history.json")


@dataclass
class PanelConfig:
    name: str
    url: str
    token: str
    records: int = 10


@dataclass
class Config:
    bot_token: str
    admin_ids: List[int] = field(default_factory=list)
    group_ids_log: List[int] = field(default_factory=list)
    panels: List[PanelConfig] = field(default_factory=list)
    release_timeout_minutes: int = 15
    keep_used_locked: bool = True
    numbers_file: Path = DEFAULT_NUMBERS_FILE
    assignments_file: Path = DEFAULT_ASSIGNMENTS_FILE
    otp_history_file: Path = DEFAULT_OTP_HISTORY_FILE

    @staticmethod
    def _parse_int_list(value: str) -> List[int]:
        return [int(v.strip()) for v in value.split(",") if v.strip()]

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = {}

        def pick(name: str, default: Any = None):
            env = os.getenv(name.upper())
            return env if env is not None else raw.get(name.lower(), raw.get(name)) or default

        panels_raw = raw.get("panels", [])
        env_panels = os.getenv("PANELS")
        if env_panels:
            try:
                panels_raw = json.loads(env_panels)
            except json.JSONDecodeError:
                panels_raw = []

        panels = [
            PanelConfig(
                name=p.get("name"),
                url=p.get("url"),
                token=p.get("token"),
                records=int(p.get("records", 10)),
            )
            for p in panels_raw
            if p.get("name") and p.get("url") and p.get("token")
        ]

        bot_token = pick("BOT_TOKEN")
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required via env or config.json")

        admin_ids_val = pick("ADMIN_IDS", "")
        group_ids_val = pick("GROUP_IDS_LOG", "")

        return cls(
            bot_token=bot_token,
            admin_ids=cls._parse_int_list(admin_ids_val) if isinstance(admin_ids_val, str) else admin_ids_val,
            group_ids_log=cls._parse_int_list(group_ids_val) if isinstance(group_ids_val, str) else group_ids_val,
            panels=panels,
            release_timeout_minutes=int(pick("RELEASE_TIMEOUT_MINUTES", 15)),
            keep_used_locked=bool(raw.get("keep_used_locked", True)),
            numbers_file=Path(pick("NUMBERS_FILE", DEFAULT_NUMBERS_FILE)),
            assignments_file=Path(pick("ASSIGNMENTS_FILE", DEFAULT_ASSIGNMENTS_FILE)),
            otp_history_file=Path(pick("OTP_HISTORY_FILE", DEFAULT_OTP_HISTORY_FILE)),
        )


def ensure_parent(path: Path) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


def atomic_save(path: Path, data: Any) -> None:
    ensure_parent(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        ensure_parent(path)
        atomic_save(path, default)
        return default
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


class StateManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.numbers: List[Dict[str, Any]] = load_json(cfg.numbers_file, [])
        stored_assignments = load_json(cfg.assignments_file, {"assignments": {}, "known_users": []})
        self.assignments: Dict[str, Dict[str, Any]] = stored_assignments.get("assignments", {})
        self.known_users: set[str] = set(str(uid) for uid in stored_assignments.get("known_users", []))
        self.otp_history: List[Dict[str, Any]] = load_json(cfg.otp_history_file, [])

    async def save(self) -> None:
        async with self.lock:
            atomic_save(self.cfg.numbers_file, self.numbers)
            atomic_save(
                self.cfg.assignments_file,
                {"assignments": self.assignments, "known_users": sorted(self.known_users)},
            )
            atomic_save(self.cfg.otp_history_file, self.otp_history)

    async def register_user(self, user_id: int) -> None:
        async with self.lock:
            self.known_users.add(str(user_id))
            atomic_save(
                self.cfg.assignments_file,
                {"assignments": self.assignments, "known_users": sorted(self.known_users)},
            )

    async def get_assignment(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.lock:
            return self.assignments.get(str(user_id))

    async def assign_number(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.lock:
            current = self.assignments.get(str(user_id))
            if current:
                num = self._find_number(current["number"])
                if num and num.get("status") == "assigned" and not current.get("last_otp"):
                    return current

            available = next((n for n in self.numbers if n.get("status", "available") == "available"), None)
            if not available:
                return None

            now = datetime.now(timezone.utc).isoformat()
            available.update(
                {
                    "status": "assigned",
                    "assigned_to": user_id,
                    "assigned_at": now,
                    "last_otp_at": None,
                }
            )
            self.assignments[str(user_id)] = {
                "number": available["number"],
                "assigned_at": now,
                "last_otp": None,
                "reassign_count": self.assignments.get(str(user_id), {}).get("reassign_count", 0),
            }
            await self.save()
            return self.assignments[str(user_id)]

    async def change_number(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.lock:
            current = self.assignments.get(str(user_id))
            if current:
                current_number = self._find_number(current["number"])
                if current_number:
                    current_number.update(
                        {
                            "status": "available" if not current.get("last_otp") else "used",
                            "assigned_to": None,
                            "assigned_at": None,
                        }
                    )
            available = next((n for n in self.numbers if n.get("status", "available") == "available"), None)
            if not available:
                await self.save()
                return None
            now = datetime.now(timezone.utc).isoformat()
            available.update(
                {
                    "status": "assigned",
                    "assigned_to": user_id,
                    "assigned_at": now,
                    "last_otp_at": None,
                }
            )
            self.assignments[str(user_id)] = {
                "number": available["number"],
                "assigned_at": now,
                "last_otp": None,
                "reassign_count": self.assignments.get(str(user_id), {}).get("reassign_count", 0) + 1,
            }
            await self.save()
            return self.assignments[str(user_id)]

    async def release_number(self, number: str, mark_used: bool = False) -> bool:
        async with self.lock:
            target = self._find_number(number)
            if not target:
                return False
            status = "used" if mark_used else "available"
            target.update(
                {
                    "status": status,
                    "assigned_to": None,
                    "assigned_at": None,
                }
            )
            for uid, info in list(self.assignments.items()):
                if info.get("number") == number:
                    del self.assignments[uid]
            await self.save()
            return True

    async def release_expired(self) -> List[str]:
        expired: List[str] = []
        async with self.lock:
            now = datetime.now(timezone.utc)
            for num in self.numbers:
                if num.get("status") != "assigned" or not num.get("assigned_at"):
                    continue
                assigned_at = datetime.fromisoformat(num["assigned_at"])
                if now - assigned_at >= timedelta(minutes=self.cfg.release_timeout_minutes) and not num.get("last_otp_at"):
                    num.update(
                        {
                            "status": "available",
                            "assigned_to": None,
                            "assigned_at": None,
                        }
                    )
                    expired.append(num["number"])
                    for uid, info in list(self.assignments.items()):
                        if info.get("number") == num["number"]:
                            del self.assignments[uid]
            if expired:
                await self.save()
        return expired

    async def add_numbers(self, numbers: List[str], country: Optional[str] = None) -> int:
        async with self.lock:
            existing = {n.get("number") for n in self.numbers}
            added = 0
            for num in numbers:
                if num in existing:
                    continue
                self.numbers.append(
                    {
                        "number": num,
                        "country": country,
                        "status": "available",
                        "assigned_to": None,
                        "assigned_at": None,
                        "last_otp_at": None,
                    }
                )
                added += 1
            if added:
                await self.save()
            return added

    async def block_number(self, number: str) -> bool:
        async with self.lock:
            target = self._find_number(number)
            if not target:
                return False
            target.update({"status": "blocked", "assigned_to": None, "assigned_at": None})
            for uid, info in list(self.assignments.items()):
                if info.get("number") == number:
                    del self.assignments[uid]
            await self.save()
            return True

    async def track_otp(self, number: str, otp: str, service: str, panel: str) -> Optional[int]:
        async with self.lock:
            number_entry = self._find_number(number)
            assigned_user: Optional[int] = None
            if number_entry:
                number_entry["last_otp_at"] = datetime.now(timezone.utc).isoformat()
                if number_entry.get("status") == "assigned":
                    assigned_user = int(number_entry.get("assigned_to")) if number_entry.get("assigned_to") else None
                if self.cfg.keep_used_locked:
                    number_entry["status"] = "used"
            for uid, info in self.assignments.items():
                if info.get("number") == number:
                    info["last_otp"] = otp
                    info["last_otp_at"] = datetime.now(timezone.utc).isoformat()
                    assigned_user = int(uid)
            self.otp_history.append(
                {
                    "number": number,
                    "otp": otp,
                    "service": service,
                    "panel": panel,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "assigned_user": assigned_user,
                }
            )
            await self.save()
            return assigned_user

    def _find_number(self, number: str) -> Optional[Dict[str, Any]]:
        for n in self.numbers:
            if n.get("number") == number or n.get("number") == f"+{number.lstrip('+')}":
                return n
        return None

    async def stock_summary(self) -> Dict[str, int]:
        async with self.lock:
            total = len(self.numbers)
            statuses = {"available": 0, "assigned": 0, "used": 0, "blocked": 0}
            for n in self.numbers:
                statuses[n.get("status", "available")] = statuses.get(n.get("status", "available"), 0) + 1
            return {"total": total, **statuses}

    async def active_users_list(self) -> List[Dict[str, Any]]:
        async with self.lock:
            result = []
            for uid, info in self.assignments.items():
                number = self._find_number(info.get("number"))
                result.append(
                    {
                        "user_id": int(uid),
                        "number": info.get("number"),
                        "assigned_at": info.get("assigned_at"),
                        "last_otp": info.get("last_otp"),
                        "last_otp_at": info.get("last_otp_at"),
                        "number_status": number.get("status") if number else "unknown",
                    }
                )
            return result


def extract_otp(message: str) -> Optional[str]:
    patterns = [r"\b\d{6}\b", r"\b\d{4}\b", r"\b\d{3}-\d{3}\b", r"\b\d{8}\b"]
    for pat in patterns:
        match = re.search(pat, message)
        if match:
            return match.group(0).replace("-", "")
    return None


def normalize_number(number: str) -> str:
    cleaned = re.sub(r"[^0-9+]", "", number)
    return cleaned.lstrip("+")


def mask_number(number_str: str) -> str:
    number_str = number_str.lstrip("+")
    length = len(number_str)
    if length <= 4:
        return "****"
    show_first = 3
    show_last = 2
    stars = "*" * max(0, length - show_first - show_last)
    return f"+{number_str[:show_first]}{stars}{number_str[-show_last:]}"


def country_flag(number_str: str) -> str:
    try:
        if not number_str.startswith("+"):
            number_str = "+" + number_str
        parsed = phonenumbers.parse(number_str)
        region = phonenumbers.region_code_for_number(parsed)
        base = 127462 - ord("A")
        return chr(base + ord(region[0])) + chr(base + ord(region[1])) if region else "🌍"
    except Exception:
        return "🌍"


def format_otp_message(number: str, otp: str, service: str, panel: str) -> str:
    masked = mask_number(number)
    flag = country_flag(number)
    return (
        f"<b>{flag} OTP Received</b>\n"
        f"🔢 Number: <code>{masked}</code>\n"
        f"🏷 Service: <b>{service}</b>\n"
        f"🔐 OTP: <code>{otp}</code>\n"
        f"🗄 Source: {panel}"
    )


async def send_to_log_groups(app: Application, cfg: Config, text: str) -> None:
    for gid in cfg.group_ids_log:
        try:
            await app.bot.send_message(chat_id=gid, text=text, parse_mode=ParseMode.HTML)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to send to group {gid}: {exc}")


async def handle_otp_record(
    app: Application, state: StateManager, cfg: Config, record: Dict[str, Any], panel: str
) -> None:
    number = normalize_number(record.get("number", ""))
    message = record.get("message", "")
    service = record.get("service", "unknown")
    otp = extract_otp(message)
    if not otp:
        return
    assigned_user = await state.track_otp(number, otp, service, panel)
    formatted = format_otp_message(number, otp, service, panel)
    if assigned_user:
        try:
            await app.bot.send_message(chat_id=assigned_user, text=formatted, parse_mode=ParseMode.HTML)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to send OTP to user {assigned_user}: {exc}")
    await send_to_log_groups(app, cfg, formatted)


async def poll_panel(context: CallbackContext) -> None:
    cfg: Config = context.application.bot_data["config"]
    state: StateManager = context.application.bot_data["state"]
    panel: PanelConfig = context.job.data["panel"]
    last_key: Optional[str] = context.job.data.get("last_key")
    try:
        response = requests.get(
            panel.url,
            params={"token": panel.token, "records": panel.records},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("data") or []
        if not records:
            return
        latest = records[0]
        uniq = f"{latest.get('num')}-{latest.get('message')}"
        if uniq == last_key:
            return
        context.job.data["last_key"] = uniq
        record = {
            "number": latest.get("num", ""),
            "service": latest.get("cli", ""),
            "message": str(latest.get("message", "")),
        }
        await handle_otp_record(context.application, state, cfg, record, panel.name)
    except Exception as exc:  # noqa: BLE001
        print(f"Panel {panel.name} error: {exc}")


async def release_worker(context: CallbackContext) -> None:
    state: StateManager = context.application.bot_data["state"]
    expired = await state.release_expired()
    if expired:
        print(f"Released expired numbers: {', '.join(expired)}")


def admin_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cfg: Config = context.application.bot_data["config"]
        user_id = update.effective_user.id if update.effective_user else 0
        if user_id not in cfg.admin_ids:
            await update.effective_message.reply_text("You are not authorized to use this command.")
            return
        return await fn(update, context)

    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    if update.effective_user:
        await state.register_user(update.effective_user.id)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Get Number", callback_data="get_number"),
                InlineKeyboardButton("Change Number", callback_data="change_number"),
            ],
            [InlineKeyboardButton("My Status", callback_data="my_status")],
            [InlineKeyboardButton("Help", callback_data="help")],
        ]
    )
    await update.message.reply_text(
        "Welcome! Use the buttons below to manage your OTP number.", reply_markup=keyboard
    )


async def get_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    assignment = await state.assign_number(update.effective_user.id)
    if not assignment:
        await update.effective_message.reply_text("No numbers are available right now. Please try later.")
        return
    number = assignment["number"]
    flag = country_flag(number)
    await update.effective_message.reply_text(
        f"Your assigned number: {flag} <code>{number}</code>\nWait for the OTP here.",
        parse_mode=ParseMode.HTML,
    )


async def change_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    assignment = await state.change_number(update.effective_user.id)
    if not assignment:
        await update.effective_message.reply_text("No alternative numbers available right now.")
        return
    number = assignment["number"]
    flag = country_flag(number)
    await update.effective_message.reply_text(
        f"New number assigned: {flag} <code>{number}</code>", parse_mode=ParseMode.HTML
    )


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    assignment = await state.get_assignment(update.effective_user.id)
    if not assignment:
        await update.effective_message.reply_text("You do not have a number yet. Use /getnumber to request one.")
        return
    number = assignment.get("number")
    last_otp = assignment.get("last_otp")
    assigned_at = assignment.get("assigned_at")
    flag = country_flag(number)
    status_lines = [f"Number: {flag} <code>{number}</code>", f"Assigned at: {assigned_at}"]
    if last_otp:
        status_lines.append(f"Last OTP: <code>{last_otp}</code>")
    else:
        status_lines.append("Waiting for OTP...")
    status_lines.append(
        f"Auto-release after {context.application.bot_data['config'].release_timeout_minutes} minutes without OTP."
    )
    await update.effective_message.reply_text("\n".join(status_lines), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "/getnumber - Assign a phone number\n"
        "/changenumber - Change your assigned number\n"
        "/mystatus - View your current status\n"
        "Admins: /admin for dashboard"
    )


@admin_only
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Stock", callback_data="admin_stock")],
            [InlineKeyboardButton("Active Users", callback_data="admin_active")],
            [InlineKeyboardButton("Add Numbers", callback_data="admin_add")],
        ]
    )
    await update.effective_message.reply_text("Admin dashboard:", reply_markup=keyboard)


@admin_only
async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    summary = await state.stock_summary()
    text = (
        f"Total: {summary['total']}\n"
        f"Available: {summary.get('available', 0)}\n"
        f"Assigned: {summary.get('assigned', 0)}\n"
        f"Used: {summary.get('used', 0)}\n"
        f"Blocked: {summary.get('blocked', 0)}"
    )
    await update.effective_message.reply_text(text)


@admin_only
async def active_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    users = await state.active_users_list()
    if not users:
        await update.effective_message.reply_text("No active users.")
        return
    lines = []
    for u in users:
        lines.append(
            f"User {u['user_id']}: {u['number']} (status {u['number_status']}) at {u['assigned_at']}"
        )
    await update.effective_message.reply_text("\n".join(lines))


@admin_only
async def add_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    if update.message and update.message.document:
        file = await update.message.document.get_file()
        content = await file.download_as_bytearray()
        numbers = re.findall(rb"\+?\d+", content)
        numbers_str = [n.decode() for n in numbers]
    else:
        text = update.message.text or ""
        numbers_str = re.findall(r"\+?\d+", text)
    added = await state.add_numbers(numbers_str)
    await update.effective_message.reply_text(f"Added {added} numbers to the pool.")


@admin_only
async def delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /deletenumber <number>")
        return
    success = await state.block_number(parts[1])
    await update.effective_message.reply_text("Blocked." if success else "Number not found.")


@admin_only
async def release_number_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /releasenumber <number>")
        return
    success = await state.release_number(parts[1])
    await update.effective_message.reply_text("Released." if success else "Number not found.")


@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: StateManager = context.application.bot_data["state"]
    text = (update.message.text or "").partition(" ")[2]
    if not text:
        await update.effective_message.reply_text("Usage: /broadcast <text>")
        return
    sent = 0
    for uid in state.known_users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"Broadcast failed to {uid}: {exc}")
    await update.effective_message.reply_text(f"Broadcast sent to {sent} users.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data
    if data == "get_number":
        await get_number(update, context)
    elif data == "change_number":
        await change_number(update, context)
    elif data == "my_status":
        await my_status(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "admin_stock":
        await stock(update, context)
    elif data == "admin_active":
        await active_users(update, context)
    elif data == "admin_add":
        await query.edit_message_text("Send numbers (one per line) or upload a file.")
    else:
        await query.edit_message_text("Unknown action.")


async def post_init(application: Application) -> None:
    cfg: Config = application.bot_data["config"]
    state: StateManager = application.bot_data["state"]
    for panel in cfg.panels:
        application.job_queue.run_repeating(
            poll_panel,
            interval=5,
            first=2,
            data={"panel": panel, "last_key": None},
            name=f"panel-{panel.name}",
        )
    application.job_queue.run_repeating(release_worker, interval=60, first=10, name="release-worker")
    print(f"Loaded {len(state.numbers)} numbers. Panels: {[p.name for p in cfg.panels]}")


def build_application(cfg: Config, state: StateManager) -> Application:
    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter())
        .build()
    )
    app.bot_data["config"] = cfg
    app.bot_data["state"] = state

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getnumber", get_number))
    app.add_handler(CommandHandler("changenumber", change_number))
    app.add_handler(CommandHandler("mystatus", my_status))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("activeusers", active_users))
    app.add_handler(CommandHandler("addnumbers", add_numbers))
    app.add_handler(CommandHandler("deletenumber", delete_number))
    app.add_handler(CommandHandler("releasenumber", release_number_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))

    admin_filter = filters.User(cfg.admin_ids)
    app.add_handler(MessageHandler(filters.Document.ALL & admin_filter, add_numbers))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, add_numbers))

    app.add_handler(CallbackQueryHandler(on_callback))

    return app


def main() -> None:
    cfg = Config.load()
    state = StateManager(cfg)
    app = build_application(cfg, state)
    app.run_polling()


if __name__ == "__main__":
    main()
