"""Retrieve the Sudo key from the chain."""

import bittensor as bt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

DEFAULT_URL = "ws://127.0.0.1:9944"


class Command(BaseCommand):
    help = "Get the Sudo key from the chain."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--url",
            type=str,
            default=None,
            help=f"Node WebSocket URL or network name (default: settings.BITTENSOR_NETWORK or {DEFAULT_URL})",
        )
        parser.add_argument(
            "--block",
            type=int,
            default=None,
            help="Block number to query (default: chain head)",
        )

    def handle(self, *args, **options) -> None:
        # The SDK resolves its own network names ("finney", "test", "local") and
        # takes a ws:// URL as-is.
        network = options["url"] or getattr(settings, "BITTENSOR_NETWORK", DEFAULT_URL)

        self.stdout.write(f"Connecting to {network}...")
        subtensor = bt.Subtensor(network=network)

        block_number = options["block"] if options["block"] is not None else subtensor.block
        self.stdout.write(f"Block #{block_number}\n")

        result = subtensor.query(bt.storage.Sudo.Key, block=block_number)

        if result is None:
            self.stderr.write(self.style.ERROR("Sudo key not found"))
            return

        self.stdout.write(f"Sudo key: {result}")
