"""One-time backfill and range repair for metagraph_validator_apy_epoch.

Per prod-ops convention this is a management command run via a compose
profile service. It is also the reconciliation path after force-refreshing an
old snapshot range: a force-refresh is not complete until this command has
been run for the same block range.

The derived block range (used when --block-start/--block-end are omitted)
comes from timestamped blocks; if a recent historical backfill hasn't been
timestamp-swept yet, pass an explicit range instead.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.metagraph.services import apy_epoch_ingest


class Command(BaseCommand):
    help = "Backfill/repair validator APY epoch rows over a block range."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=apy_epoch_ingest.RETENTION_DAYS)
        parser.add_argument("--block-start", type=int, default=None)
        parser.add_argument("--block-end", type=int, default=None)
        parser.add_argument("--chunk-size", type=int, default=50_000)
        parser.add_argument("--statement-timeout", type=str, default="30min")

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])
            if not cursor.fetchone()[0]:
                raise CommandError(
                    "validator-apy ingest lock is held (beat tick or another backfill run); try again later"
                )
            # Session-level (the `false` arg), not transaction-scoped: each
            # chunk below runs in its own transaction.atomic() block, so a
            # transaction-scoped timeout would only cover one chunk. The beat
            # task's own timeout (apy_epoch_ingest.STATEMENT_TIMEOUT, 300s) IS
            # transaction-scoped because it does a single tick in a single
            # transaction; a backfill chunk here legitimately runs much
            # longer (a 50k-block chunk covers roughly a week of snapshots)
            # but must still not run unbounded while holding the ingest lock,
            # given this DB's history of runaway multi-hour refreshes (the
            # 73-minute MV-refresh incident).
            cursor.execute("SELECT set_config('statement_timeout', %s, false)", [options["statement_timeout"]])
        try:
            self._run(options)
        finally:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])
            except Exception as exc:
                # A dead connection here must not mask a real failure raised
                # from _run(); the lock self-releases when the session ends.
                self.stderr.write(self.style.WARNING(f"failed to release validator-apy ingest lock: {exc}"))

    def _run(self, options):
        block_start, block_end = options["block_start"], options["block_end"]
        # Any explicit endpoint marks this as a targeted repair run rather
        # than the standard whole-recent-window backfill; see the
        # watermark-advance comment below for why that distinction matters.
        explicit_range = block_start is not None or block_end is not None

        with connection.cursor() as cursor:
            # Captured BEFORE any chunk: snapshots inserted while we run get
            # higher ids and are the beat task's responsibility afterwards.
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM metagraph_neuron_snapshot")
            current_max = cursor.fetchone()[0]

            if block_start is None or block_end is None:
                cursor.execute(
                    "SELECT MIN(number), MAX(number) FROM metagraph_block "
                    "WHERE timestamp >= now() - make_interval(days => %s)",
                    [options["days"]],
                )
                derived_start, derived_end = cursor.fetchone()
                if derived_start is None:
                    self.stdout.write("no timestamped blocks in range; nothing to do")
                    return
                block_start = block_start if block_start is not None else derived_start
                block_end = block_end if block_end is not None else derived_end

        chunk_size = options["chunk_size"]
        total_deleted = 0
        total_upserted = 0
        for chunk_start in range(block_start, block_end + 1, chunk_size):
            chunk_end = min(chunk_start + chunk_size - 1, block_end)
            chunk_started_at = time.monotonic()
            with transaction.atomic(), connection.cursor() as cursor:
                deleted, upserted = apy_epoch_ingest.ingest_block_range(
                    cursor, block_start=chunk_start, block_end=chunk_end
                )
            elapsed = time.monotonic() - chunk_started_at
            total_deleted += deleted
            total_upserted += upserted
            self.stdout.write(
                f"blocks [{chunk_start}..{chunk_end}]: upserted {upserted}, reconciled {deleted} ({elapsed:.1f}s)"
            )

        if explicit_range:
            # A narrow repair run must NOT advance the watermark: doing so
            # could push it past snapshot ids the beat task never processed
            # (if beat has fallen behind by more than REPROCESS_MARGIN),
            # silently and permanently hiding that gap from the beat's
            # re-scan overlap. Only a run over the derived, whole-recent-
            # window range is safe to advance past, since that range is what
            # the beat's overlap already assumes is covered.
            self.stdout.write("repair mode: watermark untouched")
        else:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE metagraph_validator_apy_ingest_state "
                    "SET last_snapshot_id = GREATEST(last_snapshot_id, %s) WHERE id = 1",
                    [current_max],
                )
                if cursor.rowcount != 1:
                    # Only reachable via manual DB surgery — migration 0015
                    # seeds the row. Mirrors the beat task's own guard.
                    raise CommandError(
                        "validator-apy ingest_state singleton (id=1) is missing; re-seed it manually per migration 0015"
                    )

        self.stdout.write(self.style.SUCCESS(f"done: {total_upserted} rows upserted, {total_deleted} reconciled"))
