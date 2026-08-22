PRAGMA foreign_keys = ON;

-- P1 is a separate candidate database. It must never share write paths with P0
-- fixtures or the V10/N6 production stores.
CREATE TABLE IF NOT EXISTS p1_ingest_runs (
    ingest_run_id TEXT PRIMARY KEY,
    parser_version TEXT NOT NULL,
    candidate_schema_version TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('offline_archive_only', 'manual_capture_then_offline_parse')),
    as_of_utc TEXT NOT NULL,
    pp_source_count INTEGER NOT NULL CHECK(pp_source_count >= 0),
    trial_source_count INTEGER NOT NULL CHECK(trial_source_count >= 0),
    status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected_time_gate', 'rejected_source', 'partial')),
    run_sha256 TEXT NOT NULL CHECK(length(run_sha256) = 64),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_manifests (
    source_id TEXT PRIMARY KEY,
    ingest_run_id TEXT NOT NULL REFERENCES p1_ingest_runs(ingest_run_id),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('hkjc_pp_list', 'hkjc_pp_form', 'hkjc_barrier_trial')),
    source_url TEXT NOT NULL,
    retrieved_at_utc TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    local_relative_path TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(source_kind, source_url, content_sha256, parser_version)
);

CREATE TABLE IF NOT EXISTS pp_identity_map (
    pp_identity_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_manifests(source_id),
    hk_horse_code TEXT NOT NULL,
    hk_horse_name TEXT NOT NULL,
    original_horse_name TEXT NOT NULL,
    source_country TEXT NOT NULL,
    official_anchor_match INTEGER NOT NULL CHECK(official_anchor_match IN (0, 1)),
    identity_confidence REAL NOT NULL CHECK(identity_confidence >= 0 AND identity_confidence <= 1),
    parse_status TEXT NOT NULL CHECK(parse_status IN ('accepted_p1', 'rejected_ambiguous', 'incomplete_official_row')),
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
    source_sectionals_available INTEGER NOT NULL CHECK(source_sectionals_available IN (0, 1)),
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
    UNIQUE(source_id, batch_label)
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
    lbw_raw TEXT,
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

CREATE TRIGGER IF NOT EXISTS p1_ingest_runs_no_update BEFORE UPDATE ON p1_ingest_runs
BEGIN SELECT RAISE(ABORT, 'p1 ingest runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS p1_ingest_runs_no_delete BEFORE DELETE ON p1_ingest_runs
BEGIN SELECT RAISE(ABORT, 'p1 ingest runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS p1_source_manifests_no_update BEFORE UPDATE ON source_manifests
BEGIN SELECT RAISE(ABORT, 'p1 source manifests are immutable'); END;
CREATE TRIGGER IF NOT EXISTS p1_source_manifests_no_delete BEFORE DELETE ON source_manifests
BEGIN SELECT RAISE(ABORT, 'p1 source manifests are immutable'); END;
CREATE TRIGGER IF NOT EXISTS p1_candidate_snapshots_no_update BEFORE UPDATE ON candidate_feature_snapshots
BEGIN SELECT RAISE(ABORT, 'p1 candidate snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS p1_candidate_snapshots_no_delete BEFORE DELETE ON candidate_feature_snapshots
BEGIN SELECT RAISE(ABORT, 'p1 candidate snapshots are immutable'); END;

CREATE INDEX IF NOT EXISTS idx_p1_pp_identity_code ON pp_identity_map(hk_horse_code);
CREATE INDEX IF NOT EXISTS idx_p1_trial_entries_code ON trial_entries(horse_code);
CREATE INDEX IF NOT EXISTS idx_p1_trial_entries_trainer ON trial_entries(trainer);
CREATE INDEX IF NOT EXISTS idx_p1_snapshots_subject ON candidate_feature_snapshots(subject_type, subject_key);

CREATE TABLE IF NOT EXISTS ingest_rejections (
    rejection_id TEXT PRIMARY KEY,
    ingest_run_id TEXT NOT NULL REFERENCES p1_ingest_runs(ingest_run_id),
    source_id TEXT REFERENCES source_manifests(source_id),
    entity_kind TEXT NOT NULL CHECK(entity_kind IN ('pp_identity', 'pp_form', 'trial_batch', 'trial_entry', 'source_manifest')),
    entity_anchor TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(ingest_run_id, entity_kind, entity_anchor, reason_code)
);
CREATE TRIGGER IF NOT EXISTS p1_ingest_rejections_no_update BEFORE UPDATE ON ingest_rejections
BEGIN SELECT RAISE(ABORT, 'p1 ingest rejections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS p1_ingest_rejections_no_delete BEFORE DELETE ON ingest_rejections
BEGIN SELECT RAISE(ABORT, 'p1 ingest rejections are immutable'); END;
CREATE INDEX IF NOT EXISTS idx_p1_rejections_run ON ingest_rejections(ingest_run_id, entity_kind);
