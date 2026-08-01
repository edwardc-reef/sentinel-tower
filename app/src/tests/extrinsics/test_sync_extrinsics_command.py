"""Connection-handling test for the sync_extrinsics daemon.

Same failure mode as tests/metagraph/test_sync_metagraph_command.py: when a chain call
fails the daemon reopens its provider, and that reopen — a websocket handshake against an
endpoint that is already misbehaving — can time out too. It must be retried, not fatal.
"""

from __future__ import annotations

import signal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.test import override_settings
from sentinel.v1.testing.providers import FakeBlockchainProvider
from structlog.testing import capture_logs

from apps.extrinsics.management.commands import sync_extrinsics
from apps.extrinsics.models import Extrinsic

HANDSHAKE_TIMEOUT = TimeoutError("timed out during handshake")


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """The daemon installs SIGTERM/SIGINT handlers; don't leak them into other tests."""
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in original.items():
        signal.signal(sig, handler)


class ScriptedProvider(FakeBlockchainProvider):
    """Fake provider that either refuses to connect, fails on use, or reports a head."""

    def __init__(
        self,
        head: int = 0,
        *,
        unreachable: bool = False,
        fail_blocks: bool = False,
        on_head: Any = None,
    ) -> None:
        super().__init__()
        self._head = head
        self._unreachable = unreachable
        self._fail_blocks = fail_blocks
        self._on_head = on_head
        self.head_calls = 0
        self.close_calls = 0
        self.closed = False

    def __enter__(self) -> ScriptedProvider:
        if self._unreachable:
            raise HANDSHAKE_TIMEOUT
        return self

    def get_current_block(self) -> int:
        self.head_calls += 1
        if self._on_head is not None:
            self._on_head()
        if self._head == 0:
            raise HANDSHAKE_TIMEOUT
        return self._head

    def get_block_hash(self, block_number: int) -> str | None:
        if self._fail_blocks:
            raise HANDSHAKE_TIMEOUT
        return super().get_block_hash(block_number)

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


@override_settings(
    BITTENSOR_SECONDS_PER_BLOCK=0,
    BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS=0,
    BITTENSOR_RECONNECT_MAX_DELAY_SECONDS=0,
    BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS=2,
)
def test_successful_reconnect_does_not_reset_outage_until_rpc_succeeds(monkeypatch):
    command = sync_extrinsics.Command()

    def request_shutdown() -> None:
        command._shutdown = True

    failing = ScriptedProvider()
    failing_again = ScriptedProvider()
    healthy = ScriptedProvider(head=1000, on_head=request_shutdown)
    providers = [failing, failing_again, healthy]
    monkeypatch.setattr(sync_extrinsics, "bittensor_provider", lambda *a, **kw: providers.pop(0))

    with capture_logs() as logs:
        call_command(command, stdout=StringIO())

    assert providers == []
    assert failing.head_calls == 1
    assert failing_again.head_calls == 1
    assert healthy.head_calls == 1
    assert failing.closed is True
    assert failing_again.closed is True
    assert healthy.closed is True
    failures = [
        entry["log_level"]
        for entry in logs
        if entry["event"] in {"Connection error fetching head, reconnecting...", "Provider connection failed"}
    ]
    assert failures == ["warning", "error"]
    recoveries = [entry for entry in logs if entry["event"] == "Provider connection recovered"]
    assert [entry["failed_attempts"] for entry in recoveries] == [2]


@override_settings(
    BITTENSOR_SECONDS_PER_BLOCK=0,
    BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS=0,
    BITTENSOR_RECONNECT_MAX_DELAY_SECONDS=0,
    BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS=2,
)
def test_successful_head_rpc_resets_outage_before_catch_up_failure(monkeypatch):
    command = sync_extrinsics.Command()

    def request_shutdown() -> None:
        command._shutdown = True

    head_failure = ScriptedProvider()
    catch_up_failure = ScriptedProvider(head=1000, fail_blocks=True)
    healthy = ScriptedProvider(head=1001, on_head=request_shutdown)
    providers = [head_failure, catch_up_failure, healthy]
    monkeypatch.setattr(sync_extrinsics, "bittensor_provider", lambda *a, **kw: providers.pop(0))

    with capture_logs() as logs:
        call_command(command, stdout=StringIO())

    failures = [
        entry
        for entry in logs
        if entry["event"]
        in {
            "Connection error fetching head, reconnecting...",
            "Error processing block, reconnecting...",
        }
    ]
    assert [(entry["event"], entry["log_level"], entry["attempt"]) for entry in failures] == [
        ("Connection error fetching head, reconnecting...", "warning", 1),
        ("Error processing block, reconnecting...", "warning", 1),
    ]
    recoveries = [entry for entry in logs if entry["event"] == "Provider connection recovered"]
    assert [entry["failed_attempts"] for entry in recoveries] == [1, 1]


@pytest.mark.django_db
@override_settings(
    BITTENSOR_SECONDS_PER_BLOCK=0,
    BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS=0,
    BITTENSOR_RECONNECT_MAX_DELAY_SECONDS=0,
    BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS=2,
)
def test_unavailable_block_keeps_provider_and_continues_ingestion(monkeypatch):
    command = sync_extrinsics.Command()
    provider_head_calls = 0

    def request_shutdown_after_ingestion() -> None:
        nonlocal provider_head_calls
        provider_head_calls += 1
        if provider_head_calls == 2:
            provider._head = 1001
        elif provider_head_calls == 3:
            command._shutdown = True

    provider = ScriptedProvider(head=1000, on_head=request_shutdown_after_ingestion)
    provider.with_block(1000, "0xunavailable")
    provider.with_block(1001, "0xhealthy").with_extrinsics(
        "0xhealthy",
        [
            {
                "index": 0,
                "extrinsic_hash": "0xextrinsic",
                "call_module": "System",
                "call_function": "remark",
                "call_args": [],
            },
        ],
    ).with_events(
        "0xhealthy",
        [
            {
                "phase": {"ApplyExtrinsic": 0},
                "extrinsic_idx": 0,
                "event_index": "0x0000",
                "module_id": "System",
                "event_id": "ExtrinsicSuccess",
                "attributes": {},
                "topics": [],
            },
        ],
    )
    providers = [provider]
    monkeypatch.setattr(sync_extrinsics, "bittensor_provider", lambda *a, **kw: providers.pop(0))

    with capture_logs() as logs:
        call_command(command, stdout=StringIO())

    assert providers == []
    assert provider.head_calls == 3
    assert provider.close_calls == 1
    assert provider.closed is True
    assert list(Extrinsic.objects.values_list("block_number", "extrinsic_hash")) == [(1001, "0xextrinsic")]
    unavailable = [entry for entry in logs if entry["event"] == "Block unavailable; leaving gap for backfill"]
    assert [(entry["block_number"], entry["log_level"]) for entry in unavailable] == [
        (1000, "warning"),
    ]
    synced = [entry for entry in logs if entry["event"] == "Extrinsics synced"]
    assert [(entry["block"], entry["extrinsics"]) for entry in synced] == [(1001, 1)]
