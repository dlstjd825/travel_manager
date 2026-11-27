PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS login_attempts (
    client_key TEXT PRIMARY KEY,
    attempt_count INTEGER NOT NULL,
    window_started_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    city TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    budget_amount REAL NOT NULL DEFAULT 0 CHECK (budget_amount >= 0),
    budget_currency TEXT NOT NULL DEFAULT 'EUR',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS trip_revisions (
    trip_id INTEGER PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trip_members (
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'editor')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trip_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_members_user ON trip_members(user_id, trip_id);

CREATE TABLE IF NOT EXISTS merged_trip_sources (
    merged_trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    source_trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (merged_trip_id, source_trip_id),
    CHECK (merged_trip_id <> source_trip_id)
);
CREATE INDEX IF NOT EXISTS idx_merged_trip_sources_source ON merged_trip_sources(source_trip_id, merged_trip_id);

CREATE TABLE IF NOT EXISTS trip_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    invited_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invited_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'declined')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (trip_id, invited_user_id)
);
CREATE INDEX IF NOT EXISTS idx_trip_invitations_user ON trip_invitations(invited_user_id, status);

CREATE TABLE IF NOT EXISTS user_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_notifications_user ON user_notifications(user_id, is_read, id);

CREATE TABLE IF NOT EXISTS accommodations (
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
);
CREATE INDEX IF NOT EXISTS idx_accommodations_trip_dates ON accommodations(trip_id, check_in_date, check_out_date);

CREATE TABLE IF NOT EXISTS trip_regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (trip_id, label)
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
    region_id INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL,
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
    region_id INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL,
    UNIQUE (day_id, sequence),
    UNIQUE (day_id, place_id)
);

CREATE TABLE IF NOT EXISTS itinerary_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day_id INTEGER NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
    before_item_id INTEGER UNIQUE REFERENCES itinerary_items(id) ON DELETE SET NULL,
    from_region_id INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL,
    to_region_id INTEGER REFERENCES trip_regions(id) ON DELETE SET NULL,
    transport_mode TEXT NOT NULL DEFAULT 'unselected' CHECK (transport_mode IN ('unselected', 'flight', 'train', 'bus', 'car', 'ferry', 'other')),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS itinerary_event_order (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    day_id INTEGER NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
    region_id INTEGER REFERENCES trip_regions(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0)
);
CREATE INDEX IF NOT EXISTS idx_itinerary_event_order_day ON itinerary_event_order(day_id, region_id, sequence);

CREATE INDEX IF NOT EXISTS idx_places_trip_id ON places(trip_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_days_trip_id ON itinerary_days(trip_id);

CREATE TABLE IF NOT EXISTS trip_transport_legs (
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
);
CREATE INDEX IF NOT EXISTS idx_transport_legs_trip ON trip_transport_legs(trip_id, departure_at);

CREATE TABLE IF NOT EXISTS trip_recommendation_settings (
    trip_id INTEGER PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,
    flight_buffer_hours REAL NOT NULL DEFAULT 4,
    train_buffer_hours REAL NOT NULL DEFAULT 2,
    bus_buffer_hours REAL NOT NULL DEFAULT 2,
    day_start_time TEXT NOT NULL DEFAULT '08:00',
    day_end_time TEXT NOT NULL DEFAULT '20:00',
    arrival_latest_start TEXT NOT NULL DEFAULT '18:00',
    departure_earliest_cutoff TEXT NOT NULL DEFAULT '12:00'
);

CREATE TABLE IF NOT EXISTS trip_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    place_id INTEGER REFERENCES places(id) ON DELETE SET NULL,
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

CREATE TABLE IF NOT EXISTS trip_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    is_done INTEGER NOT NULL DEFAULT 0 CHECK (is_done IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trip_todos_trip ON trip_todos(trip_id, is_done, id);

CREATE TABLE IF NOT EXISTS place_journals (
    place_id INTEGER PRIMARY KEY REFERENCES places(id) ON DELETE CASCADE,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    information TEXT NOT NULL DEFAULT '',
    thoughts TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS place_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    stored_file_name TEXT NOT NULL UNIQUE,
    original_file_name TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_place_photos_place ON place_photos(place_id, id);
