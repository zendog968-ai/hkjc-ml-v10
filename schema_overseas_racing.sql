PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS overseas_source_documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('fixture','summary','racecard','result','odds','resultsall')),
    http_status INTEGER,
    fetched_at_utc TEXT NOT NULL,
    content_sha256 TEXT,
    body_path TEXT,
    parser_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('ok','not_found','rate_limited','network_error','parse_error','http_error')),
    detail TEXT
);

CREATE TABLE IF NOT EXISTS overseas_discovery_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_code TEXT NOT NULL,
    requested_start_date TEXT NOT NULL,
    requested_end_date TEXT NOT NULL,
    fixture_url TEXT NOT NULL,
    discovered_meetings INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('complete','empty','source_unavailable','partial','rate_limited','error')),
    checked_at_utc TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS overseas_meetings (
    meeting_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT NOT NULL CHECK(meeting_date GLOB '????-??-??'),
    simulcast_code TEXT NOT NULL CHECK(simulcast_code GLOB 'S[0-9]*'),
    meeting_name TEXT,
    location TEXT,
    fixture_url TEXT NOT NULL,
    summary_url TEXT,
    fixture_document_id INTEGER,
    discovery_status TEXT NOT NULL DEFAULT 'discovered' CHECK(discovery_status IN ('discovered','race_count_verified','partial','cancelled','source_unavailable')),
    discovered_at_utc TEXT NOT NULL,
    UNIQUE(meeting_date, simulcast_code),
    FOREIGN KEY(fixture_document_id) REFERENCES overseas_source_documents(document_id)
);

CREATE TABLE IF NOT EXISTS overseas_races (
    overseas_race_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL,
    race_no INTEGER NOT NULL CHECK(race_no > 0),
    official_race_key TEXT NOT NULL UNIQUE,
    race_name TEXT,
    race_class TEXT,
    distance_m INTEGER,
    surface TEXT,
    going TEXT,
    scheduled_start_local TEXT,
    scheduled_start_utc TEXT,
    official_time TEXT,
    race_status TEXT NOT NULL DEFAULT 'discovered' CHECK(race_status IN ('discovered','completed','cancelled','void','partial','source_unavailable')),
    racecard_url TEXT,
    result_url TEXT,
    result_document_id INTEGER,
    fetched_at_utc TEXT,
    UNIQUE(meeting_id, race_no),
    FOREIGN KEY(meeting_id) REFERENCES overseas_meetings(meeting_id),
    FOREIGN KEY(result_document_id) REFERENCES overseas_source_documents(document_id)
);

CREATE TABLE IF NOT EXISTS overseas_starters (
    overseas_race_id INTEGER NOT NULL,
    horse_no INTEGER NOT NULL,
    horse_name TEXT NOT NULL,
    horse_country TEXT,
    jockey TEXT,
    trainer TEXT,
    weight_lbs REAL,
    draw INTEGER,
    career_starts INTEGER,
    career_wins INTEGER,
    career_places INTEGER,
    recent_runs_text TEXT,
    gear TEXT,
    finish_pos_text TEXT,
    finish_pos INTEGER,
    margin_text TEXT,
    margin_lengths REAL,
    finish_time TEXT,
    final_win_odds REAL,
    final_place_odds REAL,
    withdrawal_status TEXT,
    source_fields_json TEXT,
    PRIMARY KEY(overseas_race_id, horse_no),
    FOREIGN KEY(overseas_race_id) REFERENCES overseas_races(overseas_race_id)
);

CREATE TABLE IF NOT EXISTS overseas_dividends (
    overseas_race_id INTEGER NOT NULL,
    pool_name TEXT NOT NULL,
    winning_combination TEXT NOT NULL,
    dividend_hkd REAL,
    source_url TEXT NOT NULL,
    PRIMARY KEY(overseas_race_id, pool_name, winning_combination),
    FOREIGN KEY(overseas_race_id) REFERENCES overseas_races(overseas_race_id)
);

CREATE TABLE IF NOT EXISTS overseas_prerace_predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    overseas_race_id INTEGER NOT NULL,
    generated_at_utc TEXT NOT NULL,
    model_version TEXT NOT NULL,
    horse_no INTEGER NOT NULL,
    predicted_win_probability REAL NOT NULL,
    predicted_place_probability REAL NOT NULL,
    cold_start_tier TEXT NOT NULL,
    prior_source TEXT NOT NULL,
    win_odds_at_capture REAL,
    place_odds_at_capture REAL,
    win_ev REAL,
    place_ev REAL,
    kelly_fraction REAL,
    odds_snapshot_status TEXT NOT NULL,
    odds_snapshot_at_utc TEXT,
    odds_drop_flag INTEGER NOT NULL DEFAULT 0,
    source_json_path TEXT,
    UNIQUE(overseas_race_id, generated_at_utc, model_version, horse_no),
    FOREIGN KEY(overseas_race_id) REFERENCES overseas_races(overseas_race_id)
);

CREATE TABLE IF NOT EXISTS post_race_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_scope TEXT NOT NULL CHECK(audit_scope IN ('local','overseas')),
    race_key TEXT NOT NULL,
    audited_at_utc TEXT NOT NULL,
    had_prerace_prediction INTEGER NOT NULL,
    top1_hit INTEGER,
    top3_contains_winner INTEGER,
    stable_strategy_hit INTEGER,
    value_strategy_hit INTEGER,
    odds_drop_count INTEGER,
    odds_drop_winner_count INTEGER,
    settled_stake REAL,
    settled_net_return REAL,
    roi REAL,
    report_path TEXT,
    status TEXT NOT NULL CHECK(status IN ('archived_only','audited','partial','error')),
    detail_json TEXT,
    UNIQUE(audit_scope, race_key)
);

CREATE INDEX IF NOT EXISTS idx_overseas_meetings_date ON overseas_meetings(meeting_date, simulcast_code);
CREATE INDEX IF NOT EXISTS idx_overseas_races_status ON overseas_races(race_status, scheduled_start_utc);
CREATE INDEX IF NOT EXISTS idx_overseas_starters_name ON overseas_starters(horse_name);
CREATE INDEX IF NOT EXISTS idx_overseas_predictions_race ON overseas_prerace_predictions(overseas_race_id, generated_at_utc);
