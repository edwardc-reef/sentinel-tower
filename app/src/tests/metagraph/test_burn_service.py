"""Tests for burn, superburn, and subnet-emission caching.

Burn and emission syncing reuse ingested block rows for their shared meta-epoch
anchor. The SDK's ``FakeBlockchainProvider`` models recovery when that block is
missing, so the scenarios are configured in domain terms and never touch
substrate.
"""

from datetime import UTC, datetime

import pytest
from sentinel.v1.testing.providers import FakeBlockchainProvider

from apps.metagraph.models import Block, Hotkey, MetaEpoch, MetagraphDump, SubnetBurn, SubnetEmission
from apps.metagraph.services.burn_service import BurnService
from apps.metagraph.utils import get_epoch_containing_block
from tests.factories.metagraph import (
    BlockFactory,
    ColdkeyFactory,
    HotkeyFactory,
    MechanismMetricsFactory,
    MetagraphDumpFactory,
    NeuronFactory,
    NeuronSnapshotFactory,
    SubnetFactory,
)

SUPERBURN_COLDKEY = "5D7vUnt4TJ6M8aQbriZCMMkZ8sfYsSJJvRrVnhdWzkArVHDh"
META_EPOCH_TIMESTAMP = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
SOURCE_BLOCK_TIMESTAMP = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)


def epoch_start_for(netuid: int, near_block: int = 8_760_000) -> int:
    """First block of the epoch containing ``near_block`` on ``netuid``."""
    return get_epoch_containing_block(near_block, netuid).start


def make_service(
    *,
    timestamps: dict[int, datetime] | None = None,
) -> tuple[BurnService, TrackingFakeProvider]:
    """Build the service over a fake chain. Blocks with no timestamp read back as unreadable."""
    provider = TrackingFakeProvider()
    for block, timestamp in (timestamps or {}).items():
        provider.with_block_timestamp(block, timestamp)
    return BurnService(provider), provider


class TrackingFakeProvider(FakeBlockchainProvider):
    """Fake chain provider that exposes timestamp reads at the contact boundary."""

    def __init__(self):
        super().__init__()
        self.timestamp_calls: list[int] = []

    def get_block_timestamp(self, block_number: int) -> datetime | None:
        self.timestamp_calls.append(block_number)
        return super().get_block_timestamp(block_number)


def burn_service(provider: FakeBlockchainProvider | None = None) -> BurnService:
    return BurnService(provider)


def build_subnet_with_incentives(
    netuid: int,
    block_number: int,
    incentives: list[tuple[str, int, float]],
    owner_coldkey: str,
):
    """Create a subnet whose owner holds ``owner_coldkey``, plus the given metrics.

    ``incentives`` entries are ``(coldkey, mech_id, incentive)``. The dump that
    production writes alongside those metrics is created too, recording the owner
    as of ``block_number``.
    """
    owner = ColdkeyFactory(coldkey=owner_coldkey)
    owner_hotkey = HotkeyFactory(coldkey=owner)
    subnet = SubnetFactory(netuid=netuid, owner_hotkey=owner_hotkey)
    anchor = BurnService.meta_epoch_block(block_number)
    if anchor >= 0:
        BlockFactory(number=anchor, timestamp=META_EPOCH_TIMESTAMP)
    block = BlockFactory(number=block_number, timestamp=SOURCE_BLOCK_TIMESTAMP)
    MetagraphDumpFactory(netuid=netuid, block=block, owner_hotkey=owner_hotkey, epoch_position=0)

    snapshots: dict[str, object] = {}
    for coldkey_address, mech_id, incentive in incentives:
        if coldkey_address not in snapshots:
            neuron = NeuronFactory(
                subnet=subnet,
                hotkey=HotkeyFactory(coldkey=ColdkeyFactory(coldkey=coldkey_address)),
            )
            snapshots[coldkey_address] = NeuronSnapshotFactory(neuron=neuron, block=block)
        MechanismMetricsFactory(snapshot=snapshots[coldkey_address], mech_id=mech_id, incentive=incentive)
    return subnet


