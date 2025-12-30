"""Configuration and constants for the Todo Bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# Environment variables
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
DB_NAME = os.getenv('MONGO_DB', 'todo_bot')
DEFAULT_TZ = os.getenv('DEFAULT_TZ', 'Asia/Jakarta')
DEV_GUILDS = os.getenv('DEV_GUILDS')

# Validate critical environment variables
if not TOKEN or not MONGO_URI:
    raise ValueError('Missing DISCORD_TOKEN or MONGO_URI in environment')

# Bot configuration
COMMAND_PREFIX = '!'
INTENTS = True  # Use default intents

# Reminder thresholds in hours
REMINDER_THRESHOLDS = [72, 24, 5]

# Pagination
ITEMS_PER_PAGE = 5

# Date/Time format
DATE_FORMAT = '%Y-%m-%d %H:%M'  # For database storage and display
READABLE_DATE_FORMAT = '%A %d %B %Y'  # For user-friendly display (e.g., "Monday 30 December 2025")
