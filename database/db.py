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
        "users": {
            "username": "TEXT",
        },
        "trips": {
            "budget_amount": "REAL NOT NULL DEFAULT 0",
            "budget_currency": "TEXT NOT NULL DEFAULT 'EUR'",
            "user_id": "INTEGER REFERENCES users(id) ON DELETE CASCADE",
        },
        "accommodations": {
            "google_place_id": "TEXT",
            "coordinates_updated_at": "TEXT",
            "check_in_date": "TEXT",
            "check_out_date": "TEXT",
            "check_in_time": "TEXT NOT NULL DEFAULT '15:00'",
            "check_out_time": "TEXT NOT NULL DEFAULT '11:00'",
            "region_id": "INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL",
        },
        "places": {
            "coordinates_updated_at": "TEXT",
            "category": "TEXT NOT NULL DEFAULT 'attraction'",
            "region_id": "INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL",
        },
        "itinerary_items": {
            "region_id": "INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL",
        },
        "itinerary_transfers": {
            "transport_mode": "TEXT NOT NULL DEFAULT 'unselected'",
        },
        "trip_expenses": {
            "place_id": "INTEGER REFERENCES places(id) ON DELETE SET NULL",
        },
        "trip_recommendation_settings": {
            "bus_buffer_hours": "REAL NOT NULL DEFAULT 2",
        },
    }
    for table, columns in migrations.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, column_type in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    transport_columns = {row["name"] for row in connection.execute("PRAGMA table_info(trip_transport_legs)")}
    transport_schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trip_transport_legs'"
    ).fetchone()["sql"].lower()
    if "leg_type" in transport_columns or "'bus'" not in transport_schema:
        connection.execute(
            """CREATE TABLE trip_transport_legs_new (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                   mode TEXT NOT NULL CHECK (mode IN ('flight', 'train', 'bus')),
                   carrier TEXT NOT NULL DEFAULT '',
                   transport_number TEXT NOT NULL DEFAULT '',
                   origin TEXT NOT NULL,
                   destination TEXT NOT NULL,
                   departure_at TEXT NOT NULL,
                   arrival_at TEXT NOT NULL,
                   original_file_name TEXT,
                   stored_file_name TEXT,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        connection.execute(
            """INSERT INTO trip_transport_legs_new
               (id, trip_id, mode, carrier, transport_number, origin, destination,
                departure_at, arrival_at, original_file_name, stored_file_name, created_at)
               SELECT id, trip_id, mode, carrier, transport_number, origin, destination,
                      departure_at, arrival_at, original_file_name, stored_file_name, created_at
               FROM trip_transport_legs"""
        )
        connection.execute("DROP TABLE trip_transport_legs")
        connection.execute("ALTER TABLE trip_transport_legs_new RENAME TO trip_transport_legs")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_transport_legs_trip ON trip_transport_legs(trip_id, departure_at)")
    connection.execute(
        """INSERT OR IGNORE INTO trip_members (trip_id, user_id, role)
           SELECT id, user_id, 'owner' FROM trips WHERE user_id IS NOT NULL"""
    )
    trip_rows = connection.execute(
        "SELECT id, city, start_date, end_date FROM trips ORDER BY id"
    ).fetchall()
    trips_by_city = {}
    members_by_trip = {}
    for trip in trip_rows:
        trips_by_city.setdefault((trip["city"] or "").strip().casefold(), []).append(trip)
    for membership in connection.execute("SELECT trip_id, user_id FROM trip_members"):
        members_by_trip.setdefault(membership["trip_id"], set()).add(membership["user_id"])
    for merged_trip in trip_rows:
        components = [part.strip() for part in (merged_trip["city"] or "").split(" · ") if part.strip()]
        if len(components) < 2:
            continue
        if connection.execute(
            "SELECT 1 FROM merged_trip_sources WHERE merged_trip_id = ? LIMIT 1",
            (merged_trip["id"],),
        ).fetchone():
            continue
        parent_members = members_by_trip.get(merged_trip["id"], set())
        inferred_sources = []
        for order, component in enumerate(components):
            candidates = [
                candidate for candidate in trips_by_city.get(component.casefold(), [])
                if candidate["id"] != merged_trip["id"]
                and merged_trip["start_date"] <= candidate["start_date"]
                and candidate["end_date"] <= merged_trip["end_date"]
                and parent_members.intersection(members_by_trip.get(candidate["id"], set()))
            ]
            if len(candidates) != 1:
                inferred_sources = []
                break
            inferred_sources.append((merged_trip["id"], candidates[0]["id"], order))
        if len(inferred_sources) >= 2:
            connection.executemany(
                "INSERT OR IGNORE INTO merged_trip_sources (merged_trip_id, source_trip_id, sort_order) VALUES (?, ?, ?)",
                inferred_sources,
            )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_places_region ON places(trip_id, region_id)")
    connection.execute("INSERT OR IGNORE INTO trip_revisions (trip_id) SELECT id FROM trips")
    for table in ("trips", "accommodations", "places", "itinerary_days", "itinerary_transfers", "itinerary_event_order", "trip_transport_legs", "trip_expenses", "trip_todos", "trip_regions", "trip_recommendation_settings", "place_journals", "place_photos"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            ref = "NEW" if operation != "DELETE" else "OLD"
            trip_id_expr = f"{ref}.id" if table == "trips" else f"{ref}.trip_id"
            trigger_name = f"rev_{table}_{operation.lower()}"
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {trigger_name} AFTER {operation} ON {table}
                BEGIN
                  INSERT INTO trip_revisions (trip_id, revision, updated_at)
                    SELECT {trip_id_expr}, 1, CURRENT_TIMESTAMP WHERE EXISTS (SELECT 1 FROM trips WHERE id = {trip_id_expr})
                    ON CONFLICT(trip_id) DO UPDATE SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP;
                END""")
    for operation in ("INSERT", "UPDATE", "DELETE"):
        ref = "OLD" if operation == "DELETE" else "NEW"
        trigger = f"rev_itinerary_items_{operation.lower()}"
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {operation} ON itinerary_items
            BEGIN
              INSERT INTO trip_revisions (trip_id, revision, updated_at)
              SELECT d.trip_id, 1, CURRENT_TIMESTAMP FROM itinerary_days d WHERE d.id = {ref}.day_id
              ON CONFLICT(trip_id) DO UPDATE SET revision=revision+1, updated_at=CURRENT_TIMESTAMP;
            END""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_itinerary_items_region ON itinerary_items(region_id, day_id)")
    accommodation_indexes = connection.execute("PRAGMA index_list(accommodations)").fetchall()
    has_trip_unique = False
    for index in accommodation_indexes:
        if index["unique"]:
            index_columns = [
                row["name"] for row in connection.execute(f"PRAGMA index_info('{index['name']}')")
            ]
            if index_columns == ["trip_id"]:
                has_trip_unique = True
                break
    if has_trip_unique:
        connection.execute(
            """CREATE TABLE accommodations_multi (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                   name TEXT NOT NULL,
                   address TEXT NOT NULL DEFAULT '',
                   check_in_date TEXT,
                   check_out_date TEXT,
                   check_in_time TEXT NOT NULL DEFAULT '15:00',
                   check_out_time TEXT NOT NULL DEFAULT '11:00',
                   latitude REAL,
                   longitude REAL,
                   google_place_id TEXT,
                   coordinates_updated_at TEXT,
                   region_id INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL
               )"""
        )
        connection.execute(
            """INSERT INTO accommodations_multi
               (id, trip_id, name, address, check_in_date, check_out_date, check_in_time, check_out_time, latitude, longitude,
                google_place_id, coordinates_updated_at, region_id)
               SELECT id, trip_id, name, address, check_in_date, check_out_date, check_in_time, check_out_time, latitude, longitude,
                      google_place_id, coordinates_updated_at, region_id
               FROM accommodations"""
        )
        connection.execute("DROP TABLE accommodations")
        connection.execute("ALTER TABLE accommodations_multi RENAME TO accommodations")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_accommodations_trip_dates ON accommodations(trip_id, check_in_date, check_out_date)"
    )
    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    if "email" in user_columns:
        used_usernames = set()
        for user in connection.execute("SELECT id, email, username FROM users ORDER BY id"):
            if user["username"]:
                used_usernames.add(user["username"].casefold())
                continue
            base_name = (user["email"] or "traveler").split("@", 1)[0][:24] or "traveler"
            username = base_name
            suffix = 1
            while username.casefold() in used_usernames:
                tail = f"{suffix}"
                username = f"{base_name[:24-len(tail)]}{tail}"
                suffix += 1
            connection.execute("UPDATE users SET username = ? WHERE id = ?", (username, user["id"]))
            used_usernames.add(username.casefold())
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)")
    connection.execute(
        """INSERT OR IGNORE INTO trip_members (trip_id, user_id, role)
           SELECT id, user_id, 'owner' FROM trips WHERE user_id IS NOT NULL"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trip_expenses_place ON trip_expenses(place_id, expense_date)"
    )
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
