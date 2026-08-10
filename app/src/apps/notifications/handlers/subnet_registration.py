from typing import Any, ClassVar

from apps.notifications.base import ExtrinsicNotification
from apps.notifications.channels import DiscordWebhookChannel
from apps.notifications.registry import register


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
            lines.append(f"**Subnet {netuid}**" if netuid is not None else "**Global**")
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
        coldkey until a netuid frees up. Pre-root-reborn blocks carry neither
        event, in which case no outcome line is rendered.
        """
        added = None
        for event in extrinsic.get("events") or []:
            if not isinstance(event, dict) or event.get("module_id") != "SubtensorModule":
                continue
            if event.get("event_id") == "NetworkAdded":
                added = event
        if added is not None:
            netuid = self._event_netuid(added)
            return [f"**outcome**: created — netuid `{self.format_value(netuid)}`"]
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
