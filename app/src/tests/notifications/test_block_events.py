"""Tests for the block-level event notification path (BlockEventNotification)."""

from typing import Any, ClassVar

import pytest

from apps.notifications import registry as registry_module
from apps.notifications.base import BlockEventNotification
from apps.notifications.channels import NotificationChannel
from apps.notifications.handlers.subnet_owner_change import SubnetOwnerChangeNotification

# Real payload observed on finney block 8843504 (subnet 3 owner reassigned by lock conviction).
OWNER_CHANGED_EVENT: dict[str, Any] = {
    "phase": "Initialization",
    "extrinsic_idx": None,
    "event_index": 7,
    "module_id": "SubtensorModule",
    "event_id": "SubnetOwnerChanged",
    "attributes": {
        "netuid": 3,
        "old_coldkey": "5FUJoAsY5TWfs1FGFtscC5QUuarJMCWYwYzEftyGAeH7pUqK",
        "new_coldkey": "5GsGUrd21bnkNxQsH2grv474y4gcDwCmp5xJ72KeokspZ2bg",
    },
    "topics": [],
}


class FakeChannel(NotificationChannel):
    def __init__(self, *, succeed: bool = True):
        self.payloads: list[dict] = []
        self.should_succeed = succeed

    def send(self, payload: dict) -> bool:
        self.payloads.append(payload)
        return self.should_succeed


def _make_handler(event_patterns: list[str], channel: FakeChannel | None = None):
    """Create a concrete block-event handler with given patterns."""
    ch = channel or FakeChannel()

    class Handler(BlockEventNotification):
        events: ClassVar[list[str]] = event_patterns
        channel: ClassVar = ch

        def format_message(self, block_number: int, events: list[dict[str, Any]]) -> dict[str, Any]:
            return {"content": f"{self.__class__.__name__}: {len(events)}"}

    return Handler(), ch


# ── matches() ──────────────────────────────────────────────────────────


def test_matches_specific_event_pattern():
    handler, _ = _make_handler(["SubtensorModule:SubnetOwnerChanged"])
    assert handler.matches("SubtensorModule", "SubnetOwnerChanged") is True
    assert handler.matches("SubtensorModule", "NetworkAdded") is False
    assert handler.matches("System", "SubnetOwnerChanged") is False


def test_matches_whole_module_pattern():
    handler, _ = _make_handler(["SubtensorModule"])
    assert handler.matches("SubtensorModule", "AnythingAtAll") is True
    assert handler.matches("System", "ExtrinsicSuccess") is False


# ── notify() ───────────────────────────────────────────────────────────


def test_notify_sends_payload_and_returns_count():
    handler, channel = _make_handler(["SubtensorModule:SubnetOwnerChanged"])
    count = handler.notify(8843504, [OWNER_CHANGED_EVENT])
    assert count == 1
    assert len(channel.payloads) == 1


def test_notify_empty_events_returns_zero_without_sending():
    handler, channel = _make_handler(["SubtensorModule:SubnetOwnerChanged"])
    assert handler.notify(8843504, []) == 0
    assert channel.payloads == []


def test_notify_channel_failure_returns_zero():
    handler, _ = _make_handler(["SubtensorModule:SubnetOwnerChanged"], FakeChannel(succeed=False))
    assert handler.notify(8843504, [OWNER_CHANGED_EVENT]) == 0


# ── helpers ────────────────────────────────────────────────────────────


def test_taostats_block_link():
    assert BlockEventNotification.taostats_block_link(8843504) == "https://taostats.io/block/8843504?network=finney"


def test_event_netuid_named_dict_attrs():
    assert BlockEventNotification.event_netuid(OWNER_CHANGED_EVENT) == 3


def test_event_netuid_positional_attrs():
    assert BlockEventNotification.event_netuid({"attributes": [42, 0]}) == 42


def test_event_netuid_bare_value_attrs():
    assert BlockEventNotification.event_netuid({"attributes": 7}) == 7


def test_event_netuid_empty_attrs():
    assert BlockEventNotification.event_netuid({"attributes": []}) is None
    assert BlockEventNotification.event_netuid({"attributes": None}) is None


# ── event registry & dispatch ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_event_registry():
    """Reset the global event registry before each test."""
    original = registry_module._event_registry[:]
    registry_module._event_registry.clear()
    yield
    registry_module._event_registry.clear()
    registry_module._event_registry.extend(original)


