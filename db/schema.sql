-- Canonical TESTAG database schema. build_db.py drops and recreates these tables.
DROP TABLE IF EXISTS contest_result_official_rank;
DROP TABLE IF EXISTS contest_result_operator;
DROP TABLE IF EXISTS contest_result;
DROP TABLE IF EXISTS contest_result_category;
DROP TABLE IF EXISTS contest_result_eligibility_basis;
DROP TABLE IF EXISTS contest_result_partition;
DROP TABLE IF EXISTS testag_dataset_release;
DROP TABLE IF EXISTS testag_schema_migration;
DROP TYPE IF EXISTS testag_award_eligibility;

CREATE TYPE testag_award_eligibility AS ENUM ('eligible', 'ineligible', 'review');

CREATE TABLE testag_dataset_release (
    release_id text PRIMARY KEY,
    built_at_utc timestamptz NOT NULL DEFAULT now(),
    latest_retrieved_at_utc timestamptz NOT NULL,
    dataset_license text NOT NULL,
    attribution text NOT NULL,
    partition_count integer NOT NULL CHECK (partition_count >= 0),
    entry_count bigint NOT NULL CHECK (entry_count >= 0),
    operator_membership_count bigint NOT NULL CHECK (operator_membership_count >= 0)
);

CREATE TABLE contest_result_partition (
    contest_id text NOT NULL CHECK (
        contest_id IN ('cqww-dx', 'cq-wpx', 'wae-dx', 'wapc-dx', 'aadx', 'iota', 'arrl-dx')
    ),
    edition_year smallint NOT NULL CHECK (edition_year BETWEEN 1900 AND 2200),
    mode text NOT NULL CHECK (mode IN ('ssb', 'cw', 'mixed')),
    retrieved_at_utc timestamptz NOT NULL,
    source_url text NOT NULL,
    archive_digest text,
    builder_version text,
    PRIMARY KEY (contest_id, edition_year, mode)
);

CREATE TABLE contest_result_category (
    category_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_source text NOT NULL UNIQUE
);

CREATE TABLE contest_result_eligibility_basis (
    eligibility_basis_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    eligibility_basis text NOT NULL UNIQUE
);

CREATE TABLE contest_result (
    result_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id text NOT NULL UNIQUE,
    contest_id text NOT NULL,
    edition_year smallint NOT NULL CHECK (edition_year BETWEEN 1900 AND 2200),
    mode text NOT NULL,
    station_callsign_source text NOT NULL,
    station_callsign text NOT NULL,
    location_code_source text,
    category_id integer NOT NULL REFERENCES contest_result_category(category_id),
    dxcc_entity_source text,
    dxcc_adif smallint CHECK (dxcc_adif > 0),
    dxcc_entity_inferred text,
    dxcc_resolution text NOT NULL CHECK (
        dxcc_resolution IN ('source', 'callsign', 'unresolved', 'ambiguous_year', 'ambiguous_prefix', 'no_entity')
    ),
    overlay_category_source text,
    score_source text,
    score bigint,
    qso_count integer,
    cq_zones integer,
    cq_countries integer,
    wpx_prefixes integer,
    multipliers integer,
    operating_hours_source text,
    operating_hours numeric(6, 1),
    late_entry boolean NOT NULL,
    award_eligibility testag_award_eligibility NOT NULL,
    eligibility_basis_id integer NOT NULL REFERENCES contest_result_eligibility_basis(eligibility_basis_id),
    operator_list_complete boolean NOT NULL,
    category_world_rank_official integer CHECK (
        category_world_rank_official IS NULL OR category_world_rank_official > 0
    ),
    category_world_rank_calculated integer CHECK (
        category_world_rank_calculated IS NULL OR (
            category_world_rank_calculated > 0
            AND award_eligibility = 'eligible'
            AND score IS NOT NULL
        )
    ),
    category_in_dxcc_rank_official integer CHECK (
        category_in_dxcc_rank_official IS NULL OR category_in_dxcc_rank_official > 0
    ),
    category_in_dxcc_rank_calculated integer CHECK (
        category_in_dxcc_rank_calculated IS NULL OR (
            category_in_dxcc_rank_calculated > 0
            AND award_eligibility = 'eligible'
            AND score IS NOT NULL
            AND (dxcc_entity_source IS NOT NULL OR dxcc_adif IS NOT NULL)
        )
    ),
    overlay_world_rank_official integer CHECK (
        overlay_world_rank_official IS NULL OR overlay_world_rank_official > 0
    ),
    overlay_world_rank_calculated integer CHECK (
        overlay_world_rank_calculated IS NULL OR (
            overlay_world_rank_calculated > 0
            AND award_eligibility = 'eligible'
            AND score IS NOT NULL
            AND overlay_category_source IS NOT NULL
        )
    ),
    overlay_in_dxcc_rank_official integer CHECK (
        overlay_in_dxcc_rank_official IS NULL OR overlay_in_dxcc_rank_official > 0
    ),
    overlay_in_dxcc_rank_calculated integer CHECK (
        overlay_in_dxcc_rank_calculated IS NULL OR (
            overlay_in_dxcc_rank_calculated > 0
            AND award_eligibility = 'eligible'
            AND score IS NOT NULL
            AND overlay_category_source IS NOT NULL
            AND (dxcc_entity_source IS NOT NULL OR dxcc_adif IS NOT NULL)
        )
    ),
    CHECK (
        (dxcc_entity_source IS NOT NULL AND dxcc_resolution = 'source' AND dxcc_entity_inferred IS NULL)
        OR (dxcc_entity_source IS NULL AND dxcc_resolution = 'callsign'
            AND dxcc_adif IS NOT NULL AND dxcc_entity_inferred IS NOT NULL)
        OR (dxcc_entity_source IS NULL AND dxcc_resolution IN ('unresolved', 'ambiguous_year', 'ambiguous_prefix', 'no_entity')
            AND dxcc_adif IS NULL AND dxcc_entity_inferred IS NULL)
    ),
    metrics_json json NOT NULL DEFAULT '{}'::json CHECK (json_typeof(metrics_json) = 'object'),
    FOREIGN KEY (contest_id, edition_year, mode)
        REFERENCES contest_result_partition (contest_id, edition_year, mode)
        ON DELETE CASCADE
);

CREATE TABLE contest_result_operator (
    result_id integer NOT NULL REFERENCES contest_result(result_id) ON DELETE CASCADE,
    operator_order smallint NOT NULL CHECK (operator_order > 0),
    operator_callsign_source text NOT NULL,
    operator_callsign text NOT NULL,
    PRIMARY KEY (result_id, operator_order),
    UNIQUE (result_id, operator_callsign)
);

CREATE INDEX IF NOT EXISTS contest_result_partition_idx
    ON contest_result (contest_id, edition_year, mode);
CREATE INDEX IF NOT EXISTS contest_result_station_lookup_idx
    ON contest_result (station_callsign, contest_id, edition_year DESC, mode);
CREATE INDEX IF NOT EXISTS contest_result_operator_call_lookup_idx
    ON contest_result_operator (operator_callsign, result_id);
