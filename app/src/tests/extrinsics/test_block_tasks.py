"""Integration tests for store_block_extrinsics using FakeBlockchainProvider."""

from unittest.mock import patch

import pytest
from sentinel.v1.dto import ExtrinsicDTO
from sentinel.v1.testing import (
    AnnounceColdkeySwapExtrinsicDTOFactory,
    FakeBlockchainProvider,
    HyperparamExtrinsicDTOFactory,
    RegisterNetworkExtrinsicDTOFactory,
)

from apps.extrinsics.block_tasks import BlockExtrinsicsUnavailableError, store_block_extrinsics
from apps.extrinsics.models import Extrinsic


def _to_raw(dto: ExtrinsicDTO, *, extrinsic_hash: str, index: int = 0, **overrides) -> dict:
    """Convert an ExtrinsicDTO to the flat dict format a real node returns.

    FakeBlockchainProvider stores raw dicts which ExtrinsicExtractor
    then converts back into ExtrinsicDTO objects.
    """
    d = {
        "index": index,
        "extrinsic_hash": extrinsic_hash,
        "call_function": dto.call.call_function,
        "call_module": dto.call.call_module,
        "call_args": [a.model_dump() for a in dto.call.call_args],
        "address": dto.address or "",
        "nonce": dto.nonce,
        "tip": dto.tip,
    }
    d.update(overrides)
    return d


def _success_event(extrinsic_idx: int = 0) -> dict:
    """Build a System.ExtrinsicSuccess event for the given extrinsic index."""
    return {
        "phase": {"ApplyExtrinsic": extrinsic_idx},
        "extrinsic_idx": extrinsic_idx,
        "event_index": "0x0000",
        "module_id": "System",
        "event_id": "ExtrinsicSuccess",
        "attributes": {},
        "topics": [],
    }


@pytest.mark.django_db
@patch("apps.extrinsics.block_tasks.store_extrinsics_artifact", return_value=1)
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_coldkey_swap_extrinsic_synced_to_db(mock_dispatch, mock_artifact):
    """Coldkey swap extrinsic flows from provider through to the DB and triggers notifications."""
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xabc123")
    raw = _to_raw(dto, extrinsic_hash="0xdeadbeef01", address="5Gold...")

    provider = (
        FakeBlockchainProvider()
        .with_block(100, "0xblockhash")
        .with_extrinsics("0xblockhash", [raw])
        .with_events("0xblockhash", [_success_event(0)])
    )

    result = store_block_extrinsics(100, provider)

    assert result is not None
    assert result["db_count"] == 1

    ext = Extrinsic.objects.get(extrinsic_hash="0xdeadbeef01")
    assert ext.call_module == "SubtensorModule"
    assert ext.call_function == "announce_coldkey_swap"
    assert ext.address == "5Gold..."
    assert ext.success is True

    mock_dispatch.assert_called_once()
    block_number, extrinsics = mock_dispatch.call_args[0]
    assert block_number == 100
    assert len(extrinsics) == 1
    assert extrinsics[0]["call_function"] == "announce_coldkey_swap"


@pytest.mark.django_db
@patch("apps.extrinsics.block_tasks.store_extrinsics_artifact", return_value=1)
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_register_network_extrinsic_synced_to_db(mock_dispatch, mock_artifact):
    """Register network extrinsic is stored and dispatched."""
    dto = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Ghotkey...")
    raw = _to_raw(dto, extrinsic_hash="0xdeadbeef02", address="5Gowner...")

    provider = (
        FakeBlockchainProvider()
        .with_block(200, "0xblockhash2")
        .with_extrinsics("0xblockhash2", [raw])
        .with_events("0xblockhash2", [_success_event(0)])
    )

    result = store_block_extrinsics(200, provider)

    assert result is not None
    assert result["db_count"] == 1

    ext = Extrinsic.objects.get(extrinsic_hash="0xdeadbeef02")
    assert ext.call_module == "SubtensorModule"
    assert ext.call_function == "register_network"
    assert ext.address == "5Gowner..."


