"""Unit tests for MetagraphSyncService persistence of dTAO APY data points."""

from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.metagraph.services.metagraph_sync_service import MetagraphSyncService
from tests.factories.metagraph import BlockFactory, NeuronFactory, SubnetFactory


@pytest.mark.django_db
def test_sync_subnet_persists_tempo_and_moving_price():
    service = MetagraphSyncService()
    subnet_model = SimpleNamespace(
        netuid=4242,
        name="four",
        owner_hotkey=None,
        registered_at=timezone.now(),
        alpha_out_emission=1.0,
        tempo=360,
        moving_price=0.025,
    )

    subnet = service._sync_subnet(subnet_model)
    subnet.refresh_from_db()

    assert subnet.tempo == 360
    assert subnet.moving_price == pytest.approx(0.025)


@pytest.mark.django_db
def test_sync_subnet_updates_tempo_and_moving_price():
    service = MetagraphSyncService()
    base = SimpleNamespace(
        netuid=4242,
        name="four",
        owner_hotkey=None,
        registered_at=timezone.now(),
        alpha_out_emission=1.0,
        tempo=360,
        moving_price=0.025,
    )
    service._sync_subnet(base)

    changed = SimpleNamespace(**(vars(base) | {"tempo": 720, "moving_price": 0.05}))
    subnet = service._sync_subnet(changed)
    subnet.refresh_from_db()

    assert subnet.tempo == 720
    assert subnet.moving_price == pytest.approx(0.05)


@pytest.mark.django_db
def test_sync_neuron_snapshot_persists_dividends_in_rao():
    service = MetagraphSyncService()
    neuron = NeuronFactory()
    block = BlockFactory()
    snapshot_model = SimpleNamespace(
        uid=neuron.uid,
        axon_address="",
        total_stake=10.0,
        alpha_stake=5.0,
        normalized_stake=0.5,
        rank=0.0,
        trust=0.0,
        emissions=0.0,
        is_active=True,
        is_validator=True,
        is_immune=False,
        has_any_weights=True,
        neuron_version=None,
        block_at_registration=1,
        alpha_dividends=0.05,
        tao_dividends=0.001,
    )

    snapshot = service._sync_neuron_snapshot(snapshot_model, neuron, block)
    snapshot.refresh_from_db()

    assert int(snapshot.alpha_dividends) == 50_000_000  # 0.05 * 1e9
    assert int(snapshot.tao_dividends) == 1_000_000  # 0.001 * 1e9


@pytest.mark.django_db
def test_sync_metagraph_dump_captures_tempo():
    service = MetagraphSyncService()
    subnet = SubnetFactory(tempo=720)
    block = BlockFactory()
    dump_metadata = SimpleNamespace(
        netuid=subnet.netuid,
        epoch_position="end",
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )

    dump = service._sync_metagraph_dump(dump_metadata, block, subnet)
    dump.refresh_from_db()

    assert dump.epoch_position == 2
    assert dump.tempo == 720
