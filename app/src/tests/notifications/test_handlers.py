"""Tests for concrete notification handlers (format_message output)."""

from unittest.mock import patch

import pytest
from sentinel.v1.testing import (
    AnnounceColdkeySwapExtrinsicDTOFactory,
    ColdkeySwapExtrinsicDTOFactory,
    DisputeColdkeySwapExtrinsicDTOFactory,
    ExtrinsicDTOFactory,
    HyperparamExtrinsicDTOFactory,
    RegisterNetworkExtrinsicDTOFactory,
    RegisterNetworkWithIdentityExtrinsicDTOFactory,
)

from apps.notifications.handlers.coldkey_swap import ColdkeyRoles
from tests.notifications.conftest import flatten_extrinsic
from tests.notifications.test_base import FakeChannel

# ── AdminUtilsNotification ─────────────────────────────────────────────


def test_admin_format_shows_old_to_new(admin_handler):
    dto = HyperparamExtrinsicDTOFactory.build_for_function("sudo_set_tempo", netuid=1, tempo=360)
    ext = flatten_extrinsic(dto, extrinsic_index=5, previous_values={"tempo": 100})
    payload = admin_handler.format_message(100, [ext])

    assert payload["flags"] == 1 << 2
    content = payload["content"]
    assert "**Block #100**" in content
    assert "**Subnet 1**" in content
    assert "**tempo**: `100` → `360`" in content
    assert "taostats.io" in content


def test_admin_format_groups_by_subnet(admin_handler):
    dto1 = HyperparamExtrinsicDTOFactory.build_for_function("sudo_set_tempo", netuid=1, tempo=360)
    dto2 = HyperparamExtrinsicDTOFactory.build_for_function("sudo_set_tempo", netuid=2, tempo=720)
    ext1 = flatten_extrinsic(dto1, extrinsic_index=0, previous_values={})
    ext2 = flatten_extrinsic(dto2, extrinsic_index=1, previous_values={})
    content = admin_handler.format_message(100, [ext1, ext2])["content"]

    assert "**Subnet 1**" in content
    assert "**Subnet 2**" in content


def test_admin_format_without_previous_values(admin_handler):
    dto = HyperparamExtrinsicDTOFactory.build_for_function("sudo_set_tempo", netuid=1, tempo=360)
    ext = flatten_extrinsic(dto, extrinsic_index=0)
    content = admin_handler.format_message(100, [ext])["content"]
    assert "**tempo**: `N/A` → `360`" in content


# ── SubnetRegistrationNotification ─────────────────────────────────────


def test_registration_format_shows_details(registration_handler):
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=3,
        address="5Gxyz...",
        extrinsic_hash="0xabc123",
    )
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**Block #200**" in content
    assert "`register_network`" in content
    assert "**signer**: `5Gxyz...`" in content
    assert "**hotkey**: `5Gkey...`" in content
    assert "**hash**: `0xabc123`" in content


def test_registration_format_decodes_identity(registration_handler):
    dto = RegisterNetworkWithIdentityExtrinsicDTOFactory.build_for_hotkey(
        "5Gxyz...",
        subnet_name="My Subnet",
    )
    # Override call_args to include hex-encoded identity fields
    ext = flatten_extrinsic(dto, extrinsic_index=0, extrinsic_hash="0xabc")
    # Replace identity arg value with hex-encoded fields
    for arg in ext["call_args"]:
        if arg["name"] == "identity":
            arg["value"] = {
                "subnet_name": "0x" + b"My Subnet".hex(),
                "github_repo": "0x" + b"https://github.com/example".hex(),
            }
    content = registration_handler.format_message(300, [ext])["content"]

    assert "**subnet_name**: My Subnet" in content
    assert "**github_repo**: https://github.com/example" in content


def test_registration_outcome_created(registration_handler):
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=0,
        netuid=129,
        events=[{"module_id": "SubtensorModule", "event_id": "NetworkAdded", "attributes": [129, 1]}],
    )
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**Subnet 129**" in content
    assert "**outcome**: created — netuid `129`" in content


def test_registration_outcome_queued(registration_handler):
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=0,
        events=[
            {
                "module_id": "SubtensorModule",
                "event_id": "NetworkRegistrationQueued",
                "attributes": {
                    "coldkey": "5Gxyz...",
                    "hotkey": "5Gkey...",
                    "mechid": 1,
                    "identity": None,
                    "lock_amount": 2_451_700_000_000,
                    "median_subnet_alpha_price": 2**62,
                    "registration_block": 6_000_000,
                },
            }
        ],
    )
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**outcome**: queued — 2451.7 TAO locked, alpha price snapshot 0.250000" in content


