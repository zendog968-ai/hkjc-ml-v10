PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_manifests (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('hkjc_pp_list', 'hkjc_pp_form', 'hkjc_barrier_trial')),
    source_url TEXT NOT NULL,
    retrieved_at_utc TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    local_relative_path TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pp_identity_map (
    pp_identity_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_manifests(source_id),
    hk_horse_code TEXT NOT NULL,
    hk_horse_name TEXT NOT NULL,
    original_horse_name TEXT NOT NULL,
    source_country TEXT NOT NULL,
    official_anchor_match INTEGER NOT NULL CHECK(official_anchor_match IN (0,1)),
    identity_confidence REAL NOT NULL CHECK(identity_confidence >= 0 AND identity_confidence <= 1),
    status TEXT NOT NULL CHECK(status IN ('accepted_fixture', 'rejected_ambiguous')),
    created_at_utc TEXT NOT NULL,
    UNIQUE(hk_horse_code, source_id)
);

CREATE TABLE IF NOT EXISTS pp_external_form (
    pp_form_id TEXT PRIMARY KEY,
    pp_identity_id TEXT NOT NULL REFERENCES pp_identity_map(pp_identity_id),
    form_date TEXT,
    source_country TEXT,
    race_distance_m INTEGER,
    finishing_position INTEGER,
    field_size INTEGER,
    race_class_text TEXT,
    source_rating_raw REAL,
    source_rating_name TEXT,
    source_sectionals_available INTEGER NOT NULL CHECK(source_sectionals_available IN (0,1)),
    source_record_sha256 TEXT NOT NULL CHECK(length(source_record_sha256) = 64),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_batches (
    trial_batch_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_manifests(source_id),
    trial_date TEXT NOT NULL,
    venue TEXT NOT NULL,
    surface TEXT NOT NULL,
    distance_m INTEGER NOT NULL,
    going TEXT NOT NULL,
    batch_label TEXT NOT NULL,
    batch_time_seconds REAL NOT NULL,
    sectional_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(trial_date, venue, batch_label)
);

CREATE TABLE IF NOT EXISTS trial_entries (
    trial_entry_id TEXT PRIMARY KEY,
    trial_batch_id TEXT NOT NULL REFERENCES trial_batches(trial_batch_id),
    horse_code TEXT NOT NULL,
    horse_name TEXT NOT NULL,
    jockey TEXT,
    trainer TEXT,
    draw INTEGER,
    gear TEXT,
    lbw REAL,
    running_position_json TEXT NOT NULL,
    finish_rank INTEGER NOT NULL,
    finish_time_seconds REAL NOT NULL,
    time_vs_batch_seconds REAL NOT NULL,
    final_sectional_vs_batch_seconds REAL,
    finish_rank_pct REAL NOT NULL,
    position_gain INTEGER,
    comment TEXT,
    created_at_utc TEXT NOT NULL,
    UNIQUE(trial_batch_id, horse_code)
);

CREATE TABLE IF NOT EXISTS candidate_feature_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    candidate_schema_version TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('pp', 'trial')),
    subject_key TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    availability_json TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL CHECK(length(source_manifest_sha256) = 64),
    snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE(candidate_schema_version, subject_type, subject_key, as_of_utc)
);

CREATE TRIGGER IF NOT EXISTS source_manifests_no_update
BEFORE UPDATE ON source_manifests
BEGIN
    SELECT RAISE(ABORT, 'source_manifests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS source_manifests_no_delete
BEFORE DELETE ON source_manifests
BEGIN
    SELECT RAISE(ABORT, 'source_manifests are immutable');
END;

CREATE TRIGGER IF NOT EXISTS candidate_feature_snapshots_no_update
BEFORE UPDATE ON candidate_feature_snapshots
BEGIN
    SELECT RAISE(ABORT, 'candidate feature snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS candidate_feature_snapshots_no_delete
BEFORE DELETE ON candidate_feature_snapshots
BEGIN
    SELECT RAISE(ABORT, 'candidate feature snapshots are immutable');
END;

CREATE INDEX IF NOT EXISTS idx_pp_identity_hk_code ON pp_identity_map(hk_horse_code);
CREATE INDEX IF NOT EXISTS idx_trial_entries_horse_code ON trial_entries(horse_code);
CREATE INDEX IF NOT EXISTS idx_snapshot_subject ON candidate_feature_snapshots(subject_type, subject_key);
