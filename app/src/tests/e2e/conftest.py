"""Fixtures for end-to-end tests against a real subtensor localnet.

These tests drive the actual chain through the `bittensor` provider: they sign and
submit real extrinsics, then assert that Sentinel Tower's ingestion pipeline recorded
the consequences in Postgres and emitted the right Discord payloads.

The localnet is a *manual prerequisite* — the test suite never starts it. See QA.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import bittensor as bt
import pytest
import xxhash
from bittensor.sp_core import Keypair
from sentinel.v1.providers.bittensor import BittensorProvider, bittensor_provider

from apps.notifications import channels

DEFAULT_LOCALNET_URL = "ws://127.0.0.1:9944"

SUDO_URI = "//Alice"
SECONDARY_URI = "//Bob"

# How long to wait for a freshly started localnet to accept RPC and produce blocks.
CONNECT_TIMEOUT_SECONDS = 90

# A coldkey-swap announcement locks the account for InitialColdkeySwapAnnouncementDelay
# (~50) blocks before it can be cleared. At the localnet's ~0.3s/block that is ~16s;
# allow generous headroom for slower CI runners.
COLDKEY_SWAP_CLEAR_TIMEOUT_SECONDS = 90

# Subnet 1 is created by the localnet chainspec at genesis.
GENESIS_NETUID = 1

# A minimal subnet-registration lock cost (1000 TAO in rao). Reset before the suite so
# register_network is reliably affordable — the real cost otherwise ~doubles per
# registration and, on a long-lived chain, climbs past the sudo account's balance.
MIN_NETWORK_LOCK_COST_RAO = 1_000_000_000_000


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything under tests/e2e as `e2e` so it can be selected/excluded as a group."""
    for item in items:
        if "tests/e2e/" in item.nodeid or item.nodeid.startswith("tests/e2e"):
            item.add_marker("e2e")


def _localnet_url() -> str:
    return os.environ.get("E2E_LOCALNET_URL", DEFAULT_LOCALNET_URL)


def _get_expected_finney_runtime() -> int:
    """The localnet image should be using the same runtime as Finney"""
    # Blocking mode exposes spec_version as a property; reading it hits the chain.
    return bt.Subtensor(network="finney").spec_version


def _twox128(data: bytes) -> bytes:
    return b"".join(struct.pack("<Q", xxhash.xxh64(data, seed=seed).intdigest()) for seed in (0, 1))


def _storage_value_key(pallet: str, item: str) -> str:
    """Compute the storage key for a plain StorageValue (twox128(pallet) ++ twox128(item))."""
    return "0x" + (_twox128(pallet.encode()) + _twox128(item.encode())).hex()


def _network_added_netuid(events: Any) -> int | None:
    """Recover the netuid the chain assigned from the NetworkAdded event, if the call
    emitted one. register_network carries no netuid arg, so this is the only place the
    real assigned netuid is observable at submit time — tests assert against it.

    Reads the fully-decoded events the submission result carries; a failed extrinsic
    carries none, which is why this returns None rather than raising.
    """
    for event in events or []:
        value = getattr(event, "value", event)
        if not isinstance(value, dict):
            continue
        event_body = value.get("event")
        inner = event_body if isinstance(event_body, dict) else value
        if inner.get("event_id") != "NetworkAdded":
            continue
        attrs = inner.get("attributes")
        if isinstance(attrs, dict) and "netuid" in attrs:
            return int(attrs["netuid"])
        if isinstance(attrs, (list, tuple)) and attrs:
            return int(attrs[0])
    return None


@dataclass
class SubmittedExtrinsic:
    """The on-chain outcome of a submitted extrinsic."""

    block_number: int
    extrinsic_hash: str
    success: bool
    # The netuid the chain assigned, recovered from the NetworkAdded event (register_network only).
    netuid: int | None = None