def test_registration_outcome_queued_after_prune(registration_handler):
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=0,
        events=[
            {"module_id": "SubtensorModule", "event_id": "NetworkRemoved", "attributes": 42},
            {
                "module_id": "SubtensorModule",
                "event_id": "NetworkRegistrationQueued",
                "attributes": {
                    "lock_amount": 1_000_000_000,
                    "median_subnet_alpha_price": 2**63,
                },
            },
        ],
    )
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**outcome**: queued — 1 TAO locked, alpha price snapshot 0.500000" in content
    assert "**pruned to make room**: subnet `42`" in content


def test_registration_without_outcome_events_renders_like_before(registration_handler):
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    ext = flatten_extrinsic(dto, extrinsic_index=0, address="5Gxyz...")
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**outcome**" not in content
    assert "**Pending netuid**" in content
    assert "**Global**" not in content
    assert "**signer**: `5Gxyz...`" in content


def test_registration_outcome_malformed_attributes_fall_back(registration_handler):
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=0,
        events=[
            {
                "module_id": "SubtensorModule",
                "event_id": "NetworkRegistrationQueued",
                "attributes": ["unexpected", "positional"],
            }
        ],
    )
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**outcome**: queued — N/A TAO locked, alpha price snapshot N/A" in content
    assert "**pruned to make room**" not in content


def test_registration_outcome_survives_sudo_unwrap(registration_handler):
    """The outer extrinsic's events must survive sudo-unwrapping into the outcome line."""
    ext = {
        "call_module": "Sudo",
        "call_function": "sudo",
        "success": True,
        "netuid": None,
        "extrinsic_index": 0,
        "extrinsic_hash": "0xabc",
        "address": "5Gsudo...",
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
    content = registration_handler.format_message(200, [ext])["content"]

    assert "**outcome**: created — netuid `7`" in content


# ── ColdkeySwapNotification ───────────────────────────────────────────


def test_coldkey_format_announce_with_roles(coldkey_handler):
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc123")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=4,
        address="5Gold...",
        _coldkey_roles=ColdkeyRoles(owned_subnets=[1, 3], validator_subnets=[2]),
    )
    content = coldkey_handler.format_message(400, [ext])["content"]

    assert "**Block #400**" in content
    assert "**Coldkey Swap Announced**" in content
    assert "**signer**: `5Gold...`" in content
    assert "Subnet Owner (SN 1, SN 3)" in content
    assert "Validator (SN 2)" in content
    assert "**new_coldkey_hash**: `0xabc123`" in content


def test_coldkey_format_executed(coldkey_handler):
    dto = ColdkeySwapExtrinsicDTOFactory.build_for_coldkey("5Gnew...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=5,
        address="5Gold...",
        _coldkey_roles=ColdkeyRoles(miner_subnets=[8]),
    )
    content = coldkey_handler.format_message(500, [ext])["content"]

    assert "**Coldkey Swap Executed**" in content
    assert "**signer**: `5Gold...`" in content
    assert "Miner (SN 8)" in content
    assert "**new_coldkey**: `5Gnew...`" in content


def test_coldkey_format_duplicate_subnets_collapsed(coldkey_handler):
    """Multiple neurons on the same subnet show count instead of repeating."""
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build()
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=4,
        address="5Gold...",
        _coldkey_roles=ColdkeyRoles(miner_subnets=[54] * 19),
    )
    content = coldkey_handler.format_message(400, [ext])["content"]

    assert "Miner (SN 54 x19)" in content
    assert content.count("54") == 1


def test_coldkey_format_disputed_unknown_role(coldkey_handler):
    dto = DisputeColdkeySwapExtrinsicDTOFactory.build()
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=6,
        address="5Gkey...",
        _coldkey_roles=ColdkeyRoles(),
    )
    content = coldkey_handler.format_message(600, [ext])["content"]

    assert "**Coldkey Swap Disputed**" in content
    assert "**signer**: `5Gkey...`" in content
    assert "**role**: Unknown" in content


def test_coldkey_format_deduplicates_fanned_out(coldkey_handler):
    """When notify fans out the same extrinsic to multiple netuids, format_message deduplicates."""
    roles = ColdkeyRoles(owned_subnets=[1], validator_subnets=[2])
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xhash")
    base = flatten_extrinsic(
        dto,
        extrinsic_index=4,
        extrinsic_hash="0xabc",
        address="5Gold...",
        _coldkey_roles=roles,
    )
    # Simulate fan-out: same extrinsic duplicated with different netuids
    extrinsics = [{**base, "netuid": 1}, {**base, "netuid": 2}]
    content = coldkey_handler.format_message(400, extrinsics)["content"]

    # Should appear only once
    assert content.count("**Coldkey Swap Announced**") == 1


