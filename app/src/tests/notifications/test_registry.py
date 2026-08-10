from typing import Any, ClassVar

import pytest

from apps.notifications import registry as registry_module
from apps.notifications.base import ExtrinsicNotification
from apps.notifications.channels import NotificationChannel


class FakeChannel(NotificationChannel):
    def __init__(self, *, succeed=True):
        self.payloads: list[dict] = []
        self.should_succeed = succeed

    def send(self, payload: dict) -> bool:
        self.payloads.append(payload)
        return self.should_succeed


def _make_handler(extrinsic_patterns: list[str], channel: FakeChannel | None = None):
    """Create a concrete notification handler with given patterns."""
    ch = channel or FakeChannel()

    class Handler(ExtrinsicNotification):
        extrinsics: ClassVar[list[str]] = extrinsic_patterns
        channel: ClassVar = ch

        def format_message(self, block_number: int, extrinsics: list[dict[str, Any]]) -> dict[str, Any]:
            return {"content": f"{self.__class__.__name__}: {len(extrinsics)}"}

    return Handler(), ch


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global registry before each test."""
    original = registry_module._registry[:]
    registry_module._registry.clear()
    yield
    registry_module._registry.clear()
    registry_module._registry.extend(original)


# ── register() ─────────────────────────────────────────────────────────


def test_register_adds_to_registry():
    @registry_module.register
    class MyNotification(ExtrinsicNotification):
        extrinsics: ClassVar[list[str]] = ["Foo"]
        channel: ClassVar = FakeChannel()

        def format_message(self, block_number, extrinsics):
            return {}

    assert len(registry_module._registry) == 1
    assert isinstance(registry_module._registry[0], MyNotification)


def test_register_returns_class():
    @registry_module.register
    class MyNotification(ExtrinsicNotification):
        extrinsics: ClassVar[list[str]] = ["Foo"]
        channel: ClassVar = FakeChannel()

        def format_message(self, block_number, extrinsics):
            return {}

    assert MyNotification is not None
    # Can still instantiate the class
    assert isinstance(MyNotification(), ExtrinsicNotification)


def test_get_registry_returns_copy():
    handler, _ = _make_handler(["Foo"])
    registry_module._registry.append(handler)

    result = registry_module.get_registry()
    assert len(result) == 1
    # Modifying the copy doesn't affect the original
    result.clear()
    assert len(registry_module._registry) == 1


# ── dispatch_block_notifications() ────────────────────────────────────


def test_dispatch_empty_extrinsics():
    assert registry_module.dispatch_block_notifications(100, []) == 0


def test_dispatch_routes_to_matching_handler():
    admin_handler, admin_ch = _make_handler(["AdminUtils"])
    registry_module._registry.append(admin_handler)

    extrinsics = [
        {"call_module": "AdminUtils", "call_function": "sudo_set_tempo", "success": True, "netuid": 1},
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 1
    assert len(admin_ch.payloads) == 1


def test_dispatch_routes_multiple_handlers():
    admin_handler, admin_ch = _make_handler(["AdminUtils"])
    reg_handler, reg_ch = _make_handler(["SubtensorModule:register_network"])
    registry_module._registry.extend([admin_handler, reg_handler])

    extrinsics = [
        {"call_module": "AdminUtils", "call_function": "sudo_set_tempo", "success": True, "netuid": 1},
        {"call_module": "SubtensorModule", "call_function": "register_network", "success": True},
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 2
    assert len(admin_ch.payloads) == 1
    assert len(reg_ch.payloads) == 1


def test_dispatch_unmatched_extrinsics_ignored():
    admin_handler, admin_ch = _make_handler(["AdminUtils"])
    registry_module._registry.append(admin_handler)

    extrinsics = [
        {"call_module": "SomeOtherModule", "call_function": "do_thing", "success": True},
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 0
    assert admin_ch.payloads == []


def test_dispatch_sudo_unwrap_routes_to_specific_handler():
    """Sudo-wrapped AdminUtils call should go to AdminUtils handler, not Sudo catch-all."""
    admin_handler, admin_ch = _make_handler(["AdminUtils"])
    sudo_handler, sudo_ch = _make_handler(["Sudo"])
    registry_module._registry.extend([admin_handler, sudo_handler])

    extrinsics = [
        {
            "call_module": "Sudo",
            "call_function": "sudo",
            "success": True,
            "netuid": None,
            "call_args": [
                {
                    "name": "call",
                    "value": {
                        "call_module": "AdminUtils",
                        "call_function": "sudo_set_tempo",
                        "call_args": [{"name": "netuid", "value": 1}, {"name": "tempo", "value": 360}],
                    },
                }
            ],
        }
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 1
    assert len(admin_ch.payloads) == 1
    assert sudo_ch.payloads == []


def test_dispatch_unmatched_sudo_falls_to_catch_all():
    """Sudo-wrapped call with no specific handler goes to Sudo catch-all."""
    admin_handler, admin_ch = _make_handler(["AdminUtils"])
    sudo_handler, sudo_ch = _make_handler(["Sudo"])
    registry_module._registry.extend([admin_handler, sudo_handler])

    extrinsics = [
        {
            "call_module": "Sudo",
            "call_function": "sudo",
            "success": True,
            "netuid": None,
            "call_args": [
                {
                    "name": "call",
                    "value": {
                        "call_module": "UnknownModule",
                        "call_function": "do_something",
                        "call_args": [],
                    },
                }
            ],
        }
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 1
    assert admin_ch.payloads == []
    assert len(sudo_ch.payloads) == 1


def test_dispatch_bare_sudo_without_inner_call():
    """A Sudo call that can't be unwrapped goes to Sudo catch-all."""
    sudo_handler, sudo_ch = _make_handler(["Sudo"])
    registry_module._registry.append(sudo_handler)

    extrinsics = [
        {
            "call_module": "Sudo",
            "call_function": "sudo",
            "success": True,
            "call_args": [{"name": "call", "value": "not_a_dict"}],
        }
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 1
    assert len(sudo_ch.payloads) == 1


def test_dispatch_sudo_wrapped_dissolve_network_routes_to_dissolution_handler():
    """Sudo-wrapped dissolve_network should go to the dissolution handler, not Sudo catch-all."""
    dissolution_handler, dissolution_ch = _make_handler(["SubtensorModule:dissolve_network"])
    sudo_handler, sudo_ch = _make_handler(["Sudo"])
    registry_module._registry.extend([dissolution_handler, sudo_handler])

    extrinsics = [
        {
            "call_module": "Sudo",
            "call_function": "sudo",
            "success": True,
            "netuid": None,
            "call_args": [
                {
                    "name": "call",
                    "type": "RuntimeCall",
                    "value": {
                        "call_index": "0x073d",
                        "call_function": "dissolve_network",
                        "call_module": "SubtensorModule",
                        "call_args": [
                            {"name": "coldkey", "type": "AccountId", "value": "5Grwva..."},
                            {"name": "netuid", "type": "NetUid", "value": 2},
                        ],
                    },
                }
            ],
        }
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 1
    assert len(dissolution_ch.payloads) == 1
    assert sudo_ch.payloads == []


def test_dispatch_sudo_wrapped_register_network_routes_to_registration_handler():
    """Sudo-wrapped register_network goes to the registration handler, not the Sudo catch-all."""
    registration_handler, registration_ch = _make_handler(
        ["SubtensorModule:register_network", "SubtensorModule:register_network_with_identity"]
    )
    sudo_handler, sudo_ch = _make_handler(["Sudo"])
    registry_module._registry.extend([registration_handler, sudo_handler])

    extrinsics = [
        {
            "call_module": "Sudo",
            "call_function": "sudo",
            "success": True,
            "netuid": None,
            "events": [{"module_id": "SubtensorModule", "event_id": "NetworkAdded", "attributes": [7, 1]}],
            "call_args": [
                {
                    "name": "call",
                    "type": "RuntimeCall",
                    "value": {
                        "call_module": "SubtensorModule",
                        "call_function": "register_network",
                        "call_args": [{"name": "hotkey", "type": "AccountId", "value": "5Gkey..."}],
                    },
                }
            ],
        }
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 1
    assert len(registration_ch.payloads) == 1
    assert sudo_ch.payloads == []


def test_dispatch_groups_multiple_extrinsics_per_handler():
    admin_handler, admin_ch = _make_handler(["AdminUtils"])
    registry_module._registry.append(admin_handler)

    extrinsics = [
        {"call_module": "AdminUtils", "call_function": "sudo_set_tempo", "success": True, "netuid": 1},
        {"call_module": "AdminUtils", "call_function": "sudo_set_rate_limit", "success": True, "netuid": 1},
    ]
    count = registry_module.dispatch_block_notifications(100, extrinsics)

    assert count == 2
    # Should be sent as a single message (one payload with both extrinsics)
    assert len(admin_ch.payloads) == 1
