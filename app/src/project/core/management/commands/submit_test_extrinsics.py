"""Submit test extrinsics to a local bittensor node for development testing.

Submits real on-chain extrinsics that the block scheduler will pick up
and process through the notification pipeline.

Requires a running localnet node (e.g., ws://127.0.0.1:9944) with
the //Alice account as sudo.
"""

import time
from typing import Any

import bittensor as bt
from bittensor.sp_core import Keypair
from django.core.management.base import BaseCommand

DEFAULT_URL = "ws://127.0.0.1:9944"


def _network_added_netuid(events: list[Any]) -> int | None:
    """The netuid the chain assigned, recovered from a NetworkAdded event."""
    for event in events or []:
        value = getattr(event, "value", event)
        if not isinstance(value, dict):
            continue
        body = value.get("event")
        inner = body if isinstance(body, dict) else value
        if inner.get("event_id") != "NetworkAdded":
            continue
        attrs = inner.get("attributes")
        if isinstance(attrs, dict) and "netuid" in attrs:
            return int(attrs["netuid"])
        if isinstance(attrs, (list, tuple)) and attrs:
            return int(attrs[0])
    return None


class Command(BaseCommand):
    help = "Submit test extrinsics (sudo, register_network, dissolve_network, coldkey_swap) to a local bittensor node."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--url",
            type=str,
            default=None,
            help=f"Node WebSocket URL (default: settings.BITTENSOR_NETWORK or {DEFAULT_URL})",
        )
        parser.add_argument(
            "--type",
            type=str,
            default="all",
            choices=["sudo", "register", "dissolve", "coldkey_swap", "all"],
            help="Type of extrinsic to submit (default: all)",
        )
        parser.add_argument(
            "--netuid",
            type=int,
            default=None,
            help="Subnet UID to dissolve (overrides auto-detected netuid from register)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=2.0,
            help="Delay in seconds between extrinsics (default: 2.0)",
        )
        parser.add_argument(
            "--topup",
            action="store_true",
            help="Top up Alice's balance to 1M TAO before submitting extrinsics",
        )
        parser.add_argument(
            "--subnet-name",
            type=str,
            default=None,
            help="Subnet name (triggers register_network_with_identity when any identity flag is set)",
        )
        parser.add_argument(
            "--github-repo",
            type=str,
            default=None,
            help="GitHub repo URL for subnet identity",
        )
        parser.add_argument(
            "--subnet-contact",
            type=str,
            default=None,
            help="Contact info for subnet identity",
        )
        parser.add_argument(
            "--subnet-url",
            type=str,
            default=None,
            help="Subnet website URL for subnet identity",
        )
        parser.add_argument(
            "--discord",
            type=str,
            default=None,
            help="Discord URL for subnet identity",
        )
        parser.add_argument(
            "--description",
            type=str,
            default=None,
            help="Subnet description for subnet identity",
        )
        parser.add_argument(
            "--logo-url",
            type=str,
            default=None,
            help="Logo URL for subnet identity",
        )
        parser.add_argument(
            "--additional",
            type=str,
            default=None,
            help="Additional information for subnet identity",
        )

    def handle(self, *args, **options) -> None:
        url = options["url"] or DEFAULT_URL
        extrinsic_type = options["type"]
        delay = options["delay"]

        self.stdout.write(f"Connecting to {url}...")
        subtensor = bt.Subtensor(network=url)
        self.stdout.write(self.style.SUCCESS(f"Connected. Current block: {subtensor.block}"))

        alice = Keypair.create_from_uri("//Alice")
        bob = Keypair.create_from_uri("//Bob")

        if options["topup"]:
            self._topup_balance(subtensor, alice, bob)

        self._registered_netuid = options["netuid"]
        self._identity_options = {
            "subnet_name": options["subnet_name"],
            "github_repo": options["github_repo"],
            "subnet_contact": options["subnet_contact"],
            "subnet_url": options["subnet_url"],
            "discord": options["discord"],
            "description": options["description"],
            "logo_url": options["logo_url"],
            "additional": options["additional"],
        }

        actions = {
            "sudo": self._submit_sudo_call,
            "register": self._submit_register_network,
            "dissolve": self._submit_dissolve_network,
            "coldkey_swap": self._submit_coldkey_swap,
        }

        if extrinsic_type == "all":
            types_to_run = ["sudo", "register", "dissolve", "coldkey_swap"]
        else:
            types_to_run = [extrinsic_type]

        succeeded = 0
        for i, t in enumerate(types_to_run):
            if i > 0:
                self.stdout.write(f"Waiting {delay}s...")
                time.sleep(delay)
            try:
                actions[t](subtensor, alice, bob)
                succeeded += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Failed: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Done. {succeeded}/{len(types_to_run)} submitted."))

    def _submit(self, subtensor, call, keypair, label: str):
        """Submit a call and report where it landed."""
        self.stdout.write(f"Submitting {label}...")
        result = subtensor.submit_call(call, keypair)
        if result.success:
            self.stdout.write(self.style.SUCCESS(f"  Included in block {result.block_hash}"))
        else:
            self.stdout.write(self.style.WARNING(f"  Included but failed on-chain: {result.message}"))
        return result

    def _submit_sudo(self, subtensor, inner, keypair, label: str):
        """Submit `Sudo.sudo(inner)`. The inner call must be composed before it can nest."""
        sudo_call = bt.calls.Sudo.sudo(call=subtensor.compose(inner))
        return self._submit(subtensor, sudo_call, keypair, label)

    def _topup_balance(self, subtensor, alice, bob) -> None:
        """Force-set Alice's balance to 1M TAO via sudo so extrinsics don't fail from insufficient funds."""
        self.stdout.write("Topping up Alice's balance...")

        # Transfer a small amount from Bob to cover Alice's transaction fees
        self._submit(
            subtensor,
            bt.calls.Balances.transfer_keep_alive(dest=alice.ss58_address, value=1_000_000_000),  # 1 TAO for fees
            bob,
            "Balances → transfer_keep_alive(Bob → Alice, 1 TAO)",
        )

        # Now Alice can afford fees for the sudo call
        result = self._submit_sudo(
            subtensor,
            bt.calls.Balances.force_set_balance(
                who=alice.ss58_address,
                new_free=1_000_000_000_000_000,  # 1M TAO in rao
            ),
            alice,
            "Sudo → force_set_balance(Alice, 1M TAO)",
        )
        if result.success:
            self.stdout.write(self.style.SUCCESS("  Alice balance set to 1,000,000 TAO"))
        else:
            self.stdout.write(self.style.WARNING("  Balance top-up may have failed, continuing anyway"))

    def _submit_sudo_call(self, subtensor, alice, _bob) -> None:
        """Submit a Sudo call: sudo_set_min_burn on subnet 1."""
        self._submit_sudo(
            subtensor,
            bt.calls.AdminUtils.sudo_set_min_burn(netuid=1, min_burn=1000),
            alice,
            "Sudo → sudo_set_min_burn(netuid=1, min_burn=1000)",
        )

    # All fields required by the on-chain SubnetIdentityV3 struct
    IDENTITY_FIELDS = (
        "subnet_name",
        "github_repo",
        "subnet_contact",
        "subnet_url",
        "discord",
        "description",
        "logo_url",
        "additional",
    )

    def _submit_register_network(self, subtensor, alice, bob) -> None:
        """Submit a register_network or register_network_with_identity call.

        Uses bob as hotkey (signed by alice as coldkey) to avoid NonAssociatedColdKey
        errors when the hotkey is already registered to a different coldkey.
        """
        provided = {k: v for k, v in self._identity_options.items() if v is not None}

        if provided:
            # Build full struct — empty hex for fields not provided
            identity = {
                field: "0x" + provided[field].encode().hex() if field in provided else "0x"
                for field in self.IDENTITY_FIELDS
            }
            call = bt.calls.SubtensorModule.register_network_with_identity(
                hotkey=bob.ss58_address,
                identity=identity,
            )
            label = f"SubtensorModule → register_network_with_identity({', '.join(provided)})"
        else:
            call = bt.calls.SubtensorModule.register_network(hotkey=bob.ss58_address)
            label = "SubtensorModule → register_network"

        result = self._submit(subtensor, call, alice, label)

        netuid = _network_added_netuid(result.events)
        if netuid is not None:
            self._registered_netuid = netuid
            self.stdout.write(self.style.SUCCESS(f"  Registered subnet netuid={netuid}"))

    def _submit_dissolve_network(self, subtensor, alice, _bob) -> None:
        """Submit a dissolve_network call via Sudo for the previously registered subnet."""
        netuid = self._registered_netuid
        if netuid is None:
            self.stdout.write(
                self.style.WARNING(
                    "  No netuid to dissolve (use --netuid or run with --type=all to register first), skipping"
                )
            )
            return

        self._submit_sudo(
            subtensor,
            bt.calls.SubtensorModule.dissolve_network(coldkey=alice.ss58_address, netuid=netuid),
            alice,
            f"Sudo → dissolve_network(netuid={netuid})",
        )

    def _submit_coldkey_swap(self, subtensor, alice, bob) -> None:
        """Submit a swap_coldkey call via Sudo."""
        self._submit_sudo(
            subtensor,
            bt.calls.SubtensorModule.swap_coldkey(
                old_coldkey=alice.ss58_address,
                new_coldkey=bob.ss58_address,
                swap_cost=0,
            ),
            alice,
            f"Sudo → swap_coldkey(old={alice.ss58_address[:8]}..., new={bob.ss58_address[:8]}...)",
        )
