"""Tests for the incremental validator-APY epoch table and its ingest paths."""

from decimal import Decimal

import pytest
from django.utils import timezone
from sentinel.v1.services.apy import single_epoch_apy

from apps.metagraph import tasks
from apps.metagraph.models import (
    ValidatorApyEpoch,
    ValidatorApyIngestState,
)
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
