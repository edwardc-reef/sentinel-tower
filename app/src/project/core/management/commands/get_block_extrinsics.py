"""Fetch and display extrinsics from the current (or specified) block."""

import bittensor as bt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

DEFAULT_URL = "ws://127.0.0.1:9944"


class Command(BaseCommand):
    help = "Get extrinsics for the current block header (or a specific block number)."

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
        network = options["url"] or getattr(settings, "BITTENSOR_NETWORK", DEFAULT_URL)
        self.stdout.write(f"Connecting to {network}...")
        subtensor = bt.Subtensor(network=network)

        block = subtensor.block_info(options["block"])
        if block is None:
            self.stderr.write(self.style.ERROR(f"Block {options['block']} not found"))
            return

        self.stdout.write(self.style.SUCCESS(f"Block #{block.number} (hash: {block.hash})"))

        if not block.extrinsics:
            self.stdout.write("No extrinsics in this block.")
            return

        self.stdout.write(f"\nFound {len(block.extrinsics)} extrinsic(s):\n")
        for i, extrinsic in enumerate(block.extrinsics):
            # The SDK reports an undecodable extrinsic as None rather than dropping it,
            # so the index still lines up with the block's own ordering.
            call = (extrinsic or {}).get("call", {})
            module = call.get("call_module", "?")
            function = call.get("call_function", "?")
            args = call.get("call_args", [])

            self.stdout.write(f"  [{i}] {module}.{function}")
            for arg in args:
                name = arg.get("name", "?")
                value = arg.get("value", "?")
                self.stdout.write(f"       {name}: {value}")
            self.stdout.write("")
