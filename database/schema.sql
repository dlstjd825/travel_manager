PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    city TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    budget_amount REAL NOT NULL DEFAULT 0 CHECK (budget_amount >= 0),
    budget_currency TEXT NOT NULL DEFAULT 'EUR',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS accommodations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL UNIQUE REFERENCES trips(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    google_place_id TEXT,
    coordinates_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    provider_place_id TEXT,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    category TEXT NOT NULL DEFAULT 'attraction' CHECK (category IN ('attraction', 'restaurant')),
    stay_duration INTEGER NOT NULL DEFAULT 60 CHECK (stay_duration > 0),
    coordinates_updated_at TEXT,
    UNIQUE (trip_id, provider_place_id)
);

CREATE TABLE IF NOT EXISTS itinerary_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day_number INTEGER NOT NULL CHECK (day_number > 0),
    date TEXT NOT NULL,
    UNIQUE (trip_id, day_number)
);

CREATE TABLE IF NOT EXISTS itinerary_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id INTEGER NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
    place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    start_time TEXT,
    is_locked INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
    UNIQUE (day_id, sequence),
    UNIQUE (day_id, place_id)
);

CREATE INDEX IF NOT EXISTS idx_places_trip_id ON places(trip_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_days_trip_id ON itinerary_days(trip_id);

CREATE TABLE IF NOT EXISTS trip_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '기타',
    expense_date TEXT NOT NULL,
    amount REAL NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL DEFAULT 'EUR',
    note TEXT NOT NULL DEFAULT '',
    original_file_name TEXT,
    stored_file_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trip_expenses_trip_date ON trip_expenses(trip_id, expense_date, id);
