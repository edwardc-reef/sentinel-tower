"""Gap-closing tests for the backfill_extrinsics command."""

from __future__ import annotations

from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from sentinel.v1.testing.providers import FakeBlockchainProvider

from apps.extrinsics.models import Extrinsic


def _raw_extrinsic(extrinsic_hash: str) -> dict[str, Any]:
    """One extrinsic in the flat shape the provider hands back for a block."""
    return {
        "index": 0,
        "extrinsic_hash": extrinsic_hash,
        "call_module": "SubtensorModule",
        "call_function": "burned_register",
        "call_args": [{"name": "netuid", "type": "u16", "value": 1}],
        "address": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "nonce": 7,
        "tip": 0,
    }


def _success_event() -> dict[str, Any]:
    """The System.ExtrinsicSuccess event the chain emits for an applied extrinsic."""
    return {
        "phase": {"ApplyExtrinsic": 0},
        "extrinsic_idx": 0,
        "event_index": "0x0000",
        "module_id": "System",
        "event_id": "ExtrinsicSuccess",
        "attributes": {},
        "topics": [],
    }


class BackfillProvider(FakeBlockchainProvider):
    """A chain with a configurable current head."""

    def __init__(self, head: int) -> None:
        super().__init__()
        self._head = head

    def get_current_block(self) -> int:
        return self._head

    def serve_block(self, block_number: int, extrinsic_hash: str) -> BackfillProvider:
        block_hash = f"0xblock{block_number}"
        self.with_block(block_number, block_hash)
        self.with_extrinsics(block_hash, [_raw_extrinsic(extrinsic_hash)])
        self.with_events(block_hash, [_success_event()])
        return self


@pytest.mark.django_db
@patch("apps.extrinsics.block_tasks.dispatch_block_notifications")
def test_gaps_are_closed_within_lookback_from_chain_head(mock_dispatch, monkeypatch):
    """Missing blocks in the inclusive head/lookback window are fetched.

    At head 1002, a lookback of two scans blocks 1000 through 1002. Block 1001 is
    already present, while block 999 falls immediately outside the scan window.
    """
    from apps.extrinsics.management.commands import backfill_extrinsics

    provider = BackfillProvider(head=1002)
    for block_number in (999, 1000, 1001, 1002):
        provider.serve_block(block_number, f"0xext{block_number}")
    monkeypatch.setattr(backfill_extrinsics, "bittensor_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(backfill_extrinsics, "get_archive_provider", lambda *a, **kw: provider)

    # Already ingested by the daemon, so not a gap.
    Extrinsic.objects.create(block_number=1001, extrinsic_hash="0xext1001", call_module="", call_function="")

    call_command("backfill_extrinsics", lookback=2, rate_limit=0, stdout=StringIO())

    assert sorted(Extrinsic.objects.values_list("block_number", flat=True)) == [1000, 1001, 1002]
    assert Extrinsic.objects.get(block_number=1000).extrinsic_hash == "0xext1000"
    assert Extrinsic.objects.get(block_number=1002).extrinsic_hash == "0xext1002"
    assert not Extrinsic.objects.filter(block_number=999).exists()
