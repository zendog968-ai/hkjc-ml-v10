-- V10.2 extension: auditable complex-pool snapshots for HKJC pari-mutuel pools.
-- Apply AFTER schema_prerace_odds_snapshots.sql.  This schema deliberately separates
-- a pool event, its legs, observed pre-race market data, quoted selections, and
-- post-race official payouts.  Do NOT label a final dividend as a T-15/T-5 quote.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pre_race_pool_events (
    pool_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT NOT NULL,                  -- ISO YYYY-MM-DD
    meeting_racecourse TEXT NOT NULL CHECK (meeting_racecourse IN ('ST', 'HV')),
    pool_type TEXT NOT NULL CHECK (pool_type IN (
        'TRIFECTA_ORDERED', 'TRIO_UNORDERED',
        'QUARTET_ORDERED', 'FIRST_4_UNORDERED', 'QUARTET_FIRST_4_COMBINED',
        'DOUBLE_TRIO', 'SIX_UP'
    )),
    pool_event_code TEXT NOT NULL,                -- official pool/meeting identifier if published
    expected_leg_count INTEGER NOT NULL CHECK (expected_leg_count BETWEEN 1 AND 6),
    source_url TEXT,
    announced_at_utc TEXT,
    UNIQUE (meeting_date, meeting_racecourse, pool_type, pool_event_code)
);