def test_register_event_adds_instance_and_returns_class():
    @registry_module.register_event
    class MyEventNotification(BlockEventNotification):
        events: ClassVar[list[str]] = ["SubtensorModule:SubnetOwnerChanged"]
        channel: ClassVar = FakeChannel()

        def format_message(self, block_number, events):
            return {}

    assert len(registry_module._event_registry) == 1
    assert isinstance(registry_module._event_registry[0], MyEventNotification)
    assert MyEventNotification.__name__ == "MyEventNotification"


def test_dispatch_groups_events_per_handler():
    owner_handler, owner_channel = _make_handler(["SubtensorModule:SubnetOwnerChanged"])
    added_handler, added_channel = _make_handler(["SubtensorModule:NetworkAdded"])
    registry_module._event_registry.extend([owner_handler, added_handler])

    events = [
        OWNER_CHANGED_EVENT,
        {**OWNER_CHANGED_EVENT, "attributes": {**OWNER_CHANGED_EVENT["attributes"], "netuid": 5}},
        {"module_id": "SubtensorModule", "event_id": "NetworkAdded", "attributes": [42, 0]},
    ]
    total = registry_module.dispatch_block_event_notifications(8843504, events)

    assert total == 3
    assert len(owner_channel.payloads) == 1  # two owner events, one grouped message
    assert len(added_channel.payloads) == 1


def test_dispatch_ignores_unmatched_events():
    handler, channel = _make_handler(["SubtensorModule:SubnetOwnerChanged"])
    registry_module._event_registry.append(handler)

    events = [{"module_id": "System", "event_id": "NewAccount", "attributes": None}]
    assert registry_module.dispatch_block_event_notifications(100, events) == 0
    assert channel.payloads == []


def test_dispatch_empty_events_returns_zero():
    handler, _ = _make_handler(["SubtensorModule:SubnetOwnerChanged"])
    registry_module._event_registry.append(handler)
    assert registry_module.dispatch_block_event_notifications(100, []) == 0


def test_dispatch_isolates_handler_exceptions():
    class ExplodingHandler(BlockEventNotification):
        events: ClassVar[list[str]] = ["SubtensorModule:SubnetOwnerChanged"]
        channel: ClassVar = FakeChannel()

        def format_message(self, block_number, events):
            raise RuntimeError("boom")

    ok_handler, ok_channel = _make_handler(["SubtensorModule:NetworkAdded"])
    registry_module._event_registry.extend([ExplodingHandler(), ok_handler])

    events = [
        OWNER_CHANGED_EVENT,
        {"module_id": "SubtensorModule", "event_id": "NetworkAdded", "attributes": [42, 0]},
    ]
    total = registry_module.dispatch_block_event_notifications(8843504, events)

    assert total == 1  # exploding handler contributes nothing, ok handler still delivers
    assert len(ok_channel.payloads) == 1


# ── SubnetOwnerChangeNotification ──────────────────────────────────────


def test_owner_change_matches_only_subnet_owner_changed():
    handler = SubnetOwnerChangeNotification()
    assert handler.matches("SubtensorModule", "SubnetOwnerChanged") is True
    assert handler.matches("SubtensorModule", "NetworkAdded") is False


def test_owner_change_message_format():
    handler = SubnetOwnerChangeNotification()
    payload = handler.format_message(8843504, [OWNER_CHANGED_EVENT])

    content = payload["content"]
    assert "**Block #8843504**" in content
    assert "**Subnet 3**" in content
    assert "lock conviction" in content
    assert "5FUJoAsY5TWfs1FGFtscC5QUuarJMCWYwYzEftyGAeH7pUqK" in content
    assert "5GsGUrd21bnkNxQsH2grv474y4gcDwCmp5xJ72KeokspZ2bg" in content
    assert "https://taostats.io/block/8843504?network=finney" in content
    assert payload["flags"] == 1 << 2


def test_owner_change_message_multiple_events():
    handler = SubnetOwnerChangeNotification()
    second = {
        **OWNER_CHANGED_EVENT,
        "attributes": {"netuid": 8, "old_coldkey": "5Old...", "new_coldkey": "5New..."},
    }
    content = handler.format_message(8843504, [OWNER_CHANGED_EVENT, second])["content"]
    assert "**Subnet 3**" in content
    assert "**Subnet 8**" in content


def test_owner_change_message_tolerates_malformed_attributes():
    handler = SubnetOwnerChangeNotification()
    content = handler.format_message(100, [{**OWNER_CHANGED_EVENT, "attributes": None}])["content"]
    assert "**Block #100**" in content
    assert "N/A" in content
