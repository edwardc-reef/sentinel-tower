from datetime import UTC, datetime

import structlog
from django.conf import settings
from sentinel.v1.providers import pylon_provider
from sentinel.v1.providers.base import BlockchainProvider
from sentinel.v1.services.sentinel import sentinel_service

import apps.metagraph.utils as metagraph_utils
from apps.metagraph.services.burn_service import BurnService
from apps.metagraph.services.metagraph_sync_service import DumpMetadata, MetagraphSyncService

logger = structlog.get_logger()


def _get_epoch_position(block_number: int, netuid: int) -> str:
    """Determine the position of a block within its epoch (start, inside, end)."""
    epoch = metagraph_utils.get_epoch_containing_block(block_number, netuid)
    dumpable_blocks = metagraph_utils.get_dumpable_blocks(epoch)

    if block_number == dumpable_blocks[0]:
        return "start"
    if block_number == dumpable_blocks[-1]:
        return "end"
    return "inside"


def sync_metagraph_for_block(
    block_number: int,
    netuid: int,
    provider: BlockchainProvider,
    lite: bool | None = None,
) -> dict | None:
    """
    Sync metagraph for the given netuid at the specified block using an existing provider.

    Fetches metagraph data from the blockchain, stores it as a JSONL artifact,
    and syncs it to Django models.

    Args:
        lite: If provided, overrides settings.METAGRAPH_LITE for this call.

    Returns:
        Dict with sync stats and elapsed_ms, or None if no metagraph data found.

    """
    started_at = datetime.now(UTC)
    log = logger.bind(block=block_number, netuid=netuid)

    log.debug("Fetching metagraph from provider")
    service = sentinel_service(provider)
    lite_mode = settings.METAGRAPH_LITE if lite is None else lite
    subnet = service.ingest_subnet(netuid, block_number, lite=lite_mode)
    metagraph = subnet.metagraph
    finished_at = datetime.now(UTC)
    ingest_ms = round((finished_at - started_at).total_seconds() * 1000)
    log.debug("Provider ingest completed", ingest_ms=ingest_ms)

    if not metagraph:
        log.debug("No metagraph data found")
        return None

    t0 = datetime.now(UTC)
    # MetagraphService.store_metagraph_artifact(metagraph)
    artifact_ms = round((datetime.now(UTC) - t0).total_seconds() * 1000)
    log.debug("Stored metagraph artifact", artifact_ms=artifact_ms)

    dump_metadata = DumpMetadata(
        netuid=netuid,
        epoch_position=_get_epoch_position(block_number, netuid),
        started_at=started_at,
        finished_at=finished_at,
    )

    t0 = datetime.now(UTC)
    sync_service = MetagraphSyncService()
    stats = sync_service.sync_metagraph(metagraph, dump_metadata)
    sync_ms = round((datetime.now(UTC) - t0).total_seconds() * 1000)
    log.debug("Synced metagraph to DB", sync_ms=sync_ms, **stats)

    # Burn is derived from the mechanism metrics just written, so cache it before
    # retention can prune them. The provider is only used if the root-epoch anchor
    # block is unexpectedly absent from the ingested block table.
    if dump_metadata.epoch_position == "start":
        BurnService(provider).sync_burn(block_number, netuid)

    elapsed_ms = round((datetime.now(UTC) - started_at).total_seconds() * 1000)
    log.debug(
        "sync_metagraph_for_block completed",
        total_ms=elapsed_ms,
        ingest_ms=ingest_ms,
        artifact_ms=artifact_ms,
        sync_ms=sync_ms,
    )

    return {"neurons": stats["neurons"], "weights": stats["weights"], "bonds": stats["bonds"], "elapsed_ms": elapsed_ms}


def sync_subnet_emissions_for_block(block_number: int, provider: BlockchainProvider) -> int:
    """
    Sample ``SubnetEmissionEnabled`` for every subnet if this block starts a meta epoch.

    Unlike burn, this value leaves no trace in the metagraph snapshots, so it has
    to be read from chain storage while the block is still reachable. Returns the
    number of subnets recorded — 0 when the block does not start a meta epoch, or
    when the chain could not be read.
    """
    return len(BurnService(provider).sync_subnet_emissions(block_number))


# @block_task(
#     condition=lambda block_number, netuid: MetagraphService.is_dumpable_block(block_number, netuid),
#     args=[{"netuid": netuid} for netuid in MetagraphService.netuids_to_sync()],
#     celery_kwargs={"queue": "metagraph"},
# )
def store_metagraph(block_number: int, netuid: int) -> dict | None:
    """
    Store the metagraph for the given netuid at the specified block number.

    Fetches metagraph data from the blockchain, stores it as a JSONL artifact,
    and syncs it to Django models.
    """
    provider = pylon_provider()
    return sync_metagraph_for_block(block_number, netuid, provider)