CREATE TABLE IF NOT EXISTS pre_race_pool_event_legs (
    pool_event_id INTEGER NOT NULL,
    leg_no INTEGER NOT NULL CHECK (leg_no BETWEEN 1 AND 6),
    race_date TEXT NOT NULL,
    racecourse TEXT NOT NULL CHECK (racecourse IN ('ST', 'HV')),
    race_no INTEGER NOT NULL CHECK (race_no > 0),
    scheduled_start_utc TEXT,                  -- preserve announced start for timing audit
    PRIMARY KEY (pool_event_id, leg_no),
    UNIQUE (pool_event_id, race_date, racecourse, race_no),
    FOREIGN KEY (pool_event_id) REFERENCES pre_race_pool_events(pool_event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pre_race_pool_snapshots (
    pool_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_event_id INTEGER NOT NULL,
    snapshot_label TEXT NOT NULL CHECK (snapshot_label IN ('T_MINUS_15', 'T_MINUS_5')),
    captured_at_utc TEXT NOT NULL,
    anchor_leg_no INTEGER NOT NULL DEFAULT 1 CHECK (anchor_leg_no BETWEEN 1 AND 6),
    scheduled_anchor_start_utc TEXT,           -- planned start of the leg defining T-15/T-5
    capture_delta_seconds INTEGER,              -- actual captured_at minus target offset time
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial')),
    quote_completeness TEXT NOT NULL CHECK (quote_completeness IN ('full', 'market_summary_only', 'partial', 'unavailable')),
    gross_pool_amount REAL CHECK (gross_pool_amount IS NULL OR gross_pool_amount >= 0),
    carryover_amount REAL CHECK (carryover_amount IS NULL OR carryover_amount >= 0),
    source_url TEXT,
    source_mode TEXT,
    source_file_path TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL UNIQUE,
    raw_payload_json TEXT NOT NULL,
    imported_at_utc TEXT NOT NULL,
    UNIQUE (pool_event_id, snapshot_label, captured_at_utc),
    FOREIGN KEY (pool_event_id) REFERENCES pre_race_pool_events(pool_event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_complex_pool_snapshot_event_label
    ON pre_race_pool_snapshots (pool_event_id, snapshot_label, captured_at_utc);

-- Each row is an observed, selection-specific quote.  Use NULL when the tote page only
-- publishes aggregate pool money; never synthesize a combination price.
CREATE TABLE IF NOT EXISTS pre_race_pool_selection_quotes (
    pool_selection_quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_snapshot_id INTEGER NOT NULL,
    selection_key TEXT NOT NULL,                 -- canonical key generated from leg/position/runner_no
    selection_ordering TEXT NOT NULL CHECK (selection_ordering IN ('ORDERED', 'UNORDERED', 'LEGGED')),
    quote_kind TEXT NOT NULL CHECK (quote_kind IN ('ESTIMATED_DIVIDEND', 'DISPLAYED_ODDS', 'POOL_SHARE', 'NONE')),
    quoted_payout_tier TEXT NOT NULL DEFAULT 'MAIN' CHECK (quoted_payout_tier IN ('MAIN', 'CONSOLATION', 'SIX_WIN_BONUS', 'JACKPOT', 'OTHER')),
    quote_value REAL CHECK (quote_value IS NULL OR quote_value >= 0),
    quote_unit REAL CHECK (quote_unit IS NULL OR quote_unit > 0), -- stake unit used for the published return/dividend
    quote_is_return_inclusive INTEGER CHECK (quote_is_return_inclusive IN (0, 1)),
    currency TEXT NOT NULL DEFAULT 'HKD',
    display_rank INTEGER CHECK (display_rank IS NULL OR display_rank > 0),
    CHECK (
        (quote_kind IN ('ESTIMATED_DIVIDEND', 'DISPLAYED_ODDS')
         AND quote_value IS NOT NULL AND quote_unit IS NOT NULL AND quote_is_return_inclusive IS NOT NULL)
        OR quote_kind IN ('POOL_SHARE', 'NONE')
    ),
    UNIQUE (pool_snapshot_id, selection_key, quoted_payout_tier),
    FOREIGN KEY (pool_snapshot_id) REFERENCES pre_race_pool_snapshots(pool_snapshot_id) ON DELETE CASCADE
);

-- Preserve runner number (the pool-native selection identity) and official horse name
-- separately.  position_no is meaningful for ordered pools; for unordered pools store
-- canonical ascending component order; for DOUBLE_TRIO sort independently in each leg;
-- for SIX_UP it is normally 1.
CREATE TABLE IF NOT EXISTS pre_race_pool_selection_members (
    pool_selection_quote_id INTEGER NOT NULL,
    leg_no INTEGER NOT NULL CHECK (leg_no BETWEEN 1 AND 6),
    position_no INTEGER NOT NULL CHECK (position_no BETWEEN 1 AND 4),
    runner_no INTEGER NOT NULL CHECK (runner_no > 0),
    horse_name TEXT,
    PRIMARY KEY (pool_selection_quote_id, leg_no, position_no),
    FOREIGN KEY (pool_selection_quote_id) REFERENCES pre_race_pool_selection_quotes(pool_selection_quote_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_complex_pool_member_runner
    ON pre_race_pool_selection_members (runner_no, horse_name);

-- Store official per-leg finishers independently of dividends.  For a single-race
-- ordered pool, leg_no is 1 and finish_position is 1..3 or 1..4. For DOUBLE_TRIO,
-- retain head three in each leg; for SIX_UP retain each leg's first and second so the
-- main prize and Six Win Bonus can be verified.
CREATE TABLE IF NOT EXISTS official_pool_result_members (
    pool_event_id INTEGER NOT NULL,
    leg_no INTEGER NOT NULL CHECK (leg_no BETWEEN 1 AND 6),
    finish_position INTEGER NOT NULL CHECK (finish_position BETWEEN 1 AND 4),
    runner_no INTEGER NOT NULL CHECK (runner_no > 0),
    horse_name TEXT,
    PRIMARY KEY (pool_event_id, leg_no, finish_position),
    FOREIGN KEY (pool_event_id) REFERENCES pre_race_pool_events(pool_event_id) ON DELETE CASCADE
);

-- Store official final dividends/payouts after the result as a separate post-race fact.
-- For DOUBLE_TRIO, MAIN means two-leg success and CONSOLATION means the official
-- first-leg-only fallback tier when no main winning unit exists. This table must never
-- be joined into pre-race feature generation.
CREATE TABLE IF NOT EXISTS official_pool_payouts (
    pool_event_id INTEGER NOT NULL,
    payout_tier TEXT NOT NULL CHECK (payout_tier IN ('MAIN', 'CONSOLATION', 'SIX_WIN_BONUS', 'JACKPOT', 'OTHER')),
    winning_selection_key TEXT,
    payout_per_unit REAL NOT NULL CHECK (payout_per_unit >= 0),
    payout_unit REAL NOT NULL CHECK (payout_unit > 0),
    payout_is_return_inclusive INTEGER NOT NULL CHECK (payout_is_return_inclusive IN (0, 1)),
    currency TEXT NOT NULL DEFAULT 'HKD',
    result_source_url TEXT,
    result_payload_sha256 TEXT,
    declared_at_utc TEXT,
    PRIMARY KEY (pool_event_id, payout_tier, winning_selection_key),
    FOREIGN KEY (pool_event_id) REFERENCES pre_race_pool_events(pool_event_id) ON DELETE CASCADE
);

CREATE VIEW IF NOT EXISTS v_pre_race_complex_quotes_complete AS
SELECT
    e.meeting_date, e.meeting_racecourse, e.pool_type, e.pool_event_code,
    s.pool_snapshot_id, s.snapshot_label, s.captured_at_utc, s.anchor_leg_no,
    s.scheduled_anchor_start_utc, s.capture_delta_seconds,
    s.quote_completeness, s.gross_pool_amount, s.carryover_amount,
    q.pool_selection_quote_id, q.selection_key, q.selection_ordering,
    q.quote_kind, q.quoted_payout_tier, q.quote_value, q.quote_unit,
    q.quote_is_return_inclusive, q.currency, q.display_rank
FROM pre_race_pool_events AS e
JOIN pre_race_pool_snapshots AS s USING (pool_event_id)
LEFT JOIN pre_race_pool_selection_quotes AS q USING (pool_snapshot_id)
WHERE s.status = 'complete';