# ── SubnetDissolutionNotification ──────────────────────────────────────


def test_dissolution_sudo_wrapped_real_payload(dissolution_handler):
    """Sudo-wrapped dissolve_network from a real block routes and formats correctly."""
    extrinsics = [
        {
            "call_module": "Sudo",
            "call_function": "sudo",
            "extrinsic_index": 5,
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
                            {
                                "name": "coldkey",
                                "type": "AccountId",
                                "value": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                            },
                            {"name": "netuid", "type": "NetUid", "value": 2},
                        ],
                    },
                }
            ],
            "success": True,
        }
    ]

    assert dissolution_handler.matches("SubtensorModule", "dissolve_network") is True

    content = dissolution_handler.format_message(9, extrinsics)["content"]
    assert "**Block #9**" in content
    assert "`dissolve_network`" in content
    assert "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY" in content


def test_dissolution_format_shows_function(dissolution_handler):
    extrinsics = [
        {
            "call_module": "SubtensorModule",
            "call_function": "dissolve_network",
            "extrinsic_index": 2,
            "netuid": 42,
            "call_args": [{"name": "netuid", "value": 42}],
        }
    ]
    content = dissolution_handler.format_message(500, extrinsics)["content"]

    assert "**Block #500**" in content
    assert "`dissolve_network`" in content


# ── SudoNotification ──────────────────────────────────────────────────


def test_sudo_format_generic(sudo_handler):
    dto = ExtrinsicDTOFactory.build(
        call__call_module="Sudo",
        call__call_function="sudo",
        call__call_args=[],
    )
    ext = flatten_extrinsic(dto, extrinsic_index=1, netuid=None)
    ext["call_args"] = [{"name": "call", "value": "set_weights"}]
    content = sudo_handler.format_message(600, [ext])["content"]

    assert "**Block #600**" in content
    assert "**Global**" in content


def test_sudo_format_groups_by_netuid(sudo_handler):
    dto1 = ExtrinsicDTOFactory.build(
        call__call_module="Sudo",
        call__call_function="sudo",
        call__call_args=[],
    )
    dto2 = ExtrinsicDTOFactory.build(
        call__call_module="Sudo",
        call__call_function="sudo",
        call__call_args=[],
    )
    ext1 = flatten_extrinsic(dto1, extrinsic_index=0, netuid=1)
    ext1["call_args"] = [{"name": "call", "value": "foo"}]
    ext2 = flatten_extrinsic(dto2, extrinsic_index=1, netuid=None)
    ext2["call_args"] = [{"name": "call", "value": "bar"}]
    content = sudo_handler.format_message(700, [ext1, ext2])["content"]

    assert "**Subnet 1**" in content
    assert "**Global**" in content


# ── All handlers suppress embeds ──────────────────────────────────────


@pytest.mark.parametrize(
    "handler_fixture",
    ["admin_handler", "registration_handler", "coldkey_handler", "dissolution_handler", "sudo_handler"],
)
def test_all_handlers_suppress_embeds(handler_fixture, request):
    handler = request.getfixturevalue(handler_fixture)
    extrinsics = [
        {
            "call_module": "Test",
            "call_function": "test",
            "extrinsic_index": 0,
            "call_args": [],
            "netuid": 1,
            "address": "5Gxyz",
            "extrinsic_hash": "0xabc",
        }
    ]
    payload = handler.format_message(100, extrinsics)
    assert payload["flags"] == 1 << 2


# ── ColdkeySwapNotification identity labels ───────────────────────────


def test_coldkey_format_signer_identity_label(coldkey_handler):
    """Signer address is annotated with its Coldkey.label when available."""
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc123")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=4,
        address="5Gold...",
        _coldkey_roles=ColdkeyRoles(),
    )
    with patch.object(
        coldkey_handler,
        "_resolve_labels",
        return_value={"5Gold...": "Foundry"},
    ):
        content = coldkey_handler.format_message(400, [ext])["content"]

    assert "**signer**: `5Gold...` (Foundry)" in content


