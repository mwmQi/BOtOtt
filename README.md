# BOtOtt

## Configuration

Create a `.env` file (you can copy `.env.example`) with the following variables:

- `BOT_TOKEN`: Telegram bot token.
- `GROUP_IDS`: Comma-separated chat or group IDs for broadcasting.
- `CR_API_TOKEN`: Token for the CR API panel.
- `CR_API_URL`: CR API endpoint (defaults to the value in `.env.example` if unset).
- `CR_API_RECORDS`: Number of CR API records to request (defaults to 20).
- `MAIT_API_TOKEN`: Token for the MAIT API panel.
- `MAIT_API_URL`: MAIT API endpoint (defaults to the value in `.env.example` if unset).
- `MAIT_API_RECORDS`: Number of MAIT API records to request (defaults to 20).

The application uses [python-dotenv](https://pypi.org/project/python-dotenv/) to load variables from the `.env` file at startup and will exit if required values are missing.
