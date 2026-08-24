import abc
from typing import Any, ClassVar

import structlog

from apps.notifications.channels import DatabaseWebhookChannel, NotificationChannel

logger = structlog.get_logger()

# Constants for string truncation
MIN_LENGTH_FOR_TRUNCATION = 20
MAX_CALL_ARGS_LENGTH = 1000
MAX_LIST_ITEMS_DISPLAY = 3


class ExtrinsicNotification(abc.ABC):
    """Base class for extrinsic-based notifications.

    Subclasses declare which extrinsic patterns they handle, which channels
    to deliver to, and how to format the message.

    Attributes:
        extrinsics: Patterns to match, e.g. ["AdminUtils"] (whole module)
                    or ["SubtensorModule:register_network"] (specific function).
        channels: Notification channels to deliver to.
        success_only: If True, only successful extrinsics are notified.
    """

    extrinsics: ClassVar[list[str]]
    channel: ClassVar[NotificationChannel]
    success_only: ClassVar[bool] = True

    def matches(self, call_module: str, call_function: str) -> bool:
        """Check if this notification handles the given module/function."""
        for pattern in self.extrinsics:
            if ":" in pattern:
                p_module, p_function = pattern.split(":", 1)
                if call_module == p_module and call_function == p_function:
                    return True
            elif call_module == pattern:
                return True
        return False

    @abc.abstractmethod
    def format_message(self, block_number: int, extrinsics: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the notification payload for a group of matched extrinsics."""
        ...

    def notify(self, block_number: int, extrinsics: list[dict[str, Any]]) -> int:
        """Filter extrinsics, format, and send to all channels. Returns count notified."""
        if self.success_only:
            extrinsics = [e for e in extrinsics if e.get("success", False)]

        if not extrinsics:
            return 0

        payload = self.format_message(block_number, extrinsics)
        sent = self.channel.send(payload)

        if sent:
            logger.info(
                "Notification sent",
                notification=self.__class__.__name__,
                block_number=block_number,
                extrinsic_count=len(extrinsics),
            )
            return len(extrinsics)
        return 0

    # ── Shared formatting utilities ──────────────────────────────────────

    @staticmethod
    def unwrap_sudo_call(extrinsic: dict[str, Any]) -> dict[str, Any]:
        """Unwrap a Sudo extrinsic to extract the inner call details."""
        call_module = extrinsic.get("call_module", "")
        call_function = extrinsic.get("call_function", "")

        if call_module != "Sudo" or call_function != "sudo":
            return extrinsic

        call_args = extrinsic.get("call_args", [])
        for arg in call_args:
            if arg.get("name") == "call" and isinstance(arg.get("value"), dict):
                inner = arg["value"]
                inner_args = inner.get("call_args", [])
                netuid = extrinsic.get("netuid")
                if netuid is None:
                    for inner_arg in inner_args:
                        if inner_arg.get("name") == "netuid":
                            netuid = inner_arg.get("value")
                            break
                return {
                    **extrinsic,
                    "call_module": inner.get("call_module", call_module),
                    "call_function": inner.get("call_function", call_function),
                    "call_args": inner_args,
                    "netuid": netuid,
                    "_is_sudo": True,
                }

        return extrinsic

    @staticmethod
    def format_value(value: Any) -> str:
        """Format a value for display, truncating long lists."""
        if value is None:
            return "N/A"
        if isinstance(value, list) and len(value) > MAX_LIST_ITEMS_DISPLAY:
            return f"[{len(value)} items]"
        return str(value)

    @staticmethod
    def format_call_args(call_args: list[dict[str, Any]] | None) -> str:
        """Format call arguments for display, truncating long values."""
        if not call_args:
            return "None"

        lines = []
        for arg in call_args:
            name = arg.get("name", "unknown")
            value = arg.get("value")

            if isinstance(value, str) and len(value) > MIN_LENGTH_FOR_TRUNCATION:
                value_display = f"{value[:10]}...{value[-8:]}"
            elif isinstance(value, dict):
                value_display = "{...}"
            elif isinstance(value, list) and len(value) > MAX_LIST_ITEMS_DISPLAY:
                value_display = f"[{len(value)} items]"
            else:
                value_display = str(value)

            lines.append(f"**{name}**: `{value_display}`")

        result = "\n".join(lines)
        if len(result) > MAX_CALL_ARGS_LENGTH:
            result = result[:MAX_CALL_ARGS_LENGTH] + "..."
        return result

    @staticmethod
    def decode_hex_field(value: Any) -> str:
        """Decode a hex-encoded bytes field (e.g. SubnetIdentityV3 Vec<u8> fields)."""
        if not isinstance(value, str):
            return str(value) if value else ""
        try:
            text = value.removeprefix("0x")
            return bytes.fromhex(text).decode("utf-8", errors="replace").strip("\x00")
        except ValueError, UnicodeDecodeError:
            return value

    @staticmethod
    def taostats_link(block_number: int | str, extrinsic_index: int = 0) -> str:
        """Build a TaoStats extrinsic URL."""
        idx = f"{extrinsic_index:04d}" if isinstance(extrinsic_index, int) else "0000"
        return f"https://taostats.io/extrinsic/{block_number}-{idx}?network=finney"

    @staticmethod
    def group_by_netuid(extrinsics: list[dict[str, Any]]) -> dict[int | None, list[dict[str, Any]]]:
        """Group extrinsics by netuid."""
        groups: dict[int | None, list[dict[str, Any]]] = {}
        for ext in extrinsics:
            netuid = ext.get("netuid")
            groups.setdefault(netuid, []).append(ext)
        return groups


class BlockEventNotification(abc.ABC):
    """Base class for block-level event notifications.

    Handles chain events emitted by runtime hooks (on_initialize coinbase,
    on_idle) rather than by user extrinsics. Such events carry
    ``extrinsic_idx: None`` and are invisible to the extrinsic-driven
    pipeline above.

    Attributes:
        events: Patterns to match, e.g. ["SubtensorModule"] (whole module)
                or ["SubtensorModule:SubnetOwnerChanged"] (specific event).
        channel: Notification channel to deliver to.
    """

    events: ClassVar[list[str]]
    channel: ClassVar[NotificationChannel]

    def matches(self, module_id: str, event_id: str) -> bool:
        """Check if this notification handles the given module/event."""
        for pattern in self.events:
            if ":" in pattern:
                p_module, p_event = pattern.split(":", 1)
                if module_id == p_module and event_id == p_event:
                    return True
            elif module_id == pattern:
                return True
        return False

    @abc.abstractmethod
    def format_message(self, block_number: int, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the notification payload for a group of matched events."""
        ...

    def notify(self, block_number: int, events: list[dict[str, Any]]) -> int:
        """Format and send matched events to the channel. Returns count notified."""
        if not events:
            return 0

        payload = self.format_message(block_number, events)
        sent = self.channel.send(payload)

        if sent:
            logger.info(
                "Block event notification sent",
                notification=self.__class__.__name__,
                block_number=block_number,
                event_count=len(events),
            )
            return len(events)
        return 0

    @staticmethod
    def taostats_block_link(block_number: int) -> str:
        """Build a TaoStats block URL (block-level events have no extrinsic index)."""
        return f"https://taostats.io/block/{block_number}?network=finney"

    @staticmethod
    def event_netuid(event: dict[str, Any]) -> Any:
        """Netuid from event attributes: named dict, positional list/tuple, or bare value."""
        attrs = event.get("attributes")
        if isinstance(attrs, dict):
            return attrs.get("netuid")
        if isinstance(attrs, (list, tuple)):
            return attrs[0] if attrs else None
        return attrs


class SubnetRoutedNotification(ExtrinsicNotification):
    """Base for notifications routed to per-subnet webhook URLs from the database.

    Groups extrinsics by netuid and sends each group to the webhook URLs
    configured in the SubnetWebhook model for that subnet.

    Subclasses must define:
        extrinsics: patterns to match
        format_message: build notification payload

    Optionally define:
        fallback_channel: used when no DB webhooks are configured for a netuid
    """

    fallback_channel: ClassVar[NotificationChannel | None] = None

    @property
    def channel(self) -> NotificationChannel:  # type: ignore[override]
        raise AttributeError("SubnetRoutedNotification does not use a static channel")

    def _route_to_subnets(
        self, block_number: int, extrinsics: list[dict[str, Any]]
    ) -> tuple[int, list[dict[str, Any]]]:
        """Send each netuid group to its DB webhooks.

        Returns ``(routed_count, unrouted)`` where ``unrouted`` holds the
        extrinsics with no netuid or no working DB webhook, left for the caller
        to deliver via the fallback channel.
        """
        routed = 0
        unrouted: list[dict[str, Any]] = []

        for netuid, group in self.group_by_netuid(extrinsics).items():
            if netuid is not None:
                db_channel = DatabaseWebhookChannel(netuid)
                sent = db_channel.send(self.format_message(block_number, group))
                if sent:
                    logger.info(
                        "Subnet notification sent",
                        notification=self.__class__.__name__,
                        netuid=netuid,
                        block_number=block_number,
                        extrinsic_count=len(group),
                    )
                    routed += len(group)
                    continue
            # No DB webhook or netuid is None — collect for fallback
            unrouted.extend(group)

        return routed, unrouted

    def notify(self, block_number: int, extrinsics: list[dict[str, Any]]) -> int:
        """Filter, group by netuid, and send to per-subnet webhook URLs."""
        if self.success_only:
            extrinsics = [e for e in extrinsics if e.get("success", False)]

        if not extrinsics:
            return 0

        total, unrouted = self._route_to_subnets(block_number, extrinsics)

        if unrouted and self.fallback_channel:
            payload = self.format_message(block_number, unrouted)
            sent = self.fallback_channel.send(payload)
            if sent:
                logger.info(
                    "Fallback notification sent",
                    notification=self.__class__.__name__,
                    block_number=block_number,
                    extrinsic_count=len(unrouted),
                )
                total += len(unrouted)

        return total
