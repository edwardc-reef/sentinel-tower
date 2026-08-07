"""Retention pruning for the metagraph app.

Deletes rows whose block is at or below a cutoff block number, in bounded
batches (each batch is its own transaction, with a short sleep in between so
autovacuum and live sync keep up). Two independent cutoffs: non-validator
neuron snapshots (+ their mechanism metrics) follow the snapshot cutoff,
while the bulk tables (weight, bond, collateral) follow the — typically
newer — bulk cutoff. Validator neuron snapshots and their mechanism metrics
are never deleted — they are the source data for the validator-APY epoch
ingest (and its backfill/repair command); the legacy APY materialized views
also still read them until Release B.

There are no DB-level cascades (all FKs are NO ACTION, DEFERRABLE INITIALLY
DEFERRED), so each snapshot batch deletes child mechanism_metrics rows and
the snapshots in one data-modifying CTE statement; the deferred FK check
passes at COMMIT.

Do not run concurrently with a historical backfill that inserts blocks below
the cutoff.
"""

import time

import structlog
from django.conf import settings
from django.db import connection, transaction

logger = structlog.get_logger()

# Tables prunable by a simple indexed block-column range delete.
_SIMPLE_TABLES = (
    ("metagraph_weight", "block_id"),
    ("metagraph_bond", "block_id"),
    ("metagraph_collateral", "block_id"),
)

_SNAPSHOT_BATCH_DELETE_SQL = """
    WITH batch AS (
        SELECT id FROM metagraph_neuron_snapshot
        WHERE block_id <= %(cutoff)s AND is_validator = false
        ORDER BY block_id
        LIMIT %(batch_size)s
    ),
    mm AS (
        DELETE FROM metagraph_mechanism_metrics
        WHERE snapshot_id IN (SELECT id FROM batch)
        RETURNING 1
    ),
    ns AS (
        DELETE FROM metagraph_neuron_snapshot
        WHERE id IN (SELECT id FROM batch)
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM mm) AS mm_deleted, (SELECT count(*) FROM ns) AS ns_deleted
"""


def _delete_snapshot_batch(cutoff_block: int, batch_size: int) -> tuple[int, int]:
    """Delete one batch of non-validator snapshots and their mechanism metrics.

    Returns (mechanism_metrics_deleted, snapshots_deleted).
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            _SNAPSHOT_BATCH_DELETE_SQL,
            {"cutoff": cutoff_block, "batch_size": batch_size},
        )
        mm_deleted, ns_deleted = cursor.fetchone()
        return mm_deleted, ns_deleted


def _delete_simple_batch(table: str, block_column: str, cutoff_block: int, batch_size: int) -> int:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {table} WHERE id IN ("  # noqa: S608 — table names are module constants
            f" SELECT id FROM {table} WHERE {block_column} <= %(cutoff)s"
            f" ORDER BY {block_column} LIMIT %(batch_size)s)",
            {"cutoff": cutoff_block, "batch_size": batch_size},
        )
        return cursor.rowcount


def _count(sql: str, params: dict) -> int:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def prune_expired(
    snapshot_cutoff_block: int | None,
    bulk_cutoff_block: int | None,
    batch_size: int | None = None,
    dry_run: bool = False,
    max_batches: int | None = None,
) -> dict[str, int]:
    """Prune metagraph rows past their table group's cutoff. Returns rows per table.

    Non-validator neuron snapshots (+ their mechanism metrics) at or below
    ``snapshot_cutoff_block`` are deleted; weight/bond/collateral rows at or
    below ``bulk_cutoff_block`` are deleted. Either cutoff may be ``None``,
    which skips that table group entirely (its counts are reported as 0).

    ``max_batches`` caps batches PER TABLE (so a run may use up to
    4×``max_batches`` batches total) and makes runs resumable — a capped run
    deletes the oldest rows first, and the next run picks up where it left off.

    ``dry_run=True`` returns would-delete counts without deleting anything and
    ignores ``max_batches``.

    Single-flight/serialization is the caller's job — the
    ``project.core.retention`` orchestrator wraps this in a Postgres advisory
    lock, so concurrent runs can't overlap; do not call this concurrently from
    elsewhere.
    """
    if batch_size is None:
        batch_size = settings.DATA_RETENTION_BATCH_SIZE
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    if dry_run:
        snapshot_params = {"cutoff": snapshot_cutoff_block}
        bulk_params = {"cutoff": bulk_cutoff_block}
        return {
            "metagraph_neuron_snapshot": 0
            if snapshot_cutoff_block is None
            else _count(
                "SELECT count(*) FROM metagraph_neuron_snapshot WHERE block_id <= %(cutoff)s AND is_validator = false",
                snapshot_params,
            ),
            "metagraph_mechanism_metrics": 0
            if snapshot_cutoff_block is None
            else _count(
                "SELECT count(*) FROM metagraph_mechanism_metrics mm"
                " JOIN metagraph_neuron_snapshot ns ON ns.id = mm.snapshot_id"
                " WHERE ns.block_id <= %(cutoff)s AND ns.is_validator = false",
                snapshot_params,
            ),
            **{
                table: 0
                if bulk_cutoff_block is None
                else _count(
                    f"SELECT count(*) FROM {table} WHERE {col} <= %(cutoff)s",  # noqa: S608
                    bulk_params,
                )
                for table, col in _SIMPLE_TABLES
            },
        }

    logger.info(
        "Retention prune starting",
        snapshot_cutoff_block=snapshot_cutoff_block,
        bulk_cutoff_block=bulk_cutoff_block,
        batch_size=batch_size,
    )

    deleted = {
        "metagraph_neuron_snapshot": 0,
        "metagraph_mechanism_metrics": 0,
        **{table: 0 for table, _ in _SIMPLE_TABLES},
    }

    if snapshot_cutoff_block is not None:
        batches = 0
        while max_batches is None or batches < max_batches:
            mm_count, ns_count = _delete_snapshot_batch(snapshot_cutoff_block, batch_size)
            deleted["metagraph_mechanism_metrics"] += mm_count
            deleted["metagraph_neuron_snapshot"] += ns_count
            batches += 1
            if ns_count == 0:
                break
            logger.info(
                "Pruned retention batch",
                table="metagraph_neuron_snapshot",
                snapshots=ns_count,
                mechanism_metrics=mm_count,
            )
            time.sleep(settings.DATA_RETENTION_BATCH_SLEEP_SECONDS)

    if bulk_cutoff_block is not None:
        for table, col in _SIMPLE_TABLES:
            batches = 0
            while max_batches is None or batches < max_batches:
                count = _delete_simple_batch(table, col, bulk_cutoff_block, batch_size)
                deleted[table] += count
                batches += 1
                if count == 0:
                    break
                logger.info("Pruned retention batch", table=table, rows=count)
                time.sleep(settings.DATA_RETENTION_BATCH_SLEEP_SECONDS)

    logger.info(
        "Retention prune finished",
        snapshot_cutoff_block=snapshot_cutoff_block,
        bulk_cutoff_block=bulk_cutoff_block,
        **deleted,
    )
    return deleted
