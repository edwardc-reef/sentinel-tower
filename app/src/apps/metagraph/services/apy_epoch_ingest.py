"""Incremental ingestion of validator per-epoch APY facts.

Single source of truth for the SQL used by the `ingest_validator_apy_epochs`
beat task (snapshot-id ranges) and the `backfill_validator_apy_epochs`
management command (block ranges).

Sources are NOT append-only (the sync service uses update_or_create), so the
insert is an upsert and each range pass first removes facts whose source rows
no longer qualify. The `IS DISTINCT FROM` guard keeps re-scans of unchanged
rows from generating dead tuples every tick.

Block timestamps are treated as write-once here: the upsert and
`sweep_timestamps` only ever fill a NULL `epoch_ts`, they never overwrite an
already-set one. A future timestamp-correction effort must not rely on this
path to propagate a changed non-NULL timestamp.
"""

# Advisory-lock key serialising ingest ticks and backfill runs ("APYE").
# Session-level (command) and xact-level (task) locks share one lock space,
# so the two paths mutually exclude.
INGEST_LOCK_KEY = 0x41505945

# Id overlap re-scanned below the watermark each tick; must exceed the ids
# that can commit around any in-flight sync transaction. The task warns when
# a single tick advances by more than half of this.
REPROCESS_MARGIN = 1_000_000

RETENTION_DAYS = 90

STATEMENT_TIMEOUT = "300s"

_ID_RANGE = "ns.id > %(min_id)s AND ns.id <= %(max_id)s"
_BLOCK_RANGE = "ns.block_id >= %(block_start)s AND ns.block_id <= %(block_end)s"

# The overflow guard mirrors migration 0011: power(b, e) = exp(e*ln(b)) in
# Postgres, so cap the exponent term, then cap the result at 1e6 %.
_APY_EXPR = """
    LEAST(
        (exp(LEAST(
            (2629800.0 / (COALESCE(NULLIF(d.tempo, 0), NULLIF(s.tempo, 0), 360) + 1))
            * ln(1 + ns.alpha_dividends::numeric / ns.alpha_stake::numeric),
            14::numeric
        )) - 1) * 100,
        1000000::numeric
    )
"""

# Two shapes of the same delete. Which one wins depends on how much of
# metagraph_neuron_snapshot the range covers, so each caller picks its own.
#
# The plain join lets the planner drive from whichever side it likes. That is
# right for a backfill chunk, whose block range spans millions of snapshot
# rows: materialising them costs more than joining the epoch table directly
# (measured on prod over a 5k-block range, warm: 1.2 s plain against 4.1 s
# materialised).
_RECONCILE_TEMPLATE = """
    DELETE FROM metagraph_validator_apy_epoch e
    USING metagraph_neuron_snapshot ns
    JOIN metagraph_neuron n ON n.id = ns.neuron_id
    WHERE {range_predicate}
      AND e.subnet_id = n.subnet_id
      AND e.neuron_id = ns.neuron_id
      AND e.epoch_block = ns.block_id
      AND NOT (
          ns.is_validator = true
          AND ns.alpha_stake > 0
          AND EXISTS (
              SELECT 1 FROM metagraph_dump d
              WHERE d.block_id = ns.block_id
                AND d.netuid = n.subnet_id
                AND d.epoch_position = 2
          )
      )
"""

# A tick's id window is bounded by REPROCESS_MARGIN, so its candidate set is
# small and worth materialising: it stops the planner from abandoning the range
# once the epoch table grows. With the plain join above, prod (2.6M epoch rows
# after the historical backfill) scanned every epoch row and probed the 9 GB
# unique_neuron_block index once per row, taking 275 s per tick against 65-80 s
# when the table held 0.45M rows, and running ~20 s under the 300 s statement
# timeout. Fenced, the same tick measured 4.6 s.
_RECONCILE_FENCED_TEMPLATE = """
    WITH candidate_snapshots AS MATERIALIZED (
        SELECT ns.neuron_id, ns.block_id, ns.is_validator, ns.alpha_stake, n.subnet_id
        FROM metagraph_neuron_snapshot ns
        JOIN metagraph_neuron n ON n.id = ns.neuron_id
        WHERE {range_predicate}
    )
    DELETE FROM metagraph_validator_apy_epoch e
    USING candidate_snapshots c
    WHERE e.subnet_id = c.subnet_id
      AND e.neuron_id = c.neuron_id
      AND e.epoch_block = c.block_id
      AND NOT (
          c.is_validator = true
          AND c.alpha_stake > 0
          AND EXISTS (
              SELECT 1 FROM metagraph_dump d
              WHERE d.block_id = c.block_id
                AND d.netuid = c.subnet_id
                AND d.epoch_position = 2
          )
      )
"""

