PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS deep_scrape_runs (
    scrape_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT NOT NULL,
    simulcast_code TEXT NOT NULL CHECK (simulcast_code GLOB 'S[0-9]*'),
    race_no INTEGER NOT NULL CHECK (race_no > 0),
    venue TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    n6_status TEXT NOT NULL CHECK (n6_status = 'disabled_non_hk'),
    fetched_at_utc TEXT NOT NULL,
    racing_post_url TEXT,
    at_the_races_url TEXT,
    timeform_url TEXT,
    hkjc_odds_source TEXT,
    source_notes TEXT NOT NULL,
    UNIQUE(meeting_date, simulcast_code, race_no, fetched_at_utc)
);

CREATE TABLE IF NOT EXISTS s1_races (
    s1_race_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_run_id INTEGER NOT NULL REFERENCES deep_scrape_runs(scrape_run_id) ON DELETE CASCADE,
    meeting_date TEXT NOT NULL,
    simulcast_code TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    venue TEXT NOT NULL,
    local_start_time TEXT,
    hkt_start_time TEXT,
    race_name TEXT,
    distance_text TEXT,
    surface TEXT,
    going TEXT,
    race_class TEXT,
    declared_runners INTEGER,
    source_status TEXT NOT NULL CHECK (source_status IN ('complete', 'partial', 'degraded')),
    UNIQUE(scrape_run_id, meeting_date, simulcast_code, race_no)
);

CREATE TABLE IF NOT EXISTS s1_starters (
    s1_starter_id INTEGER PRIMARY KEY AUTOINCREMENT,
    s1_race_id INTEGER NOT NULL REFERENCES s1_races(s1_race_id) ON DELETE CASCADE,
    runner_no INTEGER NOT NULL CHECK (runner_no > 0),
    draw_no INTEGER,
    horse_name TEXT NOT NULL,
    country_code TEXT,
    age INTEGER,
    carried_weight_text TEXT,
    jockey_name TEXT,
    trainer_name TEXT,
    official_rating INTEGER,
    racing_post_rating INTEGER,
    top_speed_rating INTEGER,
    at_the_races_rating INTEGER,
    sire TEXT,
    dam TEXT,
    damsire TEXT,
    pace_hint TEXT,
    distance_runs INTEGER,
    distance_wins INTEGER,
    similar_going_runs INTEGER,
    similar_going_wins INTEGER,
    course_runs INTEGER,
    course_wins INTEGER,
    jockey_york_runs INTEGER,
    jockey_york_wins INTEGER,
    trainer_york_runs INTEGER,
    trainer_york_wins INTEGER,
    hkjc_win_odds REAL,
    hkjc_place_odds REAL,
    deep_composite_score REAL,
    deep_rank INTEGER,
    data_completeness TEXT NOT NULL,
    source_rpr_ts_url TEXT,
    source_form_url TEXT,
    source_hkjc_odds_url TEXT,
    UNIQUE(s1_race_id, runner_no)
);

CREATE INDEX IF NOT EXISTS idx_s1_races_lookup ON s1_races(meeting_date, simulcast_code, race_no, venue);
CREATE INDEX IF NOT EXISTS idx_s1_starters_race ON s1_starters(s1_race_id, deep_rank);

CREATE TABLE IF NOT EXISTS s1_source_field_status (
    source_field_status_id INTEGER PRIMARY KEY AUTOINCREMENT,
    s1_starter_id INTEGER NOT NULL REFERENCES s1_starters(s1_starter_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    availability TEXT NOT NULL CHECK (availability IN ('available_public', 'unavailable_paid_or_restricted', 'unavailable_parse', 'not_requested')),
    source_url TEXT,
    captured_at_utc TEXT NOT NULL,
    UNIQUE(s1_starter_id, field_name)
);
