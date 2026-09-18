"""Backfill burn/superburn (and optionally subnet-emission) rows over a block range.

Burn and superburn are functions of the mechanism metrics the metagraph sync
already stored plus the owner recorded on that block's dump, so this recovers
history for every epoch-start block still inside the snapshot retention window —
enough to fill the Burns and Emissions dashboard the moment the feature is
deployed. Blocks whose dump is gone are skipped: attributing an old epoch's
incentive to the subnet's present owner would persist a wrong value.

The burn pass normally touches no chain connection: its meta-epoch anchor blocks
are expected to have been ingested already. If an anchor is missing, the burn
service recovers that one block from the archive. Subnet-emission history cannot
be recovered from snapshots, so ``--emissions`` re-reads it from chain storage
and needs an archive node for anything older than the pruning window of a regular
node.
"""

import signal

import structlog
from django.core.management.base import BaseCommand, CommandError

from apps.metagraph.models import NeuronSnapshot
from apps.metagraph.services.burn_service import ROOT_NETUID, BurnService
from apps.metagraph.services.metagraph_service import MetagraphService
from apps.metagraph.utils import epoch_start_blocks_in_range
from project.core.utils import get_archive_provider

logger = structlog.get_logger()


class Command(BaseCommand):
    help = "Recompute burn/superburn rows (and optionally re-read subnet emissions) for a block range."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown = False

    def _handle_signal(self, signum, frame):
        logger.info("Received shutdown signal, stopping after current block", signal=signal.Signals(signum).name)
        self._shutdown = True

    def add_arguments(self, parser):
        parser.add_argument("--from-block", type=int, required=True, help="First block of the range (inclusive).")
        parser.add_argument("--to-block", type=int, required=True, help="Last block of the range (inclusive).")
        parser.add_argument(
            "--netuid",
            type=int,
            action="append",
            dest="netuids",
            help="Subnet to backfill; repeatable. Default: the configured metagraph netuids.",
        )
        parser.add_argument(
            "--emissions",
            action="store_true",
            help="Also re-read SubnetEmissionEnabled per meta epoch from the chain (needs an archive node).",
        )
        parser.add_argument(
            "--skip-burn",
            action="store_true",
            help="Skip the burn/superburn pass (useful with --emissions to only backfill emissions).",
        )

    def handle(self, *args, **options):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        from_block: int = options["from_block"]
        to_block: int = options["to_block"]
        if from_block > to_block:
            raise CommandError("--from-block must not be greater than --to-block")

        netuids = options["netuids"] or MetagraphService.netuids_to_sync()
        netuids = [netuid for netuid in netuids if netuid != ROOT_NETUID]

        # Burn reads stored snapshots and blocks. The service opens an archive
        # connection lazily only if a root-epoch anchor block is missing.
        if not options["skip_burn"]:
            self._backfill_burn(BurnService(), netuids, from_block, to_block)

        if options["emissions"]:
            with get_archive_provider() as provider:
                self._backfill_emissions(BurnService(provider), from_block, to_block)

    def _backfill_burn(self, service: BurnService, netuids: list[int], from_block: int, to_block: int) -> None:
        total = 0
        for netuid in netuids:
            if self._shutdown:
                break

            candidates = epoch_start_blocks_in_range(from_block, to_block, netuid)
            if not candidates:
                continue

            # Bound by block range rather than an IN-list of ~thousands of epoch
            # starts: the range folds into the index condition, the IN-list would
            # be re-checked per row (see apps.metagraph.tasks for the same trap).
            snapshot_blocks = set(
                NeuronSnapshot.objects.filter(
                    block_id__gte=from_block,
                    block_id__lte=to_block,
                    neuron__subnet_id=netuid,
                )
                .values_list("block_id", flat=True)
                .distinct()
            )
            blocks = [block for block in candidates if block in snapshot_blocks]

            written = 0
            for block in blocks:
                if self._shutdown:
                    break
                if service.sync_burn(block, netuid) is not None:
                    written += 1

            total += written
            self.stdout.write(
                f"subnet {netuid}: {written} burn rows written "
                f"({len(blocks)} of {len(candidates)} epoch starts had snapshots)"
            )

        self.stdout.write(self.style.SUCCESS(f"Burn backfill complete: {total} rows"))

    def _backfill_emissions(self, service: BurnService, from_block: int, to_block: int) -> None:
        anchors = epoch_start_blocks_in_range(from_block, to_block, ROOT_NETUID)
        written = 0
        for block in anchors:
            if self._shutdown:
                break
            written += len(service.sync_subnet_emissions(block))

        self.stdout.write(
            self.style.SUCCESS(f"Emission backfill complete: {written} rows across {len(anchors)} meta epochs")
        )