@pytest.mark.django_db
@patch("apps.extrinsics.block_tasks.store_extrinsics_artifact", return_value=1)
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_hyperparam_change_enriched_with_previous_values(mock_dispatch, mock_artifact):
    """AdminUtils hyperparam extrinsic gets previous_values populated."""
    dto = HyperparamExtrinsicDTOFactory.build_for_function("sudo_set_tempo", netuid=1, tempo=360)
    raw = _to_raw(dto, extrinsic_hash="0xdeadbeef03", address="5Gadmin...")

    provider = (
        FakeBlockchainProvider()
        .with_block(300, "0xblockhash3")
        .with_extrinsics("0xblockhash3", [raw])
        .with_events("0xblockhash3", [_success_event(0)])
    )

    result = store_block_extrinsics(300, provider)

    assert result is not None
    mock_dispatch.assert_called_once()
    _, extrinsics = mock_dispatch.call_args[0]
    enriched = extrinsics[0]
    assert enriched["call_function"] == "sudo_set_tempo"
    assert "previous_values" in enriched
    # First time seeing this param — previous value is None
    assert enriched["previous_values"]["tempo"] is None


@pytest.mark.django_db
@patch("apps.extrinsics.block_tasks.store_extrinsics_artifact", return_value=1)
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_multiple_extrinsics_in_single_block(mock_dispatch, mock_artifact):
    """Multiple extrinsics in one block are all synced."""
    dto1 = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xhash1")
    dto2 = RegisterNetworkExtrinsicDTOFactory.build_for_hotkey("5Gkey...")
    raw1 = _to_raw(dto1, extrinsic_hash="0xext01", index=0, address="5Ga...")
    raw2 = _to_raw(dto2, extrinsic_hash="0xext02", index=1, address="5Gb...")

    provider = (
        FakeBlockchainProvider()
        .with_block(400, "0xblockhash4")
        .with_extrinsics("0xblockhash4", [raw1, raw2])
        .with_events("0xblockhash4", [_success_event(0), _success_event(1)])
    )

    result = store_block_extrinsics(400, provider)

    assert result is not None
    assert result["db_count"] == 2
    assert Extrinsic.objects.filter(block_number=400).count() == 2


@pytest.mark.django_db
@patch("apps.extrinsics.block_tasks.store_extrinsics_artifact", return_value=1)
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_duplicate_extrinsics_are_skipped(mock_dispatch, mock_artifact):
    """Re-processing the same block does not create duplicate Extrinsic rows."""
    dto = AnnounceColdkeySwapExtrinsicDTOFactory.build_for_hash("0xdup")
    raw = _to_raw(dto, extrinsic_hash="0xext_dup", address="5Gc...")

    provider = (
        FakeBlockchainProvider()
        .with_block(500, "0xblockhash5")
        .with_extrinsics("0xblockhash5", [raw])
        .with_events("0xblockhash5", [_success_event(0)])
    )

    result1 = store_block_extrinsics(500, provider)
    assert result1 is not None

    result2 = store_block_extrinsics(500, provider)
    assert result2 is not None

    assert result1["db_count"] == 1
    assert result2["db_count"] == 0
    assert Extrinsic.objects.filter(extrinsic_hash="0xext_dup").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("extrinsics", [[], None], ids=["empty-list", "no-response"])
@patch("apps.extrinsics.block_tasks.store_extrinsics_artifact", return_value=0)
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_unavailable_block_raises(mock_dispatch, mock_artifact, extrinsics):
    """A missing block response is not mistaken for a block with no extrinsics."""
    provider = FakeBlockchainProvider().with_block(600, "0xblockhash6")
    if extrinsics is not None:
        provider.with_extrinsics("0xblockhash6", extrinsics)

    with pytest.raises(BlockExtrinsicsUnavailableError) as exc_info:
        store_block_extrinsics(600, provider)

    assert exc_info.value.block_number == 600
    assert Extrinsic.objects.filter(block_number=600).count() == 0
    mock_artifact.assert_not_called()
    mock_dispatch.assert_not_called()