def test_coldkey_format_arg_identity_label(coldkey_handler):
    """Coldkey call args are annotated with their Coldkey.label when available."""
    dto = ColdkeySwapExtrinsicDTOFactory.build_for_coldkey("5Gnew...")
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=5,
        address="5Gold...",
        _coldkey_roles=ColdkeyRoles(),
    )
    with patch.object(
        coldkey_handler,
        "_resolve_labels",
        return_value={"5Gnew...": "Taoshi"},
    ):
        content = coldkey_handler.format_message(500, [ext])["content"]

    assert "**new_coldkey**: `5Gnew...` (Taoshi)" in content
    # Signer has no label — should appear without annotation
    assert "**signer**: `5Gold...`\n" in content


def test_coldkey_format_no_labels(coldkey_handler):
    """When no labels are found, output is unchanged from the original format."""
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build()
    ext = flatten_extrinsic(
        dto,
        extrinsic_index=4,
        address="5Gold...",
        _coldkey_roles=ColdkeyRoles(owned_subnets=[1]),
    )
    with patch.object(
        coldkey_handler,
        "_resolve_labels",
        return_value={},
    ):
        content = coldkey_handler.format_message(400, [ext])["content"]

    assert "**signer**: `5Gold...`\n" in content
    assert "(" not in content.split("signer")[1].split("\n")[0]


# ── ColdkeySwapNotification routing: central always, owned subnet extra ──


@pytest.mark.django_db
@patch("apps.notifications.handlers.coldkey_swap.resolve_coldkey_roles")
@patch("apps.notifications.channels._http_client")
def test_coldkey_notify_owner_swap_goes_to_central_and_owned_subnet(mock_http, mock_resolve, coldkey_handler):
    """An owner swap reaches the central channel AND the owned subnet's DB webhook."""
    from apps.notifications.models import SubnetWebhook

    SubnetWebhook.objects.create(netuid=7, url="https://discord.com/api/webhooks/db/subnet7")
    mock_resolve.return_value = ColdkeyRoles(owned_subnets=[7])

    central = FakeChannel()
    coldkey_handler.fallback_channel = central

    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc")
    ext = flatten_extrinsic(dto, extrinsic_index=0, address="5Gowner...")

    coldkey_handler.notify(100, [ext])

    # central channel always receives it
    assert len(central.payloads) == 1
    # owned subnet's DB webhook additionally receives it
    assert mock_http.post.call_args[0][0] == "https://discord.com/api/webhooks/db/subnet7"


@pytest.mark.django_db
@patch("apps.notifications.handlers.coldkey_swap.resolve_coldkey_roles")
def test_coldkey_notify_owner_swap_without_webhook_posts_central_once(mock_resolve, coldkey_handler):
    """An owner swap whose subnet has no webhook reaches central exactly once (no double-post)."""
    mock_resolve.return_value = ColdkeyRoles(owned_subnets=[7])  # no SubnetWebhook configured

    central = FakeChannel()
    coldkey_handler.fallback_channel = central

    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc")
    ext = flatten_extrinsic(dto, extrinsic_index=0, address="5Gowner...")

    coldkey_handler.notify(100, [ext])

    assert len(central.payloads) == 1


@pytest.mark.django_db
@patch("apps.notifications.handlers.coldkey_swap.resolve_coldkey_roles")
@patch("apps.notifications.channels._http_client")
def test_coldkey_notify_non_owner_swap_central_only(mock_http, mock_resolve, coldkey_handler):
    """A validator/miner (non-owner) swap goes to central only — never a subnet webhook."""
    from apps.notifications.models import SubnetWebhook

    SubnetWebhook.objects.create(netuid=7, url="https://discord.com/api/webhooks/db/subnet7")
    mock_resolve.return_value = ColdkeyRoles(validator_subnets=[7], miner_subnets=[9])

    central = FakeChannel()
    coldkey_handler.fallback_channel = central

    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc")
    ext = flatten_extrinsic(dto, extrinsic_index=0, address="5Gvali...")

    coldkey_handler.notify(100, [ext])

    assert len(central.payloads) == 1
    mock_http.post.assert_not_called()


@pytest.mark.django_db
@patch("apps.notifications.handlers.coldkey_swap.resolve_coldkey_roles")
@patch("apps.notifications.channels._http_client")
def test_coldkey_notify_unknown_signer_central_only(mock_http, mock_resolve, coldkey_handler):
    """A swap by a signer with no known roles still goes to the central channel."""
    mock_resolve.return_value = ColdkeyRoles()  # unknown coldkey, no roles

    central = FakeChannel()
    coldkey_handler.fallback_channel = central

    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc")
    ext = flatten_extrinsic(dto, extrinsic_index=0, address="5Gunknown...", netuid=None)

    coldkey_handler.notify(100, [ext])

    assert len(central.payloads) == 1
    mock_http.post.assert_not_called()