@dataclass
class CapturedWebhooks:
    """Records Discord webhook deliveries instead of performing them.

    Discord is a true external boundary, so it is stubbed at the transport seam
    (`channels._http_client`). Everything below it — handler matching, sudo unwrapping,
    payload formatting — stays real.
    """

    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def urls(self) -> list[str]:
        return [url for url, _ in self.posts]

    def payloads_for(self, url: str) -> list[dict[str, Any]]:
        return [payload for posted_url, payload in self.posts if posted_url == url]

    def contents_for(self, url: str) -> list[str]:
        return [payload.get("content", "") for payload in self.payloads_for(url)]


class _RecordingHTTPClient:
    """Stands in for `httpx.Client`, recording posts and reporting success."""

    def __init__(self, captured: CapturedWebhooks) -> None:
        self._captured = captured

    def post(self, url: str, json: dict[str, Any] | None = None, **_: Any) -> Any:
        self._captured.posts.append((url, json or {}))
        return _NoOpResponse()


class _NoOpResponse:
    status_code = 204

    def raise_for_status(self) -> None:
        return None


class Localnet:
    """Drives the localnet: signs, submits, and reports where things landed."""

    def __init__(self, provider: BittensorProvider, subtensor: bt.Subtensor) -> None:
        self.provider = provider
        # The provider covers reads the ingestion pipeline itself makes; signing and
        # submitting is not part of its interface, so the tests hold their own client.
        self.subtensor = subtensor
        self.sudo_keypair = Keypair.create_from_uri(SUDO_URI)
        self.secondary_keypair = Keypair.create_from_uri(SECONDARY_URI)

    def head(self) -> int:
        return self.provider.get_current_block()

    def compose(self, module: str, function: str, params: dict[str, Any]) -> Any:
        """A call descriptor, addressed the way the SDK's generated catalog names it."""
        return getattr(getattr(bt.calls, module), function)(**params)

    def submit(self, call: Any, keypair: Keypair | None = None) -> SubmittedExtrinsic:
        """Submit a call and wait for inclusion, returning where it landed."""
        signer = keypair or self.sudo_keypair
        result = self.subtensor.submit_call(call, signer)
        if not result.extrinsic_id:
            raise AssertionError(f"localnet did not report where the extrinsic landed: {result.message}")
        # "<block number>-<extrinsic index>", the only place the SDK reports either.
        block_number, _, index = result.extrinsic_id.partition("-")
        return SubmittedExtrinsic(
            block_number=int(block_number),
            extrinsic_hash=self._extrinsic_hash(int(block_number), int(index)),
            success=bool(result.success),
            netuid=_network_added_netuid(result.events),
        )

    def _extrinsic_hash(self, block_number: int, index: int) -> str:
        """The hash of one extrinsic, read back through the ingestion provider.

        The SDK reports an extrinsic's position, not its hash, so the hash comes from
        the block itself — via the same provider the pipeline ingests with, which is
        exactly the value the assertions then look up in the database.
        """
        block_hash = self.provider.get_block_hash(block_number)
        if block_hash is None:
            raise AssertionError(f"localnet returned no block {block_number}")
        extrinsics = self.provider.get_extrinsics(block_hash) or []
        if index >= len(extrinsics):
            raise AssertionError(f"block {block_number} has no extrinsic at index {index}")
        return str(extrinsics[index]["extrinsic_hash"])

    def submit_sudo(self, module: str, function: str, params: dict[str, Any]) -> SubmittedExtrinsic:
        """Submit `Sudo.sudo(inner)` — the path governance actions actually take."""
        # A nested call has to be composed against the runtime before it can be a
        # parameter of the outer one.
        inner = self.subtensor.compose(self.compose(module, function, params))
        return self.submit(bt.calls.Sudo.sudo(call=inner))

    def fund(self, keypair: Keypair, rao: int) -> SubmittedExtrinsic:
        return self.submit_sudo("Balances", "force_set_balance", {"who": keypair.ss58_address, "new_free": rao})

    def reset_network_lock_cost(self) -> None:
        """Force the subnet-registration lock cost down to its minimum via sudo.

        The lock cost roughly doubles per registration and decays only slowly, so on a
        shared long-lived chain it eventually exceeds the sudo account's balance and
        register_network fails with CannotAffordLockCost. Resetting the stored last-lock
        keeps registration cheap and deterministic across runs.
        """
        key = _storage_value_key("SubtensorModule", "NetworkLastLockCost")
        value = "0x" + MIN_NETWORK_LOCK_COST_RAO.to_bytes(8, "little").hex()
        self.submit_sudo("System", "set_storage", {"items": [[key, value]]})

    def free_balance(self, keypair: Keypair) -> int:
        account = self.subtensor.query(bt.storage.System.Account, [keypair.ss58_address])
        free = account["data"]["free"]
        # Newer runtimes wrap balances in a single-element tuple.
        if isinstance(free, tuple):
            free = free[0]
        # A TaoBalance-typed field decodes to a Balance rather than a bare int.
        return int(getattr(free, "rao", free))

    def announce_coldkey_swap(self, keypair: Keypair | None = None) -> SubmittedExtrinsic:
        """Announce a real coldkey swap. The handler matches `announce_coldkey_swap`.

        This *locks* the signing account until the announcement matures
        (`InitialColdkeySwapAnnouncementDelay` blocks); callers must
        `clear_coldkey_swap_announcement(..., wait=True)` afterwards to unlock it.
        """
        assert self.secondary_keypair.public_key
        signer = keypair or self.sudo_keypair
        new_coldkey_hash = hashlib.blake2b(
            self.secondary_keypair.public_key,
            digest_size=32,
        ).hexdigest()
        return self.submit(
            self.compose("SubtensorModule", "announce_coldkey_swap", {"new_coldkey_hash": "0x" + new_coldkey_hash}),
            keypair=signer,
        )

    def clear_coldkey_swap_announcement(
        self,
        keypair: Keypair | None = None,
        *,
        wait: bool = False,
        timeout_seconds: float = COLDKEY_SWAP_CLEAR_TIMEOUT_SECONDS,
    ) -> bool:
        """Clear a pending coldkey-swap announcement, unlocking the account.

        Before the announcement matures the clear is *included but fails on-chain*
        (``is_success == False``) rather than raising — so we must check the extrinsic's
        on-chain result, not merely that it was submitted. With ``wait=True``, poll until
        it actually succeeds or the timeout elapses. Returns True if the account is
        confirmed clear. Best-effort with ``wait=False`` (session self-heal).
        """
        signer = keypair or self.sudo_keypair
        deadline = time.monotonic() + timeout_seconds
        while True:
            # A pre-maturity clear is included-but-failed; a rejected one raises. Neither
            # is fatal — keep polling until it actually succeeds or we run out of time.
            with contextlib.suppress(Exception):
                result = self.submit(
                    self.compose("SubtensorModule", "clear_coldkey_swap_announcement", {}),
                    keypair=signer,
                )
                if result.success:
                    return True
            if not wait or time.monotonic() >= deadline:
                return False
            time.sleep(1)


