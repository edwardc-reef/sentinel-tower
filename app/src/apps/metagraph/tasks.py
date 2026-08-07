"""Celery tasks for the metagraph app.

`ingest_validator_apy_epochs` is the 15-min beat task (see
CELERY_BEAT_SCHEDULE in project/settings.py) that incrementally maintains
`metagraph_validator_apy_epoch`, the table backing the validator-APY
dashboard.

`refresh_validator_apy_windows` is no longer scheduled. It refreshes the
legacy materialized views `mv_validator_apy_windows` and
`mv_subnet_validator_apy_epochs` and is kept for manual/emergency use only,
until Release B drops those views. See its own docstring for refresh details.
"""

from datetime import timedelta

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task
from django.conf import settings
from django.db import connection, transaction
from prometheus_client import Gauge

from apps.metagraph.models import Block, NeuronSnapshot, SnapshotHealthMetric, Subnet
from apps.metagraph.services import apy_epoch_ingest
from apps.metagraph.utils import get_dumpable_blocks_in_range

logger = structlog.get_logger()

REFRESH_TIME_LIMIT = int(timedelta(minutes=10).total_seconds())

# Arbitrary constant identifying the advisory lock that serialises refreshes.
_REFRESH_LOCK_KEY = 0x41505957  # "APYW"
# Kept modest on purpose — the prod DB host is small (~8 GiB RAM).
_REFRESH_WORK_MEM = "256MB"


