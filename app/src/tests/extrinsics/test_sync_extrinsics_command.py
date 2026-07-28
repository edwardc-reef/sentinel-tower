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
