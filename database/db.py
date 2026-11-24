import sqlite3
import os

import click
from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def query_db(sql, parameters=(), one=False):
    cursor = get_db().execute(sql, parameters)
    rows = cursor.fetchall()
    cursor.close()
    return (rows[0] if rows else None) if one else rows


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    connection = get_db()
    with current_app.open_resource("database/schema.sql") as schema:
        connection.executescript(schema.read().decode("utf-8"))
    # Keep existing local databases usable after adding Places API metadata.
    migrations = {
        "trips": {
            "budget_amount": "REAL NOT NULL DEFAULT 0",
            "budget_currency": "TEXT NOT NULL DEFAULT 'EUR'",
        },
        "accommodations": {
            "google_place_id": "TEXT",
            "coordinates_updated_at": "TEXT",
        },
        "places": {
            "coordinates_updated_at": "TEXT",
            "category": "TEXT NOT NULL DEFAULT 'attraction'",
        },
    }
    for table, columns in migrations.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, column_type in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    connection.commit()


@click.command("init-db")
def init_db_command():
    init_db()
    click.echo("데이터베이스를 초기화했습니다.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    with app.app_context():
        init_db()
