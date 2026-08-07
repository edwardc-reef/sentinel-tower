"""Tests for the incremental validator-APY epoch table and its ingest paths."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.metagraph.models import (
    ValidatorApyEpoch,
    ValidatorApyIngestState,
)
from tests.factories.metagraph import (
    MetagraphDumpFactory,
    NeuronFactory,
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
