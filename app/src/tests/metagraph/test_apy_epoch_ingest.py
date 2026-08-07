"""Tests for the incremental validator-APY epoch table and its ingest paths."""

from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.utils import timezone
from sentinel.v1.services.apy import single_epoch_apy

from apps.metagraph import tasks
from apps.metagraph.models import (
    MetagraphDump,
    ValidatorApyEpoch,
    ValidatorApyIngestState,
)
from apps.metagraph.services import apy_epoch_ingest
from tests.factories.metagraph import (
    BlockFactory,
    MetagraphDumpFactory,
    NeuronFactory,
    NeuronSnapshotFactory,
    SubnetFactory,
)


@pytest.mark.django_db
def test_watermark_singleton_is_seeded_by_migration():
    # Empty-source bootstrap: the test DB has no snapshots when migrations run,
    # so the seeded watermark must be zero — and the row must already exist.
    state = ValidatorApyIngestState.objects.get(id=1)
    assert state.last_snapshot_id == 0


@pytest.mark.django_db
def test_dump_tempo_field_persists():
    dump = MetagraphDumpFactory(epoch_position=2, tempo=360)
    dump.refresh_from_db()
    assert dump.tempo == 360


@pytest.mark.django_db
def test_validator_apy_epoch_row_roundtrip():
    neuron = NeuronFactory()
    row = ValidatorApyEpoch.objects.create(
        subnet_id=neuron.subnet_id,
        neuron=neuron,
        hotkey=neuron.hotkey,
        epoch_block=1000,
        epoch_ts=timezone.now(),
        alpha_stake=Decimal(10**12),
        alpha_dividends=Decimal(10**9),
        total_stake=Decimal(2 * 10**12),
        tempo=360,
        apy_pct=12.5,
    )
    row.refresh_from_db()
    assert row.apy_pct == pytest.approx(12.5)
    assert int(row.total_stake) == 2 * 10**12


def _make_epoch_source(
    *,
    subnet=None,
    block_number=720,
    epoch_position=2,
    is_validator=True,
    alpha_stake=10**12,
    alpha_dividends=10**9,
    total_stake=2 * 10**12,
    block_ts="now",
    dump_tempo=360,
):
    """One end-of-epoch validator snapshot with its dump + block."""
    subnet = subnet or SubnetFactory(tempo=360)
    block = BlockFactory(
        number=block_number,
        timestamp=timezone.now() if block_ts == "now" else block_ts,
    )
    neuron = NeuronFactory(subnet=subnet)
    snapshot = NeuronSnapshotFactory(
        neuron=neuron,
        block=block,
        is_validator=is_validator,
        alpha_stake=alpha_stake,
        alpha_dividends=alpha_dividends,
        total_stake=total_stake,
    )
    MetagraphDumpFactory(
        netuid=subnet.netuid,
        block=block,
        epoch_position=epoch_position,
        tempo=dump_tempo,
    )
    return snapshot


@pytest.mark.django_db
def test_ingest_creates_epoch_row_and_advances_watermark():
    snapshot = _make_epoch_source()

    tasks.ingest_validator_apy_epochs()

    row = ValidatorApyEpoch.objects.get()
    assert row.subnet_id == snapshot.neuron.subnet_id
    assert row.neuron_id == snapshot.neuron_id
    assert row.hotkey_id == snapshot.neuron.hotkey_id
    assert row.epoch_block == snapshot.block_id
    assert row.epoch_ts == snapshot.block.timestamp
    assert int(row.alpha_stake) == 10**12
    assert int(row.alpha_dividends) == 10**9
    assert int(row.total_stake) == 2 * 10**12
    assert row.tempo == 360
    expected = single_epoch_apy(alpha_earned=float(10**9), alpha_staked=float(10**12), tempo=360)
    assert row.apy_pct == pytest.approx(expected, rel=1e-6)
    assert ValidatorApyIngestState.objects.get(id=1).last_snapshot_id == snapshot.id


@pytest.mark.django_db
def test_ingest_is_idempotent():
    _make_epoch_source()
    tasks.ingest_validator_apy_epochs()
    first = list(ValidatorApyEpoch.objects.values())

    tasks.ingest_validator_apy_epochs()

    assert list(ValidatorApyEpoch.objects.values()) == first


