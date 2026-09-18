"""Per-subnet burn, superburn, and emission-enabled metrics.

Three cached tables, all keyed on :class:`~apps.metagraph.models.MetaEpoch` so
that values from 128 subnets on 128 different epoch schedules line up on one
time axis:

* ``burn`` — the share of a subnet's incentive that flows to the subnet owner's
  own coldkey.
* ``superburn`` — the same measurement for ``settings.METAGRAPH_SUPERBURN_COLDKEY``.
* ``emission_enabled`` — the chain's ``SubnetEmissionEnabled`` flag.

Burn and superburn are computed from mechanism metrics already stored by the
metagraph sync, sampled at each subnet's epoch-start block, and attributed to the
owner that the block's ``MetagraphDump`` recorded — never to the subnet's current
owner, which would move historical rows whenever a subnet changes hands.
Emission-enabled is read from chain storage once per meta epoch, at the meta
epoch's own first block.

Both paths resolve the shared root-epoch anchor through the ingested ``Block``
table. A missing anchor is recovered from the current provider, with the archive
provider as a fallback, and stored without dump metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, FloatField, Sum, Value, When
from sentinel.v1.providers.base import BlockchainProvider

from apps.metagraph import utils as metagraph_utils
from apps.metagraph.models import (
    Block,
    MechanismMetrics,
    MetaEpoch,
    MetagraphDump,
    Subnet,
    SubnetBurn,
    SubnetEmission,
)
from project.core.utils import get_archive_provider

logger = structlog.get_logger()

ROOT_NETUID = 0


@dataclass(frozen=True)
class MechanismShares:
    """Per-mechanism incentive shares for the two tracked coldkeys."""

    burn: float
    superburn: float


class BurnService:
    """Computes and caches burn, superburn, and subnet-emission rows.

    ``provider`` reads emission state and is also the first fallback for a
    missing meta-epoch anchor block. The archive provider is tried only when the
    current provider cannot return that block's timestamp.
    """

    def __init__(self, provider: BlockchainProvider | None = None) -> None:
        self._provider = provider

    # Epoch helpers

    @staticmethod
    def is_subnet_epoch_start(block_number: int, netuid: int) -> bool:
        """True only for the first block of the subnet's own epoch."""
        return block_number == metagraph_utils.get_epoch_containing_block(block_number, netuid).start

    @classmethod
    def is_meta_epoch_start(cls, block_number: int) -> bool:
        """True only for the first block of a root-subnet epoch."""
        return cls.is_subnet_epoch_start(block_number, ROOT_NETUID)

    @staticmethod
    def meta_epoch_block(block_number: int) -> int:
        """Return the root-epoch start block that anchors ``block_number``.

        Negative for blocks before the chain's first root-epoch boundary (block
        359) — those blocks belong to a root epoch that never started. This is
        only reachable on a freshly created chain such as the e2e localnet.
        """
        return metagraph_utils.get_epoch_containing_block(block_number, ROOT_NETUID).start

    def meta_epoch_for_block(self, block_number: int) -> MetaEpoch | None:
        """Return the root meta epoch containing ``block_number``.

        The anchor normally already exists in the ingested block table. Only a
        missing block triggers chain access; the current provider is tried first
        and the archive provider second.
        """
        anchor = self.meta_epoch_block(block_number)
        if anchor < 0:
            return None

        existing = MetaEpoch.objects.select_related("block").filter(block_id=anchor).first()
        if existing is not None:
            return existing

        block = Block.objects.filter(number=anchor).first()
        if block is None:
            timestamp = self._recover_block_timestamp(anchor)
            if timestamp is None:
                return None
            block, _ = Block.objects.get_or_create(number=anchor, defaults={"timestamp": timestamp})

        meta_epoch, _ = MetaEpoch.objects.select_related("block").get_or_create(block=block)
        return meta_epoch

    def _recover_block_timestamp(self, block_number: int) -> datetime | None:
        """Read a missing block timestamp from the current node, then archive."""
        if self._provider is not None:
            timestamp = self._timestamp_from_provider(self._provider, block_number, source="current")
            if timestamp is not None:
                return timestamp

        try:
            with get_archive_provider() as archive_provider:
                return self._timestamp_from_provider(archive_provider, block_number, source="archive")
        except Exception as exc:
            logger.warning(
                "Could not open archive provider for missing meta-epoch block",
                block=block_number,
                error=str(exc),
            )
            return None

    @staticmethod
    def _timestamp_from_provider(
        provider: BlockchainProvider,
        block_number: int,
        *,
        source: str,
    ) -> datetime | None:
        try:
            timestamp = provider.get_block_timestamp(block_number)
        except Exception as exc:
            logger.warning(
                "Could not read missing meta-epoch block timestamp",
                block=block_number,
                provider=source,
                error=str(exc),
            )
            return None
        if timestamp is None:
            logger.warning(
                "Missing meta-epoch block timestamp unavailable",
                block=block_number,
                provider=source,
            )
        return timestamp

    # Burn / superburn

    def sync_burn(
        self,
        block_number: int,
        netuid: int,
    ) -> SubnetBurn | None:
        """Cache burn and superburn for a subnet at one of its epoch-start blocks.

        Burn is attributed to the owner recorded on the *dump* of that block, not
        to the subnet's current owner: the same computation runs live and months
        later from ``backfill_burn_metrics``, and an ownership or coldkey change
        in between must not rewrite history.

        Returns None — without writing anything — when the block is not the
        subnet's epoch start, when the subnet is the root subnet, when no
        mechanism metrics were stored for that block, or when no dump recorded
        who owned the subnet at that block.
        """
        log = logger.bind(block=block_number, netuid=netuid)

        if netuid == ROOT_NETUID:
            log.debug("Skipping burn for root subnet")
            return None
        if not self.is_subnet_epoch_start(block_number, netuid):
            log.debug("Skipping burn for non-epoch-start block")
            return None
        if self.meta_epoch_block(block_number) < 0:
            log.debug("Skipping burn, block precedes the chain's first root epoch")
            return None

        subnet = Subnet.objects.filter(netuid=netuid).first()
        if subnet is None:
            log.warning("Skipping burn, subnet not stored")
            return None

        # Every path that writes the snapshots this reads writes the dump in the
        # same transaction, so a missing dump means the block's provenance is gone.
        # Skip rather than fall back to the subnet's current owner, which would
        # silently attribute an old epoch's incentive to today's owner.
        dump = MetagraphDump.objects.select_related("owner_hotkey").filter(netuid=netuid, block_id=block_number).first()
        if dump is None:
            log.warning("Skipping burn, no dump recorded the subnet owner at this block")
            return None

        owner_coldkey_id = dump.owner_hotkey.coldkey_id if dump.owner_hotkey else None
        shares = self._per_mechanism_shares(
            block_number=block_number,
            netuid=netuid,
            owner_coldkey_id=owner_coldkey_id,
            superburn_coldkey=settings.METAGRAPH_SUPERBURN_COLDKEY,
        )
        if not shares:
            log.warning("Skipping burn, no mechanism metrics stored for block")
            return None

        burn = _clamp(sum(share.burn for share in shares) / len(shares))
        superburn = _clamp(sum(share.superburn for share in shares) / len(shares))

        meta_epoch = self.meta_epoch_for_block(block_number)
        if meta_epoch is None:
            log.warning("Skipping burn, meta-epoch anchor block unavailable")
            return None

        with transaction.atomic():
            subnet_burn, _ = SubnetBurn.objects.update_or_create(
                subnet=subnet,
                meta_epoch=meta_epoch,
                defaults={
                    "source_block_number": block_number,
                    "burn": burn,
                    "superburn": superburn,
                },
            )
        log.info(
            "Synced subnet burn",
            meta_epoch_block=meta_epoch.block_id,
            mechanisms=len(shares),
            burn=burn,
            superburn=superburn,
        )
        return subnet_burn

    @staticmethod
    def _per_mechanism_shares(
        block_number: int,
        netuid: int,
        owner_coldkey_id: int | None,
        superburn_coldkey: str | None,
    ) -> list[MechanismShares]:
        """Sum each tracked coldkey's incentive per mechanism present in the dump.

        One row per mechanism, so a subnet with several mechanisms contributes
        each of them equally to the averaged burn.
        """
        owner_incentive: Case | Value = Value(0.0, output_field=FloatField())
        if owner_coldkey_id is not None:
            owner_incentive = Case(
                When(snapshot__neuron__hotkey__coldkey_id=owner_coldkey_id, then=F("incentive")),
                default=Value(0.0),
                output_field=FloatField(),
            )

        superburn_incentive: Case | Value = Value(0.0, output_field=FloatField())
        if superburn_coldkey:
            superburn_incentive = Case(
                When(snapshot__neuron__hotkey__coldkey__coldkey=superburn_coldkey, then=F("incentive")),
                default=Value(0.0),
                output_field=FloatField(),
            )

        rows = (
            MechanismMetrics.objects.filter(
                snapshot__block_id=block_number,
                snapshot__neuron__subnet_id=netuid,
            )
            .values("mech_id")
            .annotate(
                burn=Sum(owner_incentive, default=0.0),
                superburn=Sum(superburn_incentive, default=0.0),
            )
            .order_by("mech_id")
        )
        return [MechanismShares(burn=row["burn"] or 0.0, superburn=row["superburn"] or 0.0) for row in rows]

    # Subnet emissions

    def sync_subnet_emissions(self, block_number: int) -> list[SubnetEmission]:
        """Cache ``SubnetEmissionEnabled`` for every subnet at a meta-epoch start block.

        Returns an empty list — without writing anything — when the block does
        not start a root epoch or when the chain state could not be read.
        """
        log = logger.bind(block=block_number)
        if not self.is_meta_epoch_start(block_number):
            log.debug("Skipping subnet emissions for non-meta-epoch-start block")
            return []
        if self._provider is None:
            raise ValueError("sync_subnet_emissions needs a provider; BurnService was built without one")

        try:
            chain_values = self._provider.get_subnet_emission_enabled(block_number)
        except NotImplementedError:
            log.warning("Skipping subnet emissions, provider does not support the chain read")
            return []
        if chain_values is None:
            log.warning("Skipping subnet emissions, chain state unavailable")
            return []

        # The root subnet has no burn/emission dashboard row of its own; it only
        # supplies the epoch schedule everything else is anchored to.
        enabled_by_netuid = {netuid: enabled for netuid, enabled in chain_values.items() if netuid != ROOT_NETUID}

        meta_epoch = self.meta_epoch_for_block(block_number)
        if meta_epoch is None:
            log.warning("Skipping subnet emissions, meta-epoch anchor block unavailable")
            return []

        with transaction.atomic():
            Subnet.objects.bulk_create(
                [Subnet(netuid=netuid) for netuid in sorted(enabled_by_netuid)],
                ignore_conflicts=True,
            )
            subnets_by_netuid = Subnet.objects.in_bulk(list(enabled_by_netuid))
            emissions = [
                SubnetEmission(
                    subnet=subnets_by_netuid[netuid],
                    meta_epoch=meta_epoch,
                    emission_enabled=enabled,
                )
                for netuid, enabled in sorted(enabled_by_netuid.items())
            ]
            SubnetEmission.objects.bulk_create(
                emissions,
                update_conflicts=True,
                update_fields=["emission_enabled"],
                unique_fields=["subnet", "meta_epoch"],
            )

        log.info(
            "Synced subnet emissions",
            meta_epoch_block=meta_epoch.block_id,
            subnets=len(emissions),
            enabled=sum(1 for emission in emissions if emission.emission_enabled),
        )
        return emissions


def _clamp(value: float) -> float:
    """Keep float-summation noise inside the [0, 1] range the columns are constrained to."""
    return min(1.0, max(0.0, value))
