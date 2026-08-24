from apps.notifications.registry import dispatch_block_event_notifications, dispatch_block_notifications

# Backward-compatible alias used by apps.extrinsics imports
send_block_notifications = dispatch_block_notifications

__all__ = [
    "dispatch_block_event_notifications",
    "dispatch_block_notifications",
    "send_block_notifications",
]
