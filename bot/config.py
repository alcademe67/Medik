"""Central place for every setting the bot reads from the environment.

Secrets (bot token, API keys) never live in code — they come from real
environment variables or a local .env file (see .env.example).
"""

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# The external API the bot talks to. openFDA works without a key;
# setting one just raises your rate limit (https://open.fda.gov/apis/authentication/).
OPENFDA_BASE_URL = os.getenv("OPENFDA_BASE_URL", "https://api.fda.gov")
OPENFDA_API_KEY = os.getenv("OPENFDA_API_KEY", "")

REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