@shared_task(time_limit=REFRESH_TIME_LIMIT, soft_time_limit=REFRESH_TIME_LIMIT - 30)
def refresh_validator_apy_windows() -> None:
    """Refresh the legacy validator-APY materialized views (manual/emergency only).

    Superseded by `ingest_validator_apy_epochs` on the beat schedule; kept
    until Release B drops `mv_validator_apy_windows` and
    `mv_subnet_validator_apy_epochs`.

    CONCURRENTLY keeps the dashboard readable while refreshing; it requires the
    unique index on each view.

    Two safeguards keep the refresh healthy on the memory-constrained prod host:
      * a session-level advisory lock so overlapping beat ticks / manual runs don't
        stack — two concurrent REFRESHes of the same view block each other and pile
        up, which is how a single slow refresh snowballs into "never finishes";
      * a raised `work_mem`, because the window view aggregates ~1 month of the
        multi-GB neuron_snapshot table and the 4 MB default spills the sort to disk.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_REFRESH_LOCK_KEY])
        row = cursor.fetchone()
        if not (row and row[0]):
            logger.info("apy view refresh already running; skipping this tick")
            return
        try:
            cursor.execute("SELECT set_config('work_mem', %s, false)", [_REFRESH_WORK_MEM])
            # No parallel workers: parallel hash joins share their hash table via
            # dynamic shared memory in the db container's /dev/shm (256MB), and a
            # plan flip after index rebuilds made the refresh exhaust it
            # (psycopg DiskFull, 2026-07-14). Single-process is slower but bounded.
            cursor.execute("SELECT set_config('max_parallel_workers_per_gather', '0', false)")
            cursor.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_validator_apy_windows;")
            cursor.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_subnet_validator_apy_epochs;")
            logger.info("Refreshed mv_validator_apy_windows and mv_subnet_validator_apy_epochs")
        finally:
            cursor.execute("RESET work_mem")
            cursor.execute("RESET max_parallel_workers_per_gather")
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_REFRESH_LOCK_KEY])


INGEST_TIME_LIMIT = int(timedelta(minutes=10).total_seconds())


@shared_task(time_limit=INGEST_TIME_LIMIT, soft_time_limit=INGEST_TIME_LIMIT - 30)
def ingest_validator_apy_epochs() -> None:
    """Incrementally maintain metagraph_validator_apy_epoch (15-min beat).

    Single transaction: xact advisory lock (single-flight, self-releasing),
    transaction-scoped statement_timeout, reconcile+upsert over the bounded id
    overlap, timestamp sweep, retention, watermark advance.

    The try/except sits INSIDE the atomic block on purpose: if the soft time
    limit fires mid-statement, `transaction.atomic().__exit__` would otherwise
    run first and try to ROLLBACK a connection that's still busy running the
    cancelled query server-side, raising OperationalError and masking
    SoftTimeLimitExceeded before this handler ever sees it. With the handler
    inside atomic, we cancel the in-flight query and close the connection
    ourselves; Django's closed_in_transaction handling then sees the closed
    connection and skips the ROLLBACK. Never issue further statements on a
    possibly-busy connection.
    """
    with transaction.atomic():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])
                if not cursor.fetchone()[0]:
                    logger.info("validator-apy ingest already running; skipping this tick")
                    return
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [apy_epoch_ingest.STATEMENT_TIMEOUT],
                )
                cursor.execute(
                    "SELECT last_snapshot_id FROM metagraph_validator_apy_ingest_state WHERE id = 1 FOR UPDATE"
                )
                state_row = cursor.fetchone()
                if state_row is None:
                    # Only reachable via manual DB surgery — migration 0015 seeds
                    # the row. Do NOT auto-seed: 0 triggers an unbounded rescan
                    # crash-loop, MAX(id) silently skips unprocessed snapshots.
                    raise RuntimeError(
                        "validator-apy ingest_state singleton (id=1) is missing; re-seed it manually per migration 0015"
                    )
                watermark = state_row[0]
                cursor.execute("SELECT COALESCE(MAX(id), 0) FROM metagraph_neuron_snapshot")
                current_max = cursor.fetchone()[0]
                if current_max - watermark > apy_epoch_ingest.REPROCESS_MARGIN // 2:
                    logger.warning(
                        "validator-apy ingest tick advanced by more than half the "
                        "re-scan margin; sync volume may be outgrowing the overlap",
                        watermark=watermark,
                        current_max=current_max,
                        margin=apy_epoch_ingest.REPROCESS_MARGIN,
                    )
                deleted, upserted = apy_epoch_ingest.ingest_id_range(
                    cursor,
                    min_id=max(0, watermark - apy_epoch_ingest.REPROCESS_MARGIN),
                    max_id=current_max,
                )
                swept = apy_epoch_ingest.sweep_timestamps(cursor)
                expired = apy_epoch_ingest.apply_retention(cursor)
                cursor.execute(
                    "UPDATE metagraph_validator_apy_ingest_state "
                    "SET last_snapshot_id = GREATEST(last_snapshot_id, %s) WHERE id = 1",
                    [current_max],
                )
        except SoftTimeLimitExceeded:
            if connection.connection is not None:
                connection.connection.cancel_safe(timeout=5.0)
            connection.close()  # atomic sees closed_in_transaction and skips ROLLBACK
            raise
    logger.info(
        "Ingested validator APY epochs",
        reconciled=deleted,
        upserted=upserted,
        timestamps_swept=swept,
        expired=expired,
        scanned_to=current_max,
    )


SNAPSHOT_HEALTH_TIME_LIMIT = int(timedelta(minutes=10).total_seconds())

# Look-back windows for snapshot-health, expressed as a count of blocks.
_SNAPSHOT_HEALTH_WINDOWS = {
    "7d": 7 * 24 * 3600 // settings.BITTENSOR_SECONDS_PER_BLOCK,
    "12d": 12 * 24 * 3600 // settings.BITTENSOR_SECONDS_PER_BLOCK,
}


missing_snapshot_blocks_gauge = Gauge(
    "metagraph_missing_snapshot_blocks",
    "Dumpable blocks with no NeuronSnapshot entries for this netuid in the look-back window",
    ["netuid", "window"],
    multiprocess_mode="max",
)


def _compute_missing_snapshot_blocks() -> dict[tuple[int, str], int]:
    """Count dumpable blocks with no NeuronSnapshot per (netuid, window).

    Mirrors the logic in apps.metagraph.utils.get_dumpable_blocks_in_range: for
    each look-back window it determines the blocks for which neuron snapshots are
    expected per netuid and verifies at least one snapshot exists for each
    expected block/netuid pair. Returns an empty mapping when no timestamped
    block exists yet.
    """
    latest_block = Block.objects.filter(timestamp__isnull=False).order_by("-number").first()
    if not latest_block:
        return {}

    netuids: list[int] = settings.METAGRAPH_NETUIDS or list(Subnet.objects.values_list("netuid", flat=True))

    end_block = latest_block.number
    # Process windows largest-first so each subnet is queried once over the widest
    # block range; the narrower windows are subsets and reuse that single result.
    windows_by_size = sorted(_SNAPSHOT_HEALTH_WINDOWS.items(), key=lambda kv: kv[1], reverse=True)
    widest_start_block = end_block - windows_by_size[0][1]

    results: dict[tuple[int, str], int] = {}
    for netuid in netuids:
        widest_dumpable = get_dumpable_blocks_in_range(widest_start_block, end_block, netuid)
        if not widest_dumpable:
            continue
        # A separate query for each subnet is necessary as querying all dumpable blocks for all
        # subnets in one query causes memory-related server issues. Since this is meant to be run
        # as a Celery task every 72 minutes, the longer runtime isn't critical.
        # Bound by block range rather than `block_id__in=widest_dumpable`: the range becomes part
        # of the (neuron_id, block_id) index condition, while the ~1000-element IN-list is applied
        # as a per-row filter (~230M array comparisons per subnet on prod). Rows at non-dumpable
        # blocks may enter `covered`, but they are never in `dumpable`, so `dumpable - covered`
        # below is unaffected.
        covered = set(
            NeuronSnapshot.objects.filter(
                block_id__gte=widest_start_block,
                block_id__lte=end_block,
                neuron__subnet_id=netuid,
            )
            .values_list("block_id", flat=True)
            .distinct()
        )
        for window_name, block_delta in windows_by_size:
            # Narrow the widest dumpable set to this window's range; no extra query.
            dumpable = {block for block in widest_dumpable if block >= end_block - block_delta}
            if not dumpable:
                continue
            results[(netuid, window_name)] = len(dumpable - covered)
    return results


@shared_task(time_limit=SNAPSHOT_HEALTH_TIME_LIMIT, soft_time_limit=SNAPSHOT_HEALTH_TIME_LIMIT - 30)
def update_snapshot_health_metrics() -> None:
    """
    Recompute snapshot-health counts and persist them for the /metrics endpoint.

    The celery task does not update the metric itself as this would result in chaos on
    account of the intentionally recycled child processes (see
    settings.CELERY_WORKER_MAX_TASKS_PER_CHILD). Instead, the health metrics are persisted to a
    database table that the Prometheus /metrics endpoint can scrape.

    Run by Celery beat (see settings.CELERY_BEAT_SCHEDULE).
    """
    results = _compute_missing_snapshot_blocks()
    with transaction.atomic():
        SnapshotHealthMetric.objects.all().delete()
        SnapshotHealthMetric.objects.bulk_create(
            [
                SnapshotHealthMetric(netuid=netuid, window=window, missing_blocks=missing)
                for (netuid, window), missing in results.items()
            ]
        )
    logger.info("Updated snapshot health metrics", rows=len(results))


def set_snapshot_health_metrics() -> None:
    """
    Populate the snapshot-health gauge from persisted rows in the SnapshotHealthMetric table.

    Clears the metric to allow Prometheus to mark subnet/window combinations that no longer exist
    as stale.
    """
    missing_snapshot_blocks_gauge.clear()
    for netuid, window, missing in SnapshotHealthMetric.objects.values_list("netuid", "window", "missing_blocks"):
        missing_snapshot_blocks_gauge.labels(netuid=str(netuid), window=window).set(missing)
