from typing import Any, ClassVar

import structlog
from bittensor.sp_core import ss58_encode

from apps.metagraph.models import Coldkey
from apps.metagraph.services.coldkey_roles import ColdkeyRoles, resolve_coldkey_roles
from apps.notifications.base import SubnetRoutedNotification
from apps.notifications.channels import DiscordWebhookChannel
from apps.notifications.registry import register

logger = structlog.get_logger()

_HEX_KEY_ARGS = {"old_coldkey", "new_coldkey", "coldkey", "new_coldkey_hash"}

BITTENSOR_SS58_FORMAT = 42


def _format_arg(name: str, value: Any) -> str:
    """Format a call argument, converting hex public keys to SS58 addresses."""
    if name in _HEX_KEY_ARGS and isinstance(value, str) and value.startswith("0x"):
        try:
            return ss58_encode(bytes.fromhex(value[2:]), BITTENSOR_SS58_FORMAT)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to convert hex to SS58", name=name, value=value)
    return str(value) if value is not None else "N/A"


_ACTION_LABELS: dict[str, str] = {
    "announce_coldkey_swap": "Coldkey Swap Announced",
    "swap_coldkey_announced": "Coldkey Swap Executed",
    "dispute_coldkey_swap": "Coldkey Swap Disputed",
    "reset_coldkey_swap": "Coldkey Swap Reset",
    "clear_coldkey_swap_announcement": "Coldkey Swap Cleared",
}


@register
class ColdkeySwapNotification(SubnetRoutedNotification):
    """Notification for coldkey swap events.

    Covers the full announce -> wait -> execute lifecycle plus
    dispute, reset, and clear actions.

    Since coldkey swap extrinsics carry no netuid, the handler looks up
    the signer's roles (subnet owner / validator / miner) and routes
    notifications to all associated subnets.
    """

    extrinsics: ClassVar[list[str]] = [
        "SubtensorModule:announce_coldkey_swap",
        "SubtensorModule:swap_coldkey_announced",
        "SubtensorModule:dispute_coldkey_swap",
        "SubtensorModule:reset_coldkey_swap",
        "SubtensorModule:clear_coldkey_swap_announcement",
    ]
    fallback_channel: ClassVar = DiscordWebhookChannel("DISCORD_COLDKEY_SWAP_WEBHOOK_URL")

    def notify(self, block_number: int, extrinsics: list[dict[str, Any]]) -> int:
        """Announce every coldkey swap in the central channel; subnet owners' swaps
        are additionally posted to the owned subnet's own channel."""
        if self.success_only:
            extrinsics = [e for e in extrinsics if e.get("success", False)]

        if not extrinsics:
            return 0

        # Resolve roles for each unique signer and attach them to each extrinsic.
        roles_cache: dict[str, ColdkeyRoles] = {}
        enriched: list[dict[str, Any]] = []
        for ext in extrinsics:
            address = ext.get("address", "")
            if address and address not in roles_cache:
                try:
                    roles_cache[address] = resolve_coldkey_roles(address)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to resolve coldkey roles", address=address, exc_info=True)
                    roles_cache[address] = ColdkeyRoles()
            enriched.append({**ext, "_coldkey_roles": roles_cache.get(address, ColdkeyRoles())})

        # 1. Every coldkey swap is announced in the central channel.
        total = 0
        if self.fallback_channel and self.fallback_channel.send(self.format_message(block_number, enriched)):
            logger.info(
                "Coldkey-swap notification sent",
                notification=self.__class__.__name__,
                block_number=block_number,
                extrinsic_count=len(enriched),
            )
            total = len(enriched)

        # 2. Swaps by a subnet owner are additionally posted to that subnet's own
        #    channel. Only owned subnets route here — validator/miner associations
        #    do not trigger a per-subnet notification.
        owned: list[dict[str, Any]] = []
        for ext in enriched:
            for netuid in dict.fromkeys(ext["_coldkey_roles"].owned_subnets):
                owned.append({**ext, "netuid": netuid})
        if owned:
            self._route_to_subnets(block_number, owned)

        return total

    def _resolve_labels(self, extrinsics: list[dict[str, Any]]) -> dict[str, str]:
        """Collect all coldkey addresses from extrinsics and resolve their labels."""
        addresses: set[str] = set()
        for ext in extrinsics:
            if addr := ext.get("address"):
                addresses.add(addr)
            for arg in ext.get("call_args", []):
                if arg.get("name") in _HEX_KEY_ARGS:
                    addresses.add(_format_arg(arg["name"], arg.get("value")))
        if not addresses:
            return {}
        try:
            return dict(Coldkey.objects.filter(coldkey__in=addresses, label__gt="").values_list("coldkey", "label"))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to resolve coldkey labels", exc_info=True)
            return {}

    def format_message(self, block_number: int, extrinsics: list[dict[str, Any]]) -> dict[str, Any]:
        first = extrinsics[0]
        link = self.taostats_link(block_number, first.get("extrinsic_index", 0))
        unwrapped = [self.unwrap_sudo_call(e) for e in extrinsics]

        # Deduplicate: when fanned out, the same extrinsic appears per-netuid
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for ext in unwrapped:
            key = (ext.get("extrinsic_hash", ""), ext.get("call_function", ""))
            if key not in seen:
                seen.add(key)
                unique.append(ext)

        labels = self._resolve_labels(unique)

        lines = [f"**Block #{block_number}**", ""]

        for ext in unique:
            lines.append(self._format_swap(ext, labels))
            lines.append("")

        lines.append(f"[View on TaoStats]({link})")
        return {"content": "\n".join(lines), "flags": 1 << 2}

    def _format_swap(self, extrinsic: dict[str, Any], labels: dict[str, str]) -> str:
        call_function = extrinsic.get("call_function", "unknown")
        label = _ACTION_LABELS.get(call_function, call_function)
        address = extrinsic.get("address", "")
        call_args = extrinsic.get("call_args", [])
        roles: ColdkeyRoles = extrinsic.get("_coldkey_roles", ColdkeyRoles())

        parts = [f"**{label}**"]
        if address:
            identity = f" ({labels[address]})" if address in labels else ""
            parts.append(f"**signer**: `{address}`{identity}")

        parts.extend(roles.format_lines())

        for arg in call_args:
            name = arg.get("name", "")
            value = arg.get("value")
            formatted = _format_arg(name, value)
            identity = f" ({labels[formatted]})" if formatted in labels else ""
            parts.append(f"**{name}**: `{formatted}`{identity}")

        return "\n".join(parts)
