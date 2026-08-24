from typing import Any

import structlog

from apps.notifications.base import BlockEventNotification, ExtrinsicNotification

logger = structlog.get_logger()

_registry: list[ExtrinsicNotification] = []


def register(cls: type[ExtrinsicNotification]) -> type[ExtrinsicNotification]:
    """Class decorator that registers a notification handler."""
    _registry.append(cls())
    return cls


def get_registry() -> list[ExtrinsicNotification]:
    """Return the list of registered notification handlers."""
    return list(_registry)


def dispatch_block_notifications(block_number: int, extrinsics: list[dict[str, Any]]) -> int:
    """Dispatch extrinsics to matching notification handlers.

    Each extrinsic is unwrapped (if Sudo) and matched against registered
    handlers. Extrinsics are grouped per handler and sent as a single message.
    If a Sudo-wrapped inner call matches a specific handler, it goes there;
    otherwise it falls through to the Sudo catch-all handler.

    Returns the total number of extrinsics notified.
    """
    if not extrinsics:
        return 0

    # Unwrap Sudo calls so inner calls can match specific handlers
    unwrapped_map: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ext in extrinsics:
        unwrapped = ExtrinsicNotification.unwrap_sudo_call(ext)
        unwrapped_map.append((ext, unwrapped))

    # Group extrinsics by handler (skip the Sudo catch-all on first pass)
    handler_groups: dict[ExtrinsicNotification, list[dict[str, Any]]] = {}
    unmatched_sudo: list[dict[str, Any]] = []
    sudo_handler: ExtrinsicNotification | None = None

    for handler in _registry:
        # Identify the Sudo catch-all for the fallback pass
        if handler.extrinsics == ["Sudo"]:
            sudo_handler = handler
            continue
        handler_groups[handler] = []

    matched_originals: set[int] = set()

    for i, (original, unwrapped) in enumerate(unwrapped_map):
        for handler in handler_groups:
            call_module = unwrapped.get("call_module", "")
            call_function = unwrapped.get("call_function", "")
            if handler.matches(call_module, call_function):
                handler_groups[handler].append(original)
                matched_originals.add(i)
                break
        else:
            # No specific handler matched; if it was Sudo-wrapped, save for fallback
            if unwrapped.get("_is_sudo") or original.get("call_module") == "Sudo":
                unmatched_sudo.append(original)
                matched_originals.add(i)

    # Sudo catch-all gets only unmatched Sudo extrinsics
    if sudo_handler and unmatched_sudo:
        handler_groups[sudo_handler] = unmatched_sudo

    # Dispatch to each handler
    total_notified = 0
    for handler, grouped in handler_groups.items():
        if not grouped:
            continue
        try:
            total_notified += handler.notify(block_number, grouped)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Notification handler failed",
                handler=handler.__class__.__name__,
                block_number=block_number,
                extrinsic_count=len(grouped),
            )

    return total_notified


_event_registry: list[BlockEventNotification] = []


def register_event(cls: type[BlockEventNotification]) -> type[BlockEventNotification]:
    """Class decorator that registers a block-event notification handler."""
    _event_registry.append(cls())
    return cls


def get_event_registry() -> list[BlockEventNotification]:
    """Return the list of registered block-event notification handlers."""
    return list(_event_registry)


def dispatch_block_event_notifications(block_number: int, events: list[dict[str, Any]]) -> int:
    """Dispatch block-level events to matching notification handlers.

    Events are matched on ``module_id``/``event_id``, grouped per handler,
    and sent as a single message per handler. Unmatched events are ignored
    (blocks routinely carry 100+ block-level events).

    Returns the total number of events notified.
    """
    if not events:
        return 0

    handler_groups: dict[BlockEventNotification, list[dict[str, Any]]] = {h: [] for h in _event_registry}

    for event in events:
        module_id = event.get("module_id", "")
        event_id = event.get("event_id", "")
        for handler in handler_groups:
            if handler.matches(module_id, event_id):
                handler_groups[handler].append(event)
                break

    total_notified = 0
    for handler, grouped in handler_groups.items():
        if not grouped:
            continue
        try:
            total_notified += handler.notify(block_number, grouped)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Block event notification handler failed",
                handler=handler.__class__.__name__,
                block_number=block_number,
                event_count=len(grouped),
            )

    return total_notified