_UPSERT_TEMPLATE = f"""
    INSERT INTO metagraph_validator_apy_epoch AS e
        (subnet_id, neuron_id, hotkey_id, epoch_block, epoch_ts,
         alpha_stake, alpha_dividends, total_stake, tempo, apy_pct)
    SELECT
        n.subnet_id,
        ns.neuron_id,
        n.hotkey_id,
        ns.block_id,
        b.timestamp,
        ns.alpha_stake,
        ns.alpha_dividends,
        ns.total_stake,
        COALESCE(NULLIF(d.tempo, 0), NULLIF(s.tempo, 0), 360),
        {_APY_EXPR}
    FROM metagraph_neuron_snapshot ns
    JOIN metagraph_neuron n ON n.id = ns.neuron_id
    JOIN metagraph_subnet s ON s.netuid = n.subnet_id
    JOIN metagraph_dump d ON d.block_id = ns.block_id AND d.netuid = n.subnet_id
    LEFT JOIN metagraph_block b ON b.number = ns.block_id
    WHERE {{range_predicate}}
      AND ns.is_validator = true
      AND ns.alpha_stake > 0
      AND d.epoch_position = 2
    ON CONFLICT (subnet_id, neuron_id, epoch_block) DO UPDATE SET
        hotkey_id = EXCLUDED.hotkey_id,
        epoch_ts = COALESCE(EXCLUDED.epoch_ts, e.epoch_ts),
        alpha_stake = EXCLUDED.alpha_stake,
        alpha_dividends = EXCLUDED.alpha_dividends,
        total_stake = EXCLUDED.total_stake,
        tempo = EXCLUDED.tempo,
        apy_pct = EXCLUDED.apy_pct
    WHERE (e.hotkey_id, e.alpha_stake, e.alpha_dividends,
           e.total_stake, e.tempo, e.apy_pct)
          IS DISTINCT FROM
          (EXCLUDED.hotkey_id, EXCLUDED.alpha_stake, EXCLUDED.alpha_dividends,
           EXCLUDED.total_stake, EXCLUDED.tempo, EXCLUDED.apy_pct)
       OR (e.epoch_ts IS NULL AND EXCLUDED.epoch_ts IS NOT NULL)
"""  # noqa: S608 — _APY_EXPR is a fixed module constant, not user input

SWEEP_SQL = """
    UPDATE metagraph_validator_apy_epoch e
    SET epoch_ts = b.timestamp
    FROM metagraph_block b
    WHERE e.epoch_ts IS NULL
      AND b.number = e.epoch_block
      AND b.timestamp IS NOT NULL
"""

_RETENTION_CUTOFF_BLOCK_SQL = """
    SELECT MAX(number) FROM metagraph_block
    WHERE timestamp IS NOT NULL
      AND timestamp < now() - make_interval(days => %(days)s)
"""

_RETENTION_DELETE_TS_SQL = """
    DELETE FROM metagraph_validator_apy_epoch
    WHERE epoch_ts < now() - make_interval(days => %(days)s)
"""

_RETENTION_DELETE_TS_AND_BLOCK_SQL = """
    DELETE FROM metagraph_validator_apy_epoch
    WHERE epoch_ts < now() - make_interval(days => %(days)s)
       OR (epoch_ts IS NULL AND epoch_block <= %(cutoff_block)s)
"""


def _reconcile_and_upsert(cursor, range_predicate: str, params: dict, reconcile_template: str) -> tuple[int, int]:
    """Remove now-ineligible facts, then upsert eligible ones. Returns (deleted, upserted)."""
    cursor.execute(reconcile_template.format(range_predicate=range_predicate), params)
    deleted = cursor.rowcount
    cursor.execute(_UPSERT_TEMPLATE.format(range_predicate=range_predicate), params)
    upserted = cursor.rowcount
    return deleted, upserted


def ingest_id_range(cursor, *, min_id: int, max_id: int) -> tuple[int, int]:
    return _reconcile_and_upsert(
        cursor,
        _ID_RANGE,
        {"min_id": min_id, "max_id": max_id},
        _RECONCILE_FENCED_TEMPLATE,
    )


def ingest_block_range(cursor, *, block_start: int, block_end: int) -> tuple[int, int]:
    return _reconcile_and_upsert(
        cursor,
        _BLOCK_RANGE,
        {"block_start": block_start, "block_end": block_end},
        _RECONCILE_TEMPLATE,
    )


def sweep_timestamps(cursor) -> int:
    cursor.execute(SWEEP_SQL)
    return cursor.rowcount


def apply_retention(cursor) -> int:
    """Delete rows older than RETENTION_DAYS; NULL-timestamp rows fall back to
    a block-number cutoff so permanently untimestamped rows stay bounded."""
    days = {"days": RETENTION_DAYS}
    cursor.execute(_RETENTION_CUTOFF_BLOCK_SQL, days)
    cutoff_block = cursor.fetchone()[0]
    if cutoff_block is None:
        cursor.execute(_RETENTION_DELETE_TS_SQL, days)
    else:
        cursor.execute(_RETENTION_DELETE_TS_AND_BLOCK_SQL, days | {"cutoff_block": cutoff_block})
    return cursor.rowcount
