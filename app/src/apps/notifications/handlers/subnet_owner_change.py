from typing import Any, ClassVar

from apps.notifications.base import BlockEventNotification
from apps.notifications.channels import DiscordWebhookChannel
from apps.notifications.registry import register_event


@register_event
class SubnetOwnerChangeNotification(BlockEventNotification):
    """Notification for subnet ownership reassignment by lock conviction.

    SubnetOwnerChanged is emitted from coinbase (on_initialize) when lock
    conviction crowns a new subnet king — no extrinsic is involved.
    """

    events: ClassVar[list[str]] = ["SubtensorModule:SubnetOwnerChanged"]
    channel: ClassVar = DiscordWebhookChannel("DISCORD_SUBNET_REGISTRATION_WEBHOOK_URL")

    def format_message(self, block_number: int, events: list[dict[str, Any]]) -> dict[str, Any]:
        lines = [f"**Block #{block_number}**", ""]

        for event in events:
            attrs = event.get("attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            netuid = self.event_netuid(event)
            lines.append(f"**Subnet {netuid if netuid is not None else 'N/A'}** — owner changed by lock conviction")
            lines.append(f"**old**: `{attrs.get('old_coldkey') or 'N/A'}`")
            lines.append(f"**new**: `{attrs.get('new_coldkey') or 'N/A'}`")
            lines.append("")

        lines.append(f"[View on TaoStats]({self.taostats_block_link(block_number)})")
        return {"content": "\n".join(lines), "flags": 1 << 2}
