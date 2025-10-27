"""Database migrations"""

import sqlite3
from pathlib import Path

from deltachat2 import Bot

DATABASE_VERSION = 2


def get_db_version(database: sqlite3.Connection) -> int:
    with database:
        database.execute(
            """CREATE TABLE IF NOT EXISTS "database" (
            "id" INTEGER NOT NULL,
	    "version" INTEGER NOT NULL,
	    PRIMARY KEY("id")
            )"""
        )
        row = database.execute("SELECT version FROM database").fetchone()
        if row:
            version = row["version"]
        else:
            database.execute("REPLACE INTO database VALUES (?,?)", (1, DATABASE_VERSION))
            version = DATABASE_VERSION
    return version


def run_migrations(bot: Bot, path: Path) -> None:
    if not path.exists():
        bot.logger.debug("Database doesn't exists, skipping migrations")
        return

    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    try:
        version = get_db_version(database)
        bot.logger.debug(f"Current database version: v{version}")
        for i in range(version + 1, DATABASE_VERSION + 1):
            migration = globals().get(f"migrate{i}")
            assert migration
            bot.logger.info(f"Migrating database: v{i}")
            with database:
                database.execute("REPLACE INTO database VALUES (?,?)", (1, i))
                migration(bot, database)
    finally:
        database.close()


def migrate1(_bot: Bot, database: sqlite3.Connection) -> None:
    try:
        database.execute("ALTER TABLE account ADD COLUMN  muted_home BOOLEAN")
    except Exception as ex:
        # ignore to avoid crash caused by accidental empty database table
        print(f"WARNING: ignoring exception: {ex}")


def migrate2(_bot: Bot, database: sqlite3.Connection) -> None:
    database.execute("ALTER TABLE account ADD COLUMN  muted_notif BOOLEAN")