@pytest.mark.django_db
def test_ingest_skips_non_epoch_end_and_non_validators():
    _make_epoch_source(block_number=100, epoch_position=1)  # mid-epoch
    _make_epoch_source(block_number=200, is_validator=False)  # miner
    _make_epoch_source(block_number=300, alpha_stake=0)  # zero stake

    tasks.ingest_validator_apy_epochs()

    assert ValidatorApyEpoch.objects.count() == 0


@pytest.mark.django_db
def test_ingest_keeps_zero_dividend_epochs():
    # Semantic change vs the old MV: APY=0 rows are real data, not gaps.
    _make_epoch_source(alpha_dividends=0)

    tasks.ingest_validator_apy_epochs()

    row = ValidatorApyEpoch.objects.get()
    assert row.apy_pct == 0.0


@pytest.mark.django_db
def test_ingest_dust_stake_is_capped_not_error():
    _make_epoch_source(alpha_stake=1, alpha_dividends=10**9)

    tasks.ingest_validator_apy_epochs()

    row = ValidatorApyEpoch.objects.get()
    assert row.apy_pct <= 1_000_000.0


@pytest.mark.django_db
def test_ingest_tempo_precedence():
    # dump.tempo wins over subnet.tempo; NULL dump.tempo falls back to subnet;
    # tempo=0 everywhere falls back to 360.
    s1 = SubnetFactory(tempo=360)
    _make_epoch_source(subnet=s1, block_number=1000, dump_tempo=100)
    s2 = SubnetFactory(tempo=720)
    _make_epoch_source(subnet=s2, block_number=2000, dump_tempo=None)
    s3 = SubnetFactory(tempo=0)
    _make_epoch_source(subnet=s3, block_number=3000, dump_tempo=0)

    tasks.ingest_validator_apy_epochs()

    by_subnet = {r.subnet_id: r.tempo for r in ValidatorApyEpoch.objects.all()}
    assert by_subnet[s1.netuid] == 100
    assert by_subnet[s2.netuid] == 720
    assert by_subnet[s3.netuid] == 360


@pytest.mark.django_db
def test_ingest_null_block_timestamp_passes_through():
    _make_epoch_source(block_ts=None)

    tasks.ingest_validator_apy_epochs()

    assert ValidatorApyEpoch.objects.get().epoch_ts is None


@pytest.mark.django_db
def test_ingest_repairs_corrected_source_row():
    snapshot = _make_epoch_source()
    tasks.ingest_validator_apy_epochs()

    # A re-dump corrected the dividends (update_or_create keeps the same id,
    # which stays inside the re-scan overlap). Kept well under the 1,000,000%
    # overflow-guard cap (see apy_epoch_ingest._APY_EXPR) so this pins genuine
    # recomputation against the golden formula rather than the cap, which has
    # its own dedicated test (test_ingest_dust_stake_is_capped_not_error).
    new_dividends = 1_100_000_000
    snapshot.alpha_dividends = new_dividends
    snapshot.save(update_fields=["alpha_dividends"])
    tasks.ingest_validator_apy_epochs()

    row = ValidatorApyEpoch.objects.get()
    assert int(row.alpha_dividends) == new_dividends
    assert row.apy_pct == pytest.approx(
        single_epoch_apy(alpha_earned=float(new_dividends), alpha_staked=float(10**12), tempo=360),
        rel=1e-6,
    )


@pytest.mark.django_db
def test_ingest_removes_row_made_ineligible():
    snapshot = _make_epoch_source()
    tasks.ingest_validator_apy_epochs()
    assert ValidatorApyEpoch.objects.count() == 1

    snapshot.is_validator = False
    snapshot.save(update_fields=["is_validator"])
    tasks.ingest_validator_apy_epochs()

    assert ValidatorApyEpoch.objects.count() == 0


@pytest.mark.django_db
def test_ingest_removes_row_when_epoch_position_corrected():
    snapshot = _make_epoch_source()
    tasks.ingest_validator_apy_epochs()

    MetagraphDump.objects.filter(netuid=snapshot.neuron.subnet_id, block=snapshot.block).update(epoch_position=1)
    tasks.ingest_validator_apy_epochs()

    assert ValidatorApyEpoch.objects.count() == 0


