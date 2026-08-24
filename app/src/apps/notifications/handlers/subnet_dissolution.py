from typing import Any, ClassVar

from apps.notifications.base import BlockEventNotification, ExtrinsicNotification
from apps.notifications.channels import DiscordWebhookChannel
from apps.notifications.registry import register, register_event


@register
class SubnetDissolutionNotification(ExtrinsicNotification):
    """Notification for subnet dissolution events."""

    extrinsics: ClassVar[list[str]] = ["SubtensorModule:dissolve_network"]
    channel: ClassVar = DiscordWebhookChannel("DISCORD_SUBNET_REGISTRATION_WEBHOOK_URL")

    def format_message(self, block_number: int, extrinsics: list[dict[str, Any]]) -> dict[str, Any]:
        first = extrinsics[0]
        link = self.taostats_link(block_number, first.get("extrinsic_index", 0))
        unwrapped = [self.unwrap_sudo_call(e) for e in extrinsics]

        lines = [f"**Block #{block_number}**", ""]

        for ext in unwrapped:
            lines.append(self._format_dissolution(ext))
            lines.append("")

        lines.append(f"[View on TaoStats]({link})")
        return {"content": "\n".join(lines), "flags": 1 << 2}

    def _format_dissolution(self, extrinsic: dict[str, Any]) -> str:
        call_function = extrinsic.get("call_function", "unknown")
        call_args = extrinsic.get("call_args", [])

        params = []
        for arg in call_args:
            name = arg.get("name", "")
            if name == "netuid":
                continue
            params.append(f"**{name}**: `{self.format_value(arg.get('value'))}`")

        if params:
            return f"`{call_function}` — " + ", ".join(params)
        return f"`{call_function}`"


@register_event
class SubnetDissolutionCleanupNotification(BlockEventNotification):
    """Notification for dissolved-subnet storage cleanup finishing in on_idle.

    The dissolve_network extrinsic itself is reported by
    SubnetDissolutionNotification; this marks the deferred cleanup completing.
    """

    events: ClassVar[list[str]] = ["SubtensorModule:NetworkDissolveCleanupCompleted"]
    channel: ClassVar = DiscordWebhookChannel("DISCORD_SUBNET_REGISTRATION_WEBHOOK_URL")

    def format_message(self, block_number: int, events: list[dict[str, Any]]) -> dict[str, Any]:
        lines = [f"**Block #{block_number}**", ""]

        for event in events:
            netuid = self.event_netuid(event)
            lines.append(f"**Subnet {netuid if netuid is not None else 'N/A'}** — dissolution cleanup completed")
        lines.append("")

        lines.append(f"[View on TaoStats]({self.taostats_block_link(block_number)})")
        return {"content": "\n".join(lines), "flags": 1 << 2}
