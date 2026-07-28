"""Connection-handling tests for the sync_metagraph daemon.

When a provider call fails mid-sync the daemon closes the provider and opens a new one.
Opening it is itself a network call (a websocket handshake against the chain endpoint),
attempted exactly when that endpoint is already misbehaving — so it fails too, often.
The daemon must survive that instead of dying with an unhandled TimeoutError.
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

from apps.metagraph.management.commands import sync_metagraph

NETUID = 1
# Dumpable blocks for netuid 1 are (358, 478, 598, 718): epoch start is where
# (block + netuid + 2) % 361 == 0, plus two intermediate points and the epoch end.
DUMPABLE_BLOCK = 358
NEXT_DUMPABLE_BLOCK = 478

HANDSHAKE_TIMEOUT = TimeoutError("timed out during handshake")

RETRY_SETTINGS = {
    "METAGRAPH_NETUIDS": [NETUID],
    "BITTENSOR_SECONDS_PER_BLOCK": 0,
    "BITTENSOR_RECONNECT_INITIAL_DELAY_SECONDS": 0,
    "BITTENSOR_RECONNECT_MAX_DELAY_SECONDS": 0,
}


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """The daemon installs SIGTERM/SIGINT handlers; don't leak them into other tests."""
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in original.items():
        signal.signal(sig, handler)


class ScriptedProvider(FakeBlockchainProvider):
    """Fake provider that scripts the chain head and records what the daemon asked for."""

    def __init__(
        self,
        heads: list[int],
        *,
        unreachable: bool = False,
        fail_metagraph: bool = False,
        on_heads_exhausted: Any = None,
    ) -> None:
        super().__init__()
        self._heads = list(heads)
        self._unreachable = unreachable
        self._fail_metagraph = fail_metagraph
        self._on_heads_exhausted = on_heads_exhausted
        self.metagraph_requests: list[tuple[int, int | None]] = []
        self.head_calls = 0
        self.closed = False

    def __enter__(self) -> ScriptedProvider:
        if self._unreachable:
            raise HANDSHAKE_TIMEOUT
        return self

    def get_current_block(self) -> int:
        self.head_calls += 1
        if len(self._heads) > 1:
            return self._heads.pop(0)
        if self._on_heads_exhausted is not None:
            self._on_heads_exhausted()
        return self._heads[0]

    def get_mechanism_count(self, netuid: int, block_number: int | None = None) -> int:
        self.metagraph_requests.append((netuid, block_number))
        if self._fail_metagraph:
            raise HANDSHAKE_TIMEOUT
        return 0

    def close(self) -> None:
        self.closed = True


class ProviderFactory:
    """Stands in for `bittensor_provider()`, handing out scripted providers in order."""

    def __init__(self, *providers: ScriptedProvider, on_call: Any = None) -> None:
        self._providers = list(providers)
        self._on_call = on_call
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> ScriptedProvider:
        self.call_count += 1
        if self._on_call is not None:
            self._on_call(self.call_count)
        return self._providers.pop(0)


@override_settings(**RETRY_SETTINGS)
def test_daemon_keeps_syncing_after_a_reconnect_attempt_also_times_out(monkeypatch):
    command = sync_metagraph.Command()

    def request_shutdown() -> None:
        command._shutdown = True

    failing = ScriptedProvider([DUMPABLE_BLOCK], fail_metagraph=True)
    unreachable = ScriptedProvider([], unreachable=True)
    healthy = ScriptedProvider(
        [NEXT_DUMPABLE_BLOCK, NEXT_DUMPABLE_BLOCK],
        on_heads_exhausted=request_shutdown,
    )
    factory = ProviderFactory(failing, unreachable, healthy)
    monkeypatch.setattr(sync_metagraph, "bittensor_provider", factory)

    call_command(command, provider="bittensor", stdout=StringIO())

    assert factory.call_count == 3
    assert failing.metagraph_requests == [(NETUID, DUMPABLE_BLOCK)]
    assert healthy.metagraph_requests == [(NETUID, NEXT_DUMPABLE_BLOCK)]
    assert failing.closed is True
    assert unreachable.closed is True
    assert healthy.closed is True


@override_settings(**RETRY_SETTINGS, BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS=3)
def test_daemon_alerts_on_repeated_connection_failures_and_stops_when_signalled(monkeypatch):
    command = sync_metagraph.Command()

    def request_shutdown_on_last_attempt(attempt: int) -> None:
        if attempt == 4:
            command._shutdown = True

    factory = ProviderFactory(
        *(ScriptedProvider([], unreachable=True) for _ in range(4)),
        on_call=request_shutdown_on_last_attempt,
    )
    monkeypatch.setattr(sync_metagraph, "bittensor_provider", factory)

    with capture_logs() as logs:
        call_command(command, provider="bittensor", stdout=StringIO())

    assert factory.call_count == 4
    failures = [entry["log_level"] for entry in logs if entry["event"] == "Provider connection failed"]
    assert failures == ["warning", "warning", "error", "warning"]


@override_settings(**RETRY_SETTINGS, BITTENSOR_RECONNECT_ALERT_AFTER_ATTEMPTS=2)
def test_successful_head_rpc_resets_outage_before_catch_up_failure(monkeypatch):
    command = sync_metagraph.Command()

    def request_shutdown() -> None:
        command._shutdown = True

    unreachable = ScriptedProvider([], unreachable=True)
    catch_up_failure = ScriptedProvider([DUMPABLE_BLOCK], fail_metagraph=True)
    healthy = ScriptedProvider([NEXT_DUMPABLE_BLOCK], on_heads_exhausted=request_shutdown)
    factory = ProviderFactory(unreachable, catch_up_failure, healthy)
    monkeypatch.setattr(sync_metagraph, "bittensor_provider", factory)

    with capture_logs() as logs:
        call_command(command, provider="bittensor", stdout=StringIO())

    failures = [
        entry
        for entry in logs
        if entry["event"]
        in {
            "Provider connection failed",
            "Error syncing metagraph, reconnecting...",
        }
    ]
    assert [(entry["event"], entry["log_level"], entry["attempt"]) for entry in failures] == [
        ("Provider connection failed", "warning", 1),
        ("Error syncing metagraph, reconnecting...", "warning", 1),
    ]
    recoveries = [entry for entry in logs if entry["event"] == "Provider connection recovered"]
    assert [entry["failed_attempts"] for entry in recoveries] == [1, 1]