@pytest.fixture(scope="session")
def localnet() -> Iterator[Localnet]:
    """Connect to the localnet and prove its topology before any test relies on it."""
    url = _localnet_url()
    # `bittensor_provider()` and the management commands under test read this env var,
    # so point the whole process at the localnet rather than finney.
    os.environ["BITTENSOR_NETWORK"] = url

    # A freshly started localnet needs a moment before its RPC is ready and it has
    # produced blocks. Retry rather than fail the instant the socket is not up yet.
    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    provider = None
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            provider = bittensor_provider(url)
            provider.__enter__()
            if provider.get_current_block() > 0:
                break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if provider is not None:
                with contextlib.suppress(Exception):
                    provider.close()
                provider = None
            time.sleep(2)
    else:
        pytest.fail(
            f"Cannot reach a subtensor localnet at {url} within {CONNECT_TIMEOUT_SECONDS}s: {last_exc!r}\n"
            f"E2E tests require a manually started localnet. Run:\n"
            f"  docker compose --profile e2e up -d localnet\n"
            f"See QA.md for details.",
            pytrace=False,
        )

    assert provider is not None
    subtensor = bt.Subtensor(network=url)
    chain = Localnet(provider, subtensor)

    # TODO: There have been instances where Finney's runtime did not match the main runtime, causing code that
    # works on Finney to not work on the tests. There's no hard and fast rule when this happens, though, as far
    # as I'm aware and it's not an issue right now but I'm keeping the version check here in case someone runs
    # into this problem in the future.
    # spec_version = chain.subtensor.spec_version
    # finney_spec_version = _get_expected_finney_runtime()
    # if spec_version != finney_spec_version:
    #     provider.close()
    #     pytest.fail(
    #         f"Localnet at {url} runs runtime specVersion {spec_version}, "
    #         f"but these tests require {finney_spec_version} (finney's version).\n"
    #         f"The localnet image is pinned by digest in envs/dev/docker-compose.yml; "
    #         f"see QA.md for how to move the pin.",
    #         pytrace=False,
    #     )

    # Prove the actor roles rather than trusting the fixture's naming.
    on_chain_sudo = chain.subtensor.query(bt.storage.Sudo.Key)
    on_chain_sudo_address = getattr(on_chain_sudo, "value", on_chain_sudo)
    assert on_chain_sudo_address == chain.sudo_keypair.ss58_address, (
        f"{SUDO_URI} is not the sudo key on this chain (sudo is {on_chain_sudo_address}); "
        f"the governance tests would not exercise the real sudo path."
    )

    subnet_exists = chain.subtensor.query(bt.storage.SubtensorModule.NetworksAdded, [GENESIS_NETUID])
    assert getattr(subnet_exists, "value", subnet_exists) is True, (
        f"netuid {GENESIS_NETUID} does not exist on this localnet; the chainspec is not the expected one."
    )

    # Self-heal: a coldkey-swap announcement left pending by an aborted prior run locks
    # the sudo account, rejecting every extrinsic it signs. Best-effort clear up front so
    # the suite is not hostage to a previous run's cleanup having completed. Any run gap
    # of >~50 blocks (seconds) means a stale announcement has matured and clears here.
    with contextlib.suppress(Exception):
        chain.clear_coldkey_swap_announcement()

    yield chain

    subtensor.close()
    provider.close()


