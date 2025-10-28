"""Database migrations"""

import sqlite3
from pathlib import Path

from deltachat2 import Bot

DATABASE_VERSION = 3


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


def migrate3(bot: Bot, database: sqlite3.Connection) -> None:
    accid = bot.rpc.get_all_account_ids()[0]
    with database:
        database.execute("ALTER TABLE account RENAME TO old_account")
        database.execute(
            """
            CREATE TABLE account (
                    id INTEGER PRIMARY KEY,
                    user VARCHAR(1000) NOT NULL,
                    url VARCHAR(1000) NOT NULL,
                    token VARCHAR(1000) NOT NULL,
                    home INTEGER NOT NULL,
                    notifications INTEGER NOT NULL,
                    last_home VARCHAR(1000),
                    last_notif VARCHAR(1000),
                    muted_home BOOLEAN,
                    muted_notif BOOLEAN
            )
            """
        )
        for row in database.execute("SELECT * FROM old_account"):
            addr = row["addr"]
            conid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
            database.execute(
                (
                    "INSERT INTO account (id, user, url, token, home, notifications,"
                    " last_home, last_notif, muted_home, muted_notif)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    conid,
                    row["user"],
                    row["url"],
                    row["token"],
                    row["home"],
                    row["notifications"],
                    row["last_home"],
                    row["last_notif"],
                    row["muted_home"],
                    row["muted_notif"],
                ),
            )
        database.execute("DROP TABLE old_account")

        database.execute("ALTER TABLE dmchat RENAME TO old_dmchat")
        database.execute(
            """
            CREATE TABLE dmchat (
                    chat_id INTEGER PRIMARY KEY,
                    contact VARCHAR(1000) NOT NULL,
                    contactid INTEGER NOT NULL,
                    Foreign KEY(contactid) REFERENCES account (id)
            )
            """
        )
        for row in database.execute("SELECT * FROM old_dmchat"):
            addr = row["acc_addr"]
            conid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
            database.execute(
                """
                INSERT INTO dmchat (chat_id, contactid, contact)
                VALUES (?, ?, ?)
                """,
                (row["chat_id"], conid, row["contact"]),
            )
        database.execute("DROP TABLE old_dmchat")

        database.execute("ALTER TABLE oauth RENAME TO old_oauth")
        database.execute(
            """
            CREATE TABLE oauth (
                    id INTEGER PRIMARY KEY,
                    url VARCHAR(1000) NOT NULL,
                    user VARCHAR(1000),
                    client_id VARCHAR(1000) NOT NULL,
                    client_secret VARCHAR(1000) NOT NULL
            )
            """
        )
        for row in database.execute("SELECT * FROM old_oauth"):
            addr = row["addr"]
            conid = bot.rpc.lookup_contact_id_by_addr(accid, addr)
            database.execute(
                """
                INSERT INTO oauth (id, url, user, client_id, client_secret)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conid, row["url"], row["user"], row["client_id"], row["client_secret"]),
            )
        database.execute("DROP TABLE old_oauth")
