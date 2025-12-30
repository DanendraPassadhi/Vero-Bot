"""Database initialization and collections management."""

from pymongo import MongoClient
from pymongo.collection import Collection
from config import MONGO_URI, DB_NAME


# MongoDB connection
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
tasks_col: Collection = db['tasks']
events_col: Collection = db['events']
users_col: Collection = db['user_settings']
guilds_col: Collection = db['guild_settings']


def get_all_collections():
    """Return tuple of all collections for easy access."""
    return tasks_col, events_col, users_col, guilds_col