@pytest.fixture(scope="session")
def _funded_sudo(localnet: Localnet) -> None:
    """Prepare the sudo account for repeatable submission on a long-lived chain.

    Tops up its balance (fees + lock costs accumulate across runs) and resets the
    subnet-registration lock cost so register_network stays affordable.
    """
    one_million_tao_in_rao = 1_000_000_000_000_000
    if localnet.free_balance(localnet.sudo_keypair) < one_million_tao_in_rao // 2:
        localnet.fund(localnet.sudo_keypair, one_million_tao_in_rao)
    localnet.reset_network_lock_cost()


@pytest.fixture(autouse=True)
def captured_webhooks(monkeypatch: pytest.MonkeyPatch) -> CapturedWebhooks:
    """Capture Discord deliveries. Autouse so no e2e test can post to a real webhook."""
    captured = CapturedWebhooks()
    monkeypatch.setattr(channels, "_http_client", _RecordingHTTPClient(captured))
    return captured


@pytest.fixture
def discord_webhooks(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Configure every env-var-backed Discord channel with a distinguishable URL."""
    urls = {
        "admin_utils": "https://discord.test/webhooks/admin-utils",
        "subnet_registration": "https://discord.test/webhooks/subnet-registration",
        "sudo": "https://discord.test/webhooks/sudo",
        "coldkey_swap": "https://discord.test/webhooks/coldkey-swap",
    }
    monkeypatch.setenv("DISCORD_ADMIN_UTILS_ALERTS_WEBHOOK_URL", urls["admin_utils"])
    monkeypatch.setenv("DISCORD_SUBNET_REGISTRATION_WEBHOOK_URL", urls["subnet_registration"])
    monkeypatch.setenv("DISCORD_SUDO_ALERTS_WEBHOOK_URL", urls["sudo"])
    monkeypatch.setenv("DISCORD_COLDKEY_SWAP_WEBHOOK_URL", urls["coldkey_swap"])
    return urls