@pytest.mark.django_db
def test_sweep_heals_null_timestamp():
    snapshot = _make_epoch_source(block_ts=None)
    tasks.ingest_validator_apy_epochs()
    assert ValidatorApyEpoch.objects.get().epoch_ts is None

    ts = timezone.now()
    snapshot.block.timestamp = ts
    snapshot.block.save(update_fields=["timestamp"])
    # Push the watermark past the re-scan overlap so the upsert can no longer
    # see this snapshot — only SWEEP_SQL can heal the row now. This mirrors
    # the real prod pattern: historical backfill stores NULL timestamps that
    # are filled long after the snapshot ids left the overlap.
    ValidatorApyIngestState.objects.filter(id=1).update(
        last_snapshot_id=snapshot.id + apy_epoch_ingest.REPROCESS_MARGIN + 1
    )
    tasks.ingest_validator_apy_epochs()

    assert ValidatorApyEpoch.objects.get().epoch_ts == ts


@pytest.mark.django_db
def test_retention_removes_old_and_null_ts_rows():
    # Block 100 (timestamped, 91 days old) becomes the retention cutoff block;
    # the NULL-ts row must sit at a LOWER block number (99) so the block-number
    # fallback arm (`epoch_ts IS NULL AND epoch_block <= cutoff`) removes it.
    old_ts = timezone.now() - timedelta(days=91)
    _make_epoch_source(block_number=99, block_ts=None)  # old, NULL ts
    _make_epoch_source(block_number=100, block_ts=old_ts)  # old, timestamped
    _make_epoch_source(block_number=90_000_000, block_ts="now")  # fresh

    tasks.ingest_validator_apy_epochs()

    assert ValidatorApyEpoch.objects.count() == 1
    assert ValidatorApyEpoch.objects.get().epoch_block == 90_000_000


def test_beat_schedule_runs_ingest_not_refresh(settings):
    assert "refresh-validator-apy-windows" not in settings.CELERY_BEAT_SCHEDULE
    entry = settings.CELERY_BEAT_SCHEDULE["ingest-validator-apy-epochs"]
    assert entry["task"] == "apps.metagraph.tasks.ingest_validator_apy_epochs"
    assert entry["schedule"] == timedelta(minutes=15)


@pytest.mark.django_db
def test_ingest_fails_loudly_when_watermark_row_missing():
    ValidatorApyIngestState.objects.all().delete()

    with pytest.raises(RuntimeError, match="ingest_state singleton"):
        tasks.ingest_validator_apy_epochs()


@pytest.mark.django_db
def test_backfill_command_covers_range_in_chunks():
    # Pins off-by-ones: 49_999 falls at the end of the first chunk, 50_000 at
    # the start of the second, and 100_000 is both block_end AND an exact
    # multiple of chunk_size, so it only lands in a chunk at all if the loop
    # uses range(block_start, block_end + 1, chunk_size) rather than dropping
    # the final block to an off-by-one.
    for block_number in (49_999, 50_000, 100_000):
        _make_epoch_source(block_number=block_number)
    # Watermark ahead of these ids simulates "seeded on deploy, history missing".
    ValidatorApyIngestState.objects.filter(id=1).update(last_snapshot_id=10**9)

    call_command(
        "backfill_validator_apy_epochs",
        block_start=0,
        block_end=100_000,
        chunk_size=50_000,
    )

    assert ValidatorApyEpoch.objects.count() == 3
    # Explicit range = repair mode: the watermark must be left untouched
    # entirely (not just non-regressing) — see the watermark-advance comment
    # in backfill_validator_apy_epochs.
    assert ValidatorApyIngestState.objects.get(id=1).last_snapshot_id == 10**9


@pytest.mark.django_db
def test_backfill_derived_mode_advances_watermark():
    snapshot = _make_epoch_source(block_ts="now")
    ValidatorApyIngestState.objects.filter(id=1).update(last_snapshot_id=0)

    # No explicit range: derived from --days default, so this is the
    # "standard whole-recent-window backfill" mode and must advance the
    # watermark (unlike the explicit-range repair mode above).
    call_command("backfill_validator_apy_epochs")

    assert ValidatorApyEpoch.objects.count() == 1
    assert ValidatorApyIngestState.objects.get(id=1).last_snapshot_id == snapshot.id


@pytest.mark.django_db
def test_backfill_command_is_idempotent_and_repairs():
    snapshot = _make_epoch_source(block_number=5_000)
    call_command("backfill_validator_apy_epochs", block_start=0, block_end=10_000)
    assert ValidatorApyEpoch.objects.count() == 1

    new_dividends = 1_100_000_000
    snapshot.alpha_dividends = new_dividends
    snapshot.save(update_fields=["alpha_dividends"])
    call_command("backfill_validator_apy_epochs", block_start=0, block_end=10_000)

    row = ValidatorApyEpoch.objects.get()
    assert int(row.alpha_dividends) == new_dividends


