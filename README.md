# BOtOtt

Telegram OTP forwarding bot built with `python-telegram-bot` v20+. It manages a pool of phone numbers, assigns them per-user, polls external OTP panels, forwards OTPs to users and log groups, and provides admin tooling for number stock.

## Quick start
1. Install requirements (Python 3.10+):
   ```bash
   python -m pip install -r requirements.txt
   ```
2. Create `config.json` or set environment variables:
   ```json
   {
     "bot_token": "123:ABC",
     "admin_ids": [123456789],
     "group_ids_log": [-100123456789],
     "panels": [
       {"name": "cr", "url": "https://example/api", "token": "secret", "records": 10}
     ]
   }
   ```
3. Provide an initial `data/numbers.json` (see header of `bot.py` for full example) and run:
   ```bash
   python bot.py
   ```

Commands: `/start`, `/getnumber`, `/changenumber`, `/mystatus`, `/help`, admin `/admin`, `/stock`, `/activeusers`, `/addnumbers`, `/deletenumber`, `/releasenumber`, `/broadcast`.
