from typing import Any, ClassVar

from apps.notifications.base import ExtrinsicNotification
from apps.notifications.channels import DiscordWebhookChannel
from apps.notifications.registry import register

RAO_PER_TAO = 10**9
U64F64_SCALE = 2**64


@register
class SubnetRegistrationNotification(ExtrinsicNotification):
    """Notification for subnet registration events.

    Displays full registration details including decoded identity fields.
    """

    extrinsics: ClassVar[list[str]] = [
        "SubtensorModule:register_network",
        "SubtensorModule:register_network_with_identity",
    ]
    channel: ClassVar = DiscordWebhookChannel("DISCORD_SUBNET_REGISTRATION_WEBHOOK_URL")

    def format_message(self, block_number: int, extrinsics: list[dict[str, Any]]) -> dict[str, Any]:
        first = extrinsics[0]
        link = self.taostats_link(block_number, first.get("extrinsic_index", 0))
        unwrapped = [self.unwrap_sudo_call(e) for e in extrinsics]

        lines = [f"**Block #{block_number}**", ""]

        for netuid, group in sorted(
            self.group_by_netuid(unwrapped).items(),
            key=lambda x: (x[0] is None, x[0]),
        ):
            lines.append(f"**Subnet {netuid}**" if netuid is not None else "**Pending netuid**")
            for ext in group:
                lines.append(self._format_registration(ext))
            lines.append("")

        lines.append(f"[View on TaoStats]({link})")
        return {"content": "\n".join(lines), "flags": 1 << 2}

    def _format_registration(self, extrinsic: dict[str, Any]) -> str:
        call_function = extrinsic.get("call_function", "unknown")
        call_args = extrinsic.get("call_args", [])
        address = extrinsic.get("address", "N/A")
        extrinsic_hash = extrinsic.get("extrinsic_hash", "N/A")

        parts = [f"`{call_function}`", *self._outcome_lines(extrinsic), f"**signer**: `{address}`"]

        for arg in call_args:
            name = arg.get("name", "")
            if name == "netuid":
                continue
            value = arg.get("value")
            if name == "identity" and isinstance(value, dict):
                for field_name, field_value in value.items():
                    decoded = self.decode_hex_field(field_value)
                    if decoded:
                        parts.append(f"**{field_name}**: {decoded}")
            else:
                parts.append(f"**{name}**: `{self.format_value(value)}`")

        parts.append(f"**hash**: `{extrinsic_hash}`")
        return "\n".join(parts)

    def _outcome_lines(self, extrinsic: dict[str, Any]) -> list[str]:
        """Describe the registration outcome recorded in the attached events.

        Since subtensor root-reborn (#2968), a successful register_network no
        longer implies a subnet was created: NetworkAdded means immediate
        creation; NetworkRegistrationQueued means the lock cost is held on the
        coldkey until a netuid frees up, with NetworkRemoved marking a subnet
        pruned to make room. Pre-root-reborn blocks carry none of these
        events, in which case no outcome line is rendered.
        """
        added = queued = removed = None
        for event in extrinsic.get("events") or []:
            if not isinstance(event, dict) or event.get("module_id") != "SubtensorModule":
                continue
            event_id = event.get("event_id")
            if event_id == "NetworkAdded":
                added = event
            elif event_id == "NetworkRegistrationQueued":
                queued = event
            elif event_id == "NetworkRemoved":
                removed = event
        if added is not None:
            netuid = self._event_netuid(added)
            return [f"**outcome**: created — netuid `{self.format_value(netuid)}`"]
        if queued is not None:
            attrs = queued.get("attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            lock = self._format_tao(attrs.get("lock_amount"))
            price = self._format_u64f64(attrs.get("median_subnet_alpha_price"))
            lines = [f"**outcome**: queued — {lock} TAO locked, alpha price snapshot {price}"]
            if removed is not None:
                pruned = self._event_netuid(removed)
                lines.append(f"**pruned to make room**: subnet `{self.format_value(pruned)}`")
            return lines
        return []

    @staticmethod
    def _event_netuid(event: dict[str, Any]) -> Any:
        """Netuid from event attributes: named dict, positional list/tuple, or bare value."""
        attrs = event.get("attributes")
        if isinstance(attrs, dict):
            return attrs.get("netuid")
        if isinstance(attrs, (list, tuple)):
            return attrs[0] if attrs else None
        return attrs

    @staticmethod
    def _format_tao(value: Any) -> str:
        """Render a rao amount as TAO, falling back to raw display for unexpected types."""
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value / RAO_PER_TAO:.9f}".rstrip("0").rstrip(".")
        return ExtrinsicNotification.format_value(value)

    @staticmethod
    def _format_u64f64(value: Any) -> str:
        """Render a U64F64 fixed-point (raw bits) as a decimal, falling back to raw display.

        On-chain the value decodes as a ``{"bits": <int>}`` struct (observed on
        finney, e.g. block 8693261); a bare int of raw bits is accepted too.
        """
        if isinstance(value, dict):
            value = value.get("bits")
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value / U64F64_SCALE:.6f}"
        return ExtrinsicNotification.format_value(value)