def _raw_pg_connection() -> psycopg.Connection:
    """Second, independent session to the test database."""
    s = connections["default"].settings_dict
    return psycopg.connect(
        host=s["HOST"],
        port=s["PORT"],
        dbname=s["NAME"],
        user=s["USER"],
        password=s["PASSWORD"],
        autocommit=True,
        connect_timeout=5,
    )


def _ensure_ingest_watermark_row() -> None:
    """Re-seed the singleton watermark row consumed by `ingest_validator_apy_epochs`.

    `transaction=True` tests run without a wrapping test transaction, so
    pytest-django resets state via Django's TransactionTestCase teardown,
    which TRUNCATEs every table (`flush`) rather than rolling back a
    savepoint. That wipes the row this migration seeds once
    (0015_validator_apy_epoch) and nothing re-runs a data migration on
    flush, so each transaction=True test in this module must restore it
    itself before calling the task.
    """
    ValidatorApyIngestState.objects.update_or_create(id=1, defaults={"last_snapshot_id": 0})


@pytest.mark.django_db(transaction=True)
def test_ingest_skips_when_lock_held():
    _ensure_ingest_watermark_row()
    _make_epoch_source()
    with _raw_pg_connection() as other:
        other.execute("SELECT pg_advisory_lock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])
        try:
            tasks.ingest_validator_apy_epochs()
            assert ValidatorApyEpoch.objects.count() == 0
        finally:
            other.execute("SELECT pg_advisory_unlock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])

    tasks.ingest_validator_apy_epochs()
    assert ValidatorApyEpoch.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_overlap_recovers_late_committing_snapshot():
    _ensure_ingest_watermark_row()
    # Session A inserts a snapshot but does NOT commit; a later snapshot commits
    # first and the tick advances the watermark past A's id. After A commits,
    # the fixed overlap must pick its row up on the next tick.
    early = _make_epoch_source(block_number=1000)  # committed; allocates the LOWER id
    late = _make_epoch_source(block_number=2000)  # committed; higher id

    # Rebuild "early" as an uncommitted row from a second session with a lower id:
    # delete early's snapshot, then re-insert the same values (same id) from an
    # open, uncommitted transaction while the tick runs.
    snap_values = dict(
        neuron_id=early.neuron_id,
        block_id=early.block_id,
        uid=early.uid,
        alpha_stake=early.alpha_stake,
        alpha_dividends=early.alpha_dividends,
        total_stake=early.total_stake,
    )
    early_id = early.id
    early.delete()

    with _raw_pg_connection() as other:
        other.autocommit = False
        try:
            with other.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO metagraph_neuron_snapshot
                        (id, neuron_id, block_id, uid, axon_address, total_stake,
                         normalized_stake, rank, trust, emissions, alpha_stake,
                         alpha_dividends, tao_dividends, dividend_apy, is_active,
                         is_validator, is_immune, has_any_weights)
                    VALUES (%(id)s, %(neuron_id)s, %(block_id)s, %(uid)s, '', %(total_stake)s,
                            0, 0, 0, 0, %(alpha_stake)s,
                            %(alpha_dividends)s, 0, 0, false,
                            true, false, false)
                    """,
                    snap_values | {"id": early_id},
                )
                # Tick runs while the insert above is still uncommitted.
                tasks.ingest_validator_apy_epochs()
                assert set(ValidatorApyEpoch.objects.values_list("epoch_block", flat=True)) == {late.block_id}
                state = ValidatorApyIngestState.objects.get(id=1)
                assert state.last_snapshot_id >= late.id > early_id
            other.commit()
        except BaseException:
            other.rollback()
            raise

    # Next tick: early_id <= watermark, but within the re-scan overlap.
    tasks.ingest_validator_apy_epochs()
    assert set(ValidatorApyEpoch.objects.values_list("epoch_block", flat=True)) == {1000, 2000}


@pytest.mark.django_db(transaction=True)
def test_backfill_command_refuses_when_lock_held():
    _ensure_ingest_watermark_row()
    with _raw_pg_connection() as other:
        other.execute("SELECT pg_advisory_lock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])
        try:
            with pytest.raises(CommandError):
                call_command("backfill_validator_apy_epochs", block_start=0, block_end=10)
        finally:
            other.execute("SELECT pg_advisory_unlock(%s)", [apy_epoch_ingest.INGEST_LOCK_KEY])
