"""Service for syncing metagraph data to Django models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from django.db import transaction
from django.utils import timezone

from apps.metagraph.models import (
    Block,
    MechanismMetrics,
    MetagraphDump,
    Neuron,
    NeuronSnapshot,
    Subnet,
)
from apps.metagraph.services.key_cache import KeyCacheService
from apps.metagraph.services.relation_bulk_syncer import RelationBulkSyncer

if TYPE_CHECKING:
    from sentinel.v1.services.extractors.metagraph.dto import (
        Block as BlockDTO,
    )
    from sentinel.v1.services.extractors.metagraph.dto import (
        FullSubnetSnapshot,
        NeuronSnapshotFull,
        NeuronWithRelations,
        SubnetWithOwner,
    )
    from sentinel.v1.services.extractors.metagraph.dto import (
        MechanismMetrics as MechMetricsDTO,
    )

logger = structlog.get_logger()

# Conversion factor from TAO to rao (1 TAO = 10^9 rao)
TAO_TO_RAO = 10**9


@dataclass
class DumpMetadata:
    """Metadata for metagraph dump tracking."""

    netuid: int
    epoch_position: str  # "start", "inside", "end"
    started_at: datetime
    finished_at: datetime


def _to_rao(tao_value: float | str | Decimal | None) -> int:
    """Convert TAO value to rao (integer)."""
    if tao_value is None:
        return 0
    return int(Decimal(str(tao_value)) * TAO_TO_RAO)


class MetagraphSyncService:
    """Service to sync metagraph snapshot data to Django models."""

    def __init__(self) -> None:
        self._key_cache = KeyCacheService()
        self._neuron_cache: dict[tuple[int, int], Neuron] = {}  # (hotkey_id, subnet_netuid)
        self._relation_syncer = RelationBulkSyncer(self._neuron_cache)

    def sync_metagraph(
        self,
        metagraph: FullSubnetSnapshot,
        dump_metadata: DumpMetadata,
    ) -> dict[str, int]:
        """
        Sync a FullSubnetSnapshot to Django models.

        Args:
            metagraph: FullSubnetSnapshot from sentinel SDK
            dump_metadata: Metadata for tracking the dump

        Returns:
            Dict with counts of created/updated records

        """
        stats = {
            "coldkeys": 0,
            "hotkeys": 0,
            "evmkeys": 0,
            "subnets": 0,
            "blocks": 0,
            "neurons": 0,
            "snapshots": 0,
            "mechanism_metrics": 0,
            "weights": 0,
            "bonds": 0,
            "collaterals": 0,
            "dumps": 0,
        }

        with transaction.atomic():
            # 1. Sync block
            block = self._sync_block(metagraph.block, dump_metadata)
            stats["blocks"] = 1

            # 2. Sync subnet
            subnet = self._sync_subnet(metagraph.subnet)
            stats["subnets"] = 1

            # 3. Sync neurons and their snapshots
            for neuron_snapshot in metagraph.neurons:
                neuron_rel = neuron_snapshot.neuron
                if neuron_rel.hotkey:
                    if neuron_rel.hotkey.coldkey:
                        self._key_cache.get_or_create_coldkey(neuron_rel.hotkey.coldkey.coldkey)
                        stats["coldkeys"] += 1
                    self._key_cache.get_or_create_hotkey(
                        neuron_rel.hotkey.hotkey,
                        {"coldkey": neuron_rel.hotkey.coldkey.coldkey} if neuron_rel.hotkey.coldkey else None,
                    )
                    stats["hotkeys"] += 1

                if neuron_rel.evm_key:
                    self._key_cache.get_or_create_evmkey(neuron_rel.evm_key.evm_address)
                    stats["evmkeys"] += 1

                neuron = self._sync_neuron(neuron_rel, subnet)
                stats["neurons"] += 1

                snapshot = self._sync_neuron_snapshot(neuron_snapshot, neuron, block)
                stats["snapshots"] += 1

                for mech in neuron_snapshot.mechanisms:
                    self._sync_mechanism_metrics(mech, snapshot)
                    stats["mechanism_metrics"] += 1

            # 4. Sync weights
            if metagraph.weights:
                stats["weights"] = self._relation_syncer.sync_weights(metagraph.weights, block, subnet)

            # 5. Sync bonds
            if metagraph.bonds:
                stats["bonds"] = self._relation_syncer.sync_bonds(metagraph.bonds, block, subnet)

            # 6. Sync collaterals
            if metagraph.collaterals:
                stats["collaterals"] = self._relation_syncer.sync_collaterals(metagraph.collaterals, block, subnet)

            # 7. Sync metagraph dump record
            self._sync_metagraph_dump(dump_metadata, block, subnet)
            stats["dumps"] = 1

            # 8. Bulk update last_seen for all hotkeys
            self._key_cache.flush_hotkey_last_seen()

        return stats

    def _sync_block(self, block_model: BlockDTO, dump_metadata: DumpMetadata) -> Block:
        """Sync a Block record."""
        block, created = Block.objects.get_or_create(
            number=block_model.block_number,
            defaults={
                "timestamp": block_model.timestamp,
                "dump_started_at": dump_metadata.started_at,
                "dump_finished_at": dump_metadata.finished_at,
            },
        )

        if not created:
            update_fields = []
            if block_model.timestamp and not block.timestamp:
                block.timestamp = block_model.timestamp
                update_fields.append("timestamp")
            if dump_metadata.started_at and not block.dump_started_at:
                block.dump_started_at = dump_metadata.started_at
                update_fields.append("dump_started_at")
            if dump_metadata.finished_at and not block.dump_finished_at:
                block.dump_finished_at = dump_metadata.finished_at
                update_fields.append("dump_finished_at")
            if update_fields:
                block.save(update_fields=update_fields)

        return block

    def _sync_subnet(self, subnet_model: SubnetWithOwner) -> Subnet:
        """Sync a Subnet record."""
        owner_hotkey = None
        if subnet_model.owner_hotkey:
            coldkey_data = None
            if subnet_model.owner_hotkey.coldkey:
                self._key_cache.get_or_create_coldkey(subnet_model.owner_hotkey.coldkey.coldkey)
                coldkey_data = {"coldkey": subnet_model.owner_hotkey.coldkey.coldkey}
            owner_hotkey = self._key_cache.get_or_create_hotkey(
                subnet_model.owner_hotkey.hotkey,
                coldkey_data,
            )

        registered_at = subnet_model.registered_at
        if registered_at and timezone.is_naive(registered_at):
            registered_at = timezone.make_aware(registered_at)

        alpha_out_emission_rao = _to_rao(subnet_model.alpha_out_emission)

        subnet, created = Subnet.objects.get_or_create(
            netuid=subnet_model.netuid,
            defaults={
                "name": subnet_model.name or "",
                "owner_hotkey": owner_hotkey,
                "registered_at": registered_at,
                "alpha_out_emission": alpha_out_emission_rao,
                "tempo": subnet_model.tempo,
                "moving_price": subnet_model.moving_price,
            },
        )

        if not created:
            updated = False
            if subnet_model.name and subnet.name != subnet_model.name:
                subnet.name = subnet_model.name
                updated = True
            if owner_hotkey and subnet.owner_hotkey_id != owner_hotkey.id:
                subnet.owner_hotkey = owner_hotkey
                updated = True
            if alpha_out_emission_rao and subnet.alpha_out_emission != alpha_out_emission_rao:
                subnet.alpha_out_emission = alpha_out_emission_rao
                updated = True
            # Guard: 0 is the model default / sentinel-for-missing. Do not
            # overwrite a real persisted tempo/moving_price with a missing-data
            # zero from the SDK (mirrors the alpha_out_emission guard above).
            if subnet_model.tempo and subnet.tempo != subnet_model.tempo:
                subnet.tempo = subnet_model.tempo
                updated = True
            if subnet_model.moving_price and subnet.moving_price != subnet_model.moving_price:
                subnet.moving_price = subnet_model.moving_price
                updated = True
            if updated:
                subnet.save()

        return subnet

    def _sync_neuron(self, neuron_model: NeuronWithRelations, subnet: Subnet) -> Neuron:
        """Sync a Neuron record."""
        hotkey = self._key_cache.get_cached_hotkey(neuron_model.hotkey.hotkey)
        if not hotkey:
            coldkey_data = None
            if neuron_model.hotkey.coldkey:
                coldkey_data = {"coldkey": neuron_model.hotkey.coldkey.coldkey}
            hotkey = self._key_cache.get_or_create_hotkey(neuron_model.hotkey.hotkey, coldkey_data)

        cache_key = (hotkey.id, subnet.netuid)
        if cache_key in self._neuron_cache:
            return self._neuron_cache[cache_key]

        evm_key = None
        if neuron_model.evm_key:
            evm_key = self._key_cache.get_or_create_evmkey(neuron_model.evm_key.evm_address)

        neuron, _ = Neuron.objects.get_or_create(
            hotkey=hotkey,
            subnet=subnet,
            defaults={
                "uid": neuron_model.uid,
                "evm_key": evm_key,
            },
        )

        updated = False
        if neuron.uid != neuron_model.uid:
            neuron.uid = neuron_model.uid
            updated = True
        if evm_key and neuron.evm_key_id != evm_key.id:
            neuron.evm_key = evm_key
            updated = True
        if updated:
            neuron.save()

        self._neuron_cache[cache_key] = neuron
        return neuron

    def _sync_neuron_snapshot(
        self,
        snapshot_model: NeuronSnapshotFull,
        neuron: Neuron,
        block: Block,
    ) -> NeuronSnapshot:
        """Sync a NeuronSnapshot record."""
        snapshot, _ = NeuronSnapshot.objects.update_or_create(
            neuron=neuron,
            block=block,
            defaults={
                "uid": snapshot_model.uid,
                "axon_address": snapshot_model.axon_address or "",
                "total_stake": _to_rao(snapshot_model.total_stake),
                "alpha_stake": _to_rao(snapshot_model.alpha_stake),
                "alpha_dividends": _to_rao(snapshot_model.alpha_dividends),
                "tao_dividends": _to_rao(snapshot_model.tao_dividends),
                "normalized_stake": snapshot_model.normalized_stake,
                "rank": snapshot_model.rank,
                "trust": snapshot_model.trust,
                "emissions": _to_rao(snapshot_model.emissions),
                "is_active": snapshot_model.is_active,
                "is_validator": snapshot_model.is_validator,
                "is_immune": snapshot_model.is_immune,
                "has_any_weights": snapshot_model.has_any_weights,
                "neuron_version": snapshot_model.neuron_version,
                "block_at_registration": snapshot_model.block_at_registration,
            },
        )
        return snapshot

    def _sync_mechanism_metrics(
        self,
        mech_model: MechMetricsDTO,
        snapshot: NeuronSnapshot,
    ) -> MechanismMetrics:
        """Sync a MechanismMetrics record."""
        metrics, _ = MechanismMetrics.objects.update_or_create(
            snapshot=snapshot,
            mech_id=mech_model.mech_id,
            defaults={
                "incentive": mech_model.incentive,
                "dividend": mech_model.dividend,
                "consensus": mech_model.consensus,
                "validator_trust": mech_model.validator_trust,
                "weights_sum": mech_model.weights_sum,
                "last_update": mech_model.last_update,
            },
        )
        return metrics

    def _sync_metagraph_dump(
        self,
        dump_metadata: DumpMetadata,
        block: Block,
        subnet: Subnet,
    ) -> MetagraphDump:
        """Sync a MetagraphDump record."""
        epoch_position_map = {"start": 0, "inside": 1, "end": 2}
        epoch_position = epoch_position_map.get(dump_metadata.epoch_position)

        dump, _ = MetagraphDump.objects.update_or_create(
            netuid=dump_metadata.netuid,
            block=block,
            defaults={
                "epoch_position": epoch_position,
                "started_at": dump_metadata.started_at,
                "finished_at": dump_metadata.finished_at,
                # Snapshot the tempo the epoch actually ran with; Subnet.tempo
                # is mutable and later changes must not rewrite history.
                "tempo": subnet.tempo,
            },
        )
        return dump