@pytest.mark.django_db
class TestSyncBurn:
    @pytest.fixture(autouse=True)
    def _superburn_coldkey(self, settings):
        settings.METAGRAPH_SUPERBURN_COLDKEY = SUPERBURN_COLDKEY

    def test_averages_owner_incentive_across_mechanisms(self):
        netuid = 7
        block = epoch_start_for(netuid)
        build_subnet_with_incentives(
            netuid,
            block,
            incentives=[
                ("5Owner", 0, 0.4),
                ("5Owner", 1, 0.6),
                ("5Miner", 0, 0.6),
                ("5Miner", 1, 0.4),
            ],
            owner_coldkey="5Owner",
        )
        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        # mech 0 -> 0.4, mech 1 -> 0.6, averaged across both mechanisms.
        assert result.burn == pytest.approx(0.5)
        assert result.superburn == pytest.approx(0.0)
        assert result.source_block_number == block

    def test_superburn_tracks_the_configured_coldkey(self):
        netuid = 7
        block = epoch_start_for(netuid)
        build_subnet_with_incentives(
            netuid,
            block,
            incentives=[
                ("5Owner", 0, 0.25),
                (SUPERBURN_COLDKEY, 0, 0.75),
            ],
            owner_coldkey="5Owner",
        )
        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.25)
        assert result.superburn == pytest.approx(0.75)

    def test_files_the_row_under_the_root_epoch_anchor(self):
        netuid = 7
        block = epoch_start_for(netuid)
        anchor = BurnService.meta_epoch_block(block)
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")

        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.meta_epoch.block_id == anchor
        assert result.meta_epoch.block.timestamp == META_EPOCH_TIMESTAMP

    def test_subnets_on_different_epoch_schedules_share_one_meta_epoch(self):
        first_netuid, second_netuid = 7, 8
        first_block = epoch_start_for(first_netuid)
        second_block = epoch_start_for(second_netuid, near_block=first_block)
        anchor = BurnService.meta_epoch_block(first_block)
        assert BurnService.meta_epoch_block(second_block) == anchor

        build_subnet_with_incentives(first_netuid, first_block, [("5A", 0, 0.1)], owner_coldkey="5A")
        build_subnet_with_incentives(second_netuid, second_block, [("5B", 0, 0.2)], owner_coldkey="5B")
        service = burn_service()

        service.sync_burn(first_block, first_netuid)
        service.sync_burn(second_block, second_netuid)

        # Two subnets, two different source blocks, one shared bucket — which is the
        # whole point of anchoring on the root epoch.
        assert MetaEpoch.objects.count() == 1
        assert SubnetBurn.objects.count() == 2
        assert MetaEpoch.objects.get().block.timestamp == META_EPOCH_TIMESTAMP

    def test_recomputing_the_same_subnet_and_meta_epoch_updates_in_place(self):
        netuid = 7
        block = epoch_start_for(netuid)
        subnet = build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        service = burn_service()
        first = service.sync_burn(block, netuid)
        assert first is not None and first.burn == pytest.approx(0.3)

        # The snapshot is re-dumped with a corrected incentive.
        owner_snapshot = subnet.neurons.get(hotkey__coldkey__coldkey="5Owner").snapshots.get(block_id=block)
        owner_snapshot.mechanism_metrics.filter(mech_id=0).update(incentive=0.9)

        second = service.sync_burn(block, netuid)

        assert second is not None
        assert second.pk == first.pk
        assert SubnetBurn.objects.count() == 1
        assert second.burn == pytest.approx(0.9)

    def test_skips_blocks_that_are_not_the_subnets_epoch_start(self):
        netuid = 7
        block = epoch_start_for(netuid) + 120
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")

        assert burn_service().sync_burn(block, netuid) is None
        assert SubnetBurn.objects.count() == 0

    def test_skips_the_root_subnet(self):
        block = epoch_start_for(0)

        assert burn_service().sync_burn(block, 0) is None
        assert SubnetBurn.objects.count() == 0

    def test_reuses_ingested_anchor_without_reading_a_provider(self):
        netuid = 7
        block = epoch_start_for(netuid)
        anchor = BurnService.meta_epoch_block(block)
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        service, provider = make_service(timestamps={anchor: SOURCE_BLOCK_TIMESTAMP})

        result = service.sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.3)
        assert result.meta_epoch.block.timestamp == META_EPOCH_TIMESTAMP
        assert provider.timestamp_calls == []

    def test_reuses_an_ingested_anchor_with_a_null_timestamp_without_provider_access(self):
        netuid = 7
        block = epoch_start_for(netuid)
        anchor = BurnService.meta_epoch_block(block)
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        Block.objects.filter(number=anchor).update(timestamp=None)
        service, provider = make_service(timestamps={anchor: SOURCE_BLOCK_TIMESTAMP})

        result = service.sync_burn(block, netuid)

        assert result is not None
        assert result.meta_epoch.block.timestamp is None
        assert provider.timestamp_calls == []

    def test_recovers_a_missing_anchor_block_from_the_current_provider(self, monkeypatch):
        netuid = 7
        block = epoch_start_for(netuid)
        anchor = BurnService.meta_epoch_block(block)
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        Block.objects.filter(number=anchor).delete()
        service, provider = make_service(timestamps={anchor: META_EPOCH_TIMESTAMP})
        monkeypatch.setattr(
            "apps.metagraph.services.burn_service.get_archive_provider",
            lambda: pytest.fail("archive provider must not open when the current provider succeeds"),
        )

        result = service.sync_burn(block, netuid)

        assert result is not None
        assert provider.timestamp_calls == [anchor]
        recovered = Block.objects.get(number=anchor)
        assert recovered.timestamp == META_EPOCH_TIMESTAMP
        assert recovered.dump_started_at is None
        assert recovered.dump_finished_at is None
        assert result.meta_epoch.block_id == anchor

    def test_falls_back_to_archive_when_current_provider_cannot_serve_the_anchor(self, monkeypatch):
        netuid = 7
        block = epoch_start_for(netuid)
        anchor = BurnService.meta_epoch_block(block)
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        Block.objects.filter(number=anchor).delete()
        service, provider = make_service()
        archive = TrackingFakeProvider()
        archive.with_block_timestamp(anchor, META_EPOCH_TIMESTAMP)
        monkeypatch.setattr("apps.metagraph.services.burn_service.get_archive_provider", lambda: archive)

        result = service.sync_burn(block, netuid)

        assert result is not None
        assert provider.timestamp_calls == [anchor]
        assert archive.timestamp_calls == [anchor]
        assert result.meta_epoch.block.timestamp == META_EPOCH_TIMESTAMP

    def test_skips_when_no_mechanism_metrics_were_stored(self):
        netuid = 7
        block = epoch_start_for(netuid)
        SubnetFactory(netuid=netuid, owner_hotkey=HotkeyFactory())

        assert burn_service().sync_burn(block, netuid) is None
        assert SubnetBurn.objects.count() == 0

    def test_records_zero_burn_when_the_dump_recorded_no_owner(self):
        netuid = 7
        block = epoch_start_for(netuid)
        BlockFactory(number=BurnService.meta_epoch_block(block), timestamp=META_EPOCH_TIMESTAMP)
        subnet = SubnetFactory(netuid=netuid, owner_hotkey=None)
        source_block = BlockFactory(number=block, timestamp=SOURCE_BLOCK_TIMESTAMP)
        MetagraphDumpFactory(netuid=netuid, block=source_block, owner_hotkey=None, epoch_position=0)
        snapshot = NeuronSnapshotFactory(
            neuron=NeuronFactory(subnet=subnet, hotkey=HotkeyFactory()),
            block=source_block,
        )
        MechanismMetricsFactory(snapshot=snapshot, mech_id=0, incentive=0.8)

        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.0)

    def test_attributes_burn_to_the_owner_recorded_at_the_source_block(self):
        """A later change of hands must not re-attribute an already-dumped epoch."""
        netuid = 7
        block = epoch_start_for(netuid)
        subnet = build_subnet_with_incentives(
            netuid,
            block,
            incentives=[("5OldOwner", 0, 0.4), ("5NewOwner", 0, 0.6)],
            owner_coldkey="5OldOwner",
        )
        # The subnet is sold: later dumps rewrite Subnet.owner_hotkey, the dump
        # of `block` keeps the identity that held the incentive back then.
        subnet.owner_hotkey = HotkeyFactory(coldkey=ColdkeyFactory(coldkey="5NewOwner"))
        subnet.save()

        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.4)

    def test_a_coldkey_swap_keeps_the_burn_with_the_owner_hotkey(self):
        """A swap moves every hotkey of a coldkey at once, so the dumped hotkey still resolves it."""
        netuid = 7
        block = epoch_start_for(netuid)
        build_subnet_with_incentives(
            netuid,
            block,
            incentives=[("5OwnerColdkey", 0, 0.4), ("5Miner", 0, 0.6)],
            owner_coldkey="5OwnerColdkey",
        )
        swapped_to = ColdkeyFactory(coldkey="5SwappedColdkey")
        Hotkey.objects.filter(coldkey__coldkey="5OwnerColdkey").update(coldkey=swapped_to)

        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.4)

    def test_skips_when_no_dump_recorded_the_owner_at_the_block(self):
        """Without provenance, guessing the owner would persist a wrong burn forever."""
        netuid = 7
        block = epoch_start_for(netuid)
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        MetagraphDump.objects.filter(netuid=netuid, block_id=block).delete()

        assert burn_service().sync_burn(block, netuid) is None
        assert SubnetBurn.objects.count() == 0

    def test_records_zero_superburn_when_no_coldkey_is_configured(self, settings):
        settings.METAGRAPH_SUPERBURN_COLDKEY = ""
        netuid = 7
        block = epoch_start_for(netuid)
        build_subnet_with_incentives(
            netuid,
            block,
            [("5Owner", 0, 0.25), (SUPERBURN_COLDKEY, 0, 0.75)],
            owner_coldkey="5Owner",
        )

        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.25)
        assert result.superburn == pytest.approx(0.0)

    def test_ignores_incentives_from_other_subnets(self):
        netuid, other_netuid = 7, 8
        block = epoch_start_for(netuid)
        owner = ColdkeyFactory(coldkey="5Owner")
        build_subnet_with_incentives(netuid, block, [("5Owner", 0, 0.2)], owner_coldkey="5Owner")

        other_subnet = SubnetFactory(netuid=other_netuid)
        other_snapshot = NeuronSnapshotFactory(
            neuron=NeuronFactory(subnet=other_subnet, hotkey=HotkeyFactory(coldkey=owner)),
            block=BlockFactory(number=block),
        )
        MechanismMetricsFactory(snapshot=other_snapshot, mech_id=0, incentive=0.9)

        result = burn_service().sync_burn(block, netuid)

        assert result is not None
        assert result.burn == pytest.approx(0.2)


