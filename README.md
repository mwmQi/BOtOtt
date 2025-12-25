# OTP Manager Bot

An async Telegram bot that assigns unique phone numbers to users, captures OTP messages, and keeps admins in control with uploads and stats.

## Features
- **User-friendly menu** with emoji buttons for requesting, changing, viewing, or releasing a number.
- **One-hour allocation window**: numbers automatically return to the pool if unused.
- **OTP capture** from log groups or manual admin entry, sent directly to the assigned user and marked as used.
- **Admin dashboard** showing total, available, assigned, used counts, and active users.
- **List uploads** via `.txt` files (one number per line) with duplicate detection.

## Setup
1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Configure environment** (examples shown):
   ```bash
   export BOT_TOKEN="8248989965:AAFsvj6ndFO1VZGJskF7bF6GndfYPIPlJds"
   export ADMIN_IDS="5770659918,7057157722"
   export OTP_LOG_GROUPS="-1002863645312"  # optional: groups where OTPs arrive
   export ASSIGNMENT_TTL=3600               # seconds before an unused number is freed
   ```
3. **Run the bot**
   ```bash
   python app.py
   ```

## Usage
### Users
- `/start` to open the menu.
- Tap **Get Number** or **Change Number**, pick a list, and receive an assigned number.
- OTPs sent to the configured log group (or entered by admins) are delivered automatically.
- Use **Release Number** to free your slot early.

### Admins
- `/admin` — summary dashboard (totals, availability, active users).
- `/lists` — per-list breakdown.
- **Upload lists** — send a `.txt` document (one number per line) with a caption for the list name.
- `/logotp <number> <otp>` — deliver an OTP manually to the assigned user and mark the number used.

## Data
The bot stores allocations and list data in `data/store.json`. This file is created automatically on first run.

## Notes
- The bot requires poll-based access; ensure it is added to OTP log groups specified in `OTP_LOG_GROUPS`.
- Numbers are normalized to digits only (non-numeric characters are stripped) when stored and matched.
