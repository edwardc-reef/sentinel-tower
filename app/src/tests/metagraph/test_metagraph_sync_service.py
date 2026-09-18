"""Unit tests for MetagraphSyncService persistence of dTAO APY data points."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from django.utils import timezone
from sentinel.v1.testing.factories import (
    BlockFactory as SdkBlockFactory,
)
from sentinel.v1.testing.factories import (
    ColdkeyFactory as SdkColdkeyFactory,
)
from sentinel.v1.testing.factories import (
    FullSubnetSnapshotFactory,
    HotkeyWithColdkeyFactory,
    SubnetWithOwnerFactory,
)

from apps.metagraph.models import MetagraphDump, Subnet
from apps.metagraph.services.metagraph_sync_service import DumpMetadata, MetagraphSyncService
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
def test_sync_subnet_backfills_registration_time_onto_a_placeholder_row():
    """A subnet first created by the emission sync has no registered_at; the dump must supply it."""
    Subnet.objects.create(netuid=4242)  # what BurnService.sync_subnet_emissions writes
    service = MetagraphSyncService()
    registered_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    subnet = service._sync_subnet(
        SimpleNamespace(
            netuid=4242,
            name="four",
            owner_hotkey=None,
            registered_at=registered_at,
            alpha_out_emission=1.0,
            tempo=360,
            moving_price=0.025,
        )
    )
    subnet.refresh_from_db()

    assert subnet.registered_at == registered_at
    assert subnet.name == "four"


@pytest.mark.django_db
def test_sync_subnet_keeps_the_registration_time_it_already_stored():
    """Registration time is immutable on chain: a later dump must not move it."""
    stored = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    Subnet.objects.create(netuid=4242, registered_at=stored)
    service = MetagraphSyncService()

    subnet = service._sync_subnet(
        SimpleNamespace(
            netuid=4242,
            name="four",
            owner_hotkey=None,
            registered_at=datetime(2026, 6, 7, 8, 9, 10, tzinfo=UTC),
            alpha_out_emission=1.0,
            tempo=360,
            moving_price=0.025,
        )
    )
    subnet.refresh_from_db()

    assert subnet.registered_at == stored


def _snapshot_with_owner(netuid: int, block_number: int, owner_hotkey: str, owner_coldkey: str):
    """A dumpable subnet snapshot owned by ``owner_hotkey``, with no neurons or tensors."""
    return FullSubnetSnapshotFactory.build(
        subnet=SubnetWithOwnerFactory.build(
            netuid=netuid,
            owner_hotkey=HotkeyWithColdkeyFactory.build(
                hotkey=owner_hotkey,
                coldkey=SdkColdkeyFactory.build(coldkey=owner_coldkey),
            ),
        ),
        block=SdkBlockFactory.build(block_number=block_number),
        neurons=[],
        weights=None,
        bonds=None,
        collaterals=None,
    )


def _dump_metadata(netuid: int) -> DumpMetadata:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    return DumpMetadata(netuid=netuid, epoch_position="start", started_at=now, finished_at=now)


@pytest.mark.django_db
def test_each_dump_records_the_subnet_owner_at_its_own_block():
    """Burn is recomputed from old dumps, so each one has to carry the owner of its block."""
    netuid = 4242
    service = MetagraphSyncService()

    service.sync_metagraph(
        _snapshot_with_owner(netuid, 8_760_000, "5OldOwnerHotkey", "5OldOwnerColdkey"), _dump_metadata(netuid)
    )
    # The subnet changes hands, and a later block is dumped.
    service.sync_metagraph(
        _snapshot_with_owner(netuid, 8_760_361, "5NewOwnerHotkey", "5NewOwnerColdkey"), _dump_metadata(netuid)
    )

    owners = dict(MetagraphDump.objects.values_list("block_id", "owner_hotkey__hotkey"))
    assert owners == {8_760_000: "5OldOwnerHotkey", 8_760_361: "5NewOwnerHotkey"}
    assert Subnet.objects.get(netuid=netuid).owner_hotkey.hotkey == "5NewOwnerHotkey"


@pytest.mark.django_db
def test_a_dump_reporting_no_owner_keeps_the_last_known_one():
    """A missing owner in the SDK payload is missing data, not a transfer to nobody."""
    netuid = 4242
    service = MetagraphSyncService()
    service.sync_metagraph(
        _snapshot_with_owner(netuid, 8_760_000, "5OwnerHotkey", "5OwnerColdkey"), _dump_metadata(netuid)
    )

    ownerless = _snapshot_with_owner(netuid, 8_760_361, "5OwnerHotkey", "5OwnerColdkey")
    ownerless.subnet.owner_hotkey = None
    service.sync_metagraph(ownerless, _dump_metadata(netuid))

    owners = dict(MetagraphDump.objects.values_list("block_id", "owner_hotkey__hotkey"))
    assert owners == {8_760_000: "5OwnerHotkey", 8_760_361: "5OwnerHotkey"}


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
