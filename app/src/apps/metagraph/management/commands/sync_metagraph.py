import signal

import structlog
from django.conf import settings
from django.core.management.base import BaseCommand
from sentinel.v1.providers.base import BlockchainProvider
from sentinel.v1.providers.bittensor import bittensor_provider

from apps.metagraph.block_tasks import sync_metagraph_for_block
from apps.metagraph.services.metagraph_service import MetagraphService
from project.core.services.bittensor_connection import ProviderReconnectBackoff

logger = structlog.get_logger()


class Command(BaseCommand):
    help = "Long-running daemon that syncs metagraph snapshots from the blockchain using a persistent connection."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown = False

    def _handle_signal(self, signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info("Received shutdown signal", signal=sig_name)
        self._shutdown = True

    def _build_provider(self, provider_name: str) -> BlockchainProvider:
        """Build a provider. No I/O happens here — the connection is opened by __enter__."""
        if provider_name == "bittensor":
            return bittensor_provider()
        if provider_name == "pylon":
            from sentinel.v1.providers.pylon import pylon_provider

            return pylon_provider(settings.PYLON_URL)
        raise ValueError(f"Unknown provider: {provider_name}")

    def _connect_provider(
        self,
        provider_name: str,
        reconnect: ProviderReconnectBackoff,
    ) -> BlockchainProvider | None:
        """
        Open a provider connection, retrying failures with exponential backoff.

        Connecting is itself a network call (a websocket handshake against the chain
        endpoint) and it is attempted precisely when that endpoint is already misbehaving,
        so it fails often. Letting the failure escape would kill the daemon, so keep
        retrying until it succeeds or shutdown is requested. The shared outage state
        ensures one failure reaches error level after
        BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS attempts.

        Returns:
            A connected provider, or None if shutdown was requested before one opened.

        """
        while not self._shutdown:
            provider = self._build_provider(provider_name)
            try:
                provider.__enter__()
            except Exception:
                self._close_provider(provider)
                delay = reconnect.record_failure(
                    logger,
                    "Provider connection failed",
                    provider=provider_name,
                )
                reconnect.wait(delay, lambda: self._shutdown)
                continue

            return provider

        return None

    def _close_provider(self, provider: BlockchainProvider) -> None:
        try:
            provider.__exit__(None, None, None)
        except Exception:
            logger.warning("Error closing provider", exc_info=True)

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            choices=["bittensor", "pylon"],
            default="bittensor",
            help="Blockchain provider to use for syncing (default: bittensor)",
        )

    def handle(self, *args, **options) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        provider_name = options["provider"]

        netuids = MetagraphService.netuids_to_sync()

        self.stdout.write("Starting sync_metagraph daemon...")
        logger.info(
            "sync_metagraph daemon starting", poll_interval=settings.BITTENSOR_SECONDS_PER_BLOCK, netuids=netuids
        )

        provider: BlockchainProvider | None = None
        last_processed_block = None
        reconnect = ProviderReconnectBackoff(
            initial_delay_seconds=settings.BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS,
            max_delay_seconds=settings.BITTENSOR_RECONNECT_MAX_DELAY_SECONDS,
            alert_after_attempts=settings.BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS,
        )

        try:
            while not self._shutdown:
                # A dropped provider is reopened here, so every reconnect goes through the
                # same retrying code path.
                if provider is None:
                    provider = self._connect_provider(provider_name, reconnect)
                    if provider is None:
                        break

                try:
                    head = provider.get_current_block()
                except Exception:
                    self._close_provider(provider)
                    provider = None
                    delay = reconnect.record_failure(
                        logger,
                        "Connection error fetching head, reconnecting...",
                        provider=provider_name,
                    )
                    reconnect.wait(delay, lambda: self._shutdown)
                    continue

                reconnect.record_recovery(logger, provider=provider_name)

                if last_processed_block is None:
                    last_processed_block = head - 1
                    logger.info("Starting from head", head=head)

                if head <= last_processed_block:
                    reconnect.wait(settings.BITTENSOR_SECONDS_PER_BLOCK, lambda: self._shutdown)
                    continue

                # Process all blocks from last_processed + 1 to head
                for block_number in range(last_processed_block + 1, head + 1):
                    if self._shutdown:
                        break

                    dumpable_netuids = [
                        netuid for netuid in netuids if MetagraphService.is_dumpable_block(block_number, netuid)
                    ]

                    if not dumpable_netuids:
                        logger.debug("No subnets dumpable at block", block_number=block_number)
                        last_processed_block = block_number
                        continue

                    try:
                        for netuid in dumpable_netuids:
                            result = sync_metagraph_for_block(block_number, netuid, provider)
                            if result:
                                logger.info(
                                    "Metagraph synced",
                                    block=block_number,
                                    netuid=netuid,
                                    neurons=result["neurons"],
                                    weights=result["weights"],
                                    elapsed_ms=result["elapsed_ms"],
                                )
                            else:
                                logger.debug("No metagraph data", block=block_number, netuid=netuid)
                        last_processed_block = block_number
                    except Exception:
                        self._close_provider(provider)
                        provider = None
                        delay = reconnect.record_failure(
                            logger,
                            "Error syncing metagraph, reconnecting...",
                            provider=provider_name,
                            block_number=block_number,
                        )
                        reconnect.wait(delay, lambda: self._shutdown)
                        # Skip the failed block — backfill service will catch it
                        last_processed_block = block_number
                        break

                if provider is not None:
                    reconnect.wait(settings.BITTENSOR_SECONDS_PER_BLOCK, lambda: self._shutdown)
        finally:
            if provider is not None:
                self._close_provider(provider)
            logger.info("sync_metagraph daemon stopped")