@pytest.mark.django_db
class TestSyncSubnetEmissions:
    def test_stores_one_row_per_subnet_and_creates_missing_subnets(self):
        anchor = epoch_start_for(0)
        BlockFactory(number=anchor, timestamp=META_EPOCH_TIMESTAMP)
        SubnetFactory(netuid=1, name="existing")
        service, provider = make_service()
        provider.with_subnet_emission_enabled(anchor, {1: True, 2: False, 3: True})

        emissions = service.sync_subnet_emissions(anchor)

        assert len(emissions) == 3
        stored = dict(SubnetEmission.objects.values_list("subnet_id", "emission_enabled"))
        assert stored == {1: True, 2: False, 3: True}
        assert MetaEpoch.objects.get().block.timestamp == META_EPOCH_TIMESTAMP
        assert provider.timestamp_calls == []

    def test_mixed_enabled_and_disabled_subnets_are_all_recorded(self):
        anchor = epoch_start_for(0)
        service, provider = make_service(timestamps={anchor: META_EPOCH_TIMESTAMP})
        provider.with_subnet_emission_enabled(anchor, {netuid: netuid % 2 == 0 for netuid in range(1, 9)})

        service.sync_subnet_emissions(anchor)

        stored = dict(SubnetEmission.objects.values_list("subnet_id", "emission_enabled"))
        assert stored == {1: False, 2: True, 3: False, 4: True, 5: False, 6: True, 7: False, 8: True}

    def test_resampling_the_same_meta_epoch_updates_in_place(self):
        anchor = epoch_start_for(0)
        service, provider = make_service(timestamps={anchor: META_EPOCH_TIMESTAMP})
        provider.with_subnet_emission_enabled(anchor, {1: True, 2: True})
        service.sync_subnet_emissions(anchor)

        provider.with_subnet_emission_enabled(anchor, {1: False, 2: True})
        service.sync_subnet_emissions(anchor)

        stored = dict(SubnetEmission.objects.values_list("subnet_id", "emission_enabled"))
        assert stored == {1: False, 2: True}
        assert SubnetEmission.objects.count() == 2

    def test_attaches_to_a_meta_epoch_the_burn_sync_already_created(self):
        """Backfill order: burn anchors the bucket first, emissions file into the same one."""
        burn_netuid = 7
        source_block = epoch_start_for(burn_netuid)
        anchor = BurnService.meta_epoch_block(source_block)
        build_subnet_with_incentives(burn_netuid, source_block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        burn_service().sync_burn(source_block, burn_netuid)
        assert MetaEpoch.objects.count() == 1

        service, provider = make_service(timestamps={anchor: META_EPOCH_TIMESTAMP})
        provider.with_subnet_emission_enabled(anchor, {1: True, 2: False})
        emissions = service.sync_subnet_emissions(anchor)

        assert len(emissions) == 2
        assert MetaEpoch.objects.count() == 1, "must reuse the bucket, not create a second one"
        assert MetaEpoch.objects.get().block.timestamp == META_EPOCH_TIMESTAMP
        assert provider.timestamp_calls == []
        assert SubnetEmission.objects.filter(meta_epoch__block__number=anchor).count() == 2

    def test_an_unreadable_timestamp_cannot_discard_an_already_anchored_bucket(self):
        """The existing-row check must come before the timestamp read."""
        burn_netuid = 7
        source_block = epoch_start_for(burn_netuid)
        anchor = BurnService.meta_epoch_block(source_block)
        build_subnet_with_incentives(burn_netuid, source_block, [("5Owner", 0, 0.3)], owner_coldkey="5Owner")
        burn_service().sync_burn(source_block, burn_netuid)

        # No timestamp configured for the anchor: it must not be requested.
        service, provider = make_service()
        provider.with_subnet_emission_enabled(anchor, {1: True, 2: False})

        assert len(service.sync_subnet_emissions(anchor)) == 2
        assert SubnetEmission.objects.count() == 2
        assert provider.timestamp_calls == []

    def test_skips_blocks_that_do_not_start_a_meta_epoch(self):
        block = epoch_start_for(0) + 1
        service, _ = make_service()

        assert service.sync_subnet_emissions(block) == []
        assert SubnetEmission.objects.count() == 0

    def test_unreadable_chain_state_records_nothing_rather_than_a_guess(self):
        """Half a map would read as "everything enabled"; record nothing instead."""
        anchor = epoch_start_for(0)
        service, _ = make_service(timestamps={epoch_start_for(0): META_EPOCH_TIMESTAMP})

        assert service.sync_subnet_emissions(anchor) == []
        assert SubnetEmission.objects.count() == 0
        assert MetaEpoch.objects.count() == 0

    def test_the_root_subnet_is_not_recorded(self):
        """The chain reports netuid 0; it only supplies the epoch schedule, so drop it."""
        anchor = epoch_start_for(0)
        service, provider = make_service(timestamps={anchor: META_EPOCH_TIMESTAMP})
        provider.with_subnet_emission_enabled(anchor, {0: True, 1: True, 2: False})

        emissions = service.sync_subnet_emissions(anchor)

        assert len(emissions) == 2
        assert dict(SubnetEmission.objects.values_list("subnet_id", "emission_enabled")) == {1: True, 2: False}

    def test_skips_when_both_providers_cannot_recover_a_missing_anchor(self, monkeypatch):
        anchor = epoch_start_for(0)
        service, provider = make_service()
        archive = TrackingFakeProvider()
        monkeypatch.setattr("apps.metagraph.services.burn_service.get_archive_provider", lambda: archive)
        provider.with_subnet_emission_enabled(anchor, {1: True})

        assert service.sync_subnet_emissions(anchor) == []
        assert SubnetEmission.objects.count() == 0
        assert MetaEpoch.objects.count() == 0
        assert Block.objects.filter(number=anchor).exists() is False
        assert provider.timestamp_calls == [anchor]
        assert archive.timestamp_calls == [anchor]

    def test_skips_emissions_when_the_provider_does_not_support_them(self):
        class UnsupportedProvider(FakeBlockchainProvider):
            def get_subnet_emission_enabled(self, block_number: int) -> dict[int, bool] | None:
                raise NotImplementedError

        anchor = epoch_start_for(0)

        assert BurnService(UnsupportedProvider()).sync_subnet_emissions(anchor) == []
        assert SubnetEmission.objects.count() == 0
        assert MetaEpoch.objects.count() == 0


class TestEpochAnchoring:
    def test_meta_epoch_block_is_the_root_epoch_start(self):
        anchor = epoch_start_for(0)
        assert BurnService.meta_epoch_block(anchor) == anchor
        assert BurnService.meta_epoch_block(anchor + 359) == anchor
        assert BurnService.meta_epoch_block(anchor + 361) == anchor + 361

    def test_is_meta_epoch_start_only_for_the_anchor_block(self):
        anchor = epoch_start_for(0)
        assert BurnService.is_meta_epoch_start(anchor)
        assert not BurnService.is_meta_epoch_start(anchor + 1)
        assert not BurnService.is_meta_epoch_start(anchor - 1)

    def test_every_subnet_epoch_start_falls_inside_one_meta_epoch(self):
        near = 8_760_000
        anchors = {BurnService.meta_epoch_block(epoch_start_for(netuid, near)) for netuid in range(1, 129)}
        # 128 subnet epoch starts spread over at most two adjacent root epochs.
        assert len(anchors) <= 2
