-- V10.2 auditable archive for pre-race HKJC odds snapshots.
-- This schema stores raw payloads and runner-level prices separately.  It must not
-- be used to label final/closing odds as T_MINUS_15 or T_MINUS_5.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pre_race_odds_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL,
    snapshot_label TEXT NOT NULL CHECK (snapshot_label IN ('T_MINUS_15', 'T_MINUS_5')),
    race_date TEXT NOT NULL,                 -- ISO YYYY-MM-DD
    racecourse TEXT NOT NULL CHECK (racecourse IN ('ST', 'HV')),
    race_no INTEGER NOT NULL CHECK (race_no > 0),
    captured_at_utc TEXT NOT NULL,           -- ISO-8601 UTC, from the capture process
    status TEXT NOT NULL CHECK (status = 'complete'),
    source_url TEXT,
    source_mode TEXT,
    source_file_path TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL UNIQUE,
    raw_payload_json TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL,
    UNIQUE (snapshot_label, race_date, racecourse, race_no, captured_at_utc)
);

CREATE INDEX IF NOT EXISTS idx_prerace_snapshot_race_label
    ON pre_race_odds_snapshots (race_date, racecourse, race_no, snapshot_label);

CREATE TABLE IF NOT EXISTS pre_race_odds_runner_prices (
    snapshot_id INTEGER NOT NULL,
    horse_name TEXT NOT NULL,
    win_odds REAL,
    place_odds REAL,
    PRIMARY KEY (snapshot_id, horse_name),
    FOREIGN KEY (snapshot_id) REFERENCES pre_race_odds_snapshots(snapshot_id) ON DELETE CASCADE,
    CHECK (win_odds IS NULL OR win_odds > 1.0),
    CHECK (place_odds IS NULL OR place_odds > 1.0)
);

CREATE INDEX IF NOT EXISTS idx_prerace_price_horse
    ON pre_race_odds_runner_prices (horse_name);

CREATE VIEW IF NOT EXISTS v_pre_race_odds_complete AS
SELECT
    snapshot_id, snapshot_label, race_date, racecourse, race_no,
    captured_at_utc, horse_name, win_odds, place_odds
FROM pre_race_odds_snapshots
JOIN pre_race_odds_runner_prices USING (snapshot_id)
WHERE status = 'complete';
