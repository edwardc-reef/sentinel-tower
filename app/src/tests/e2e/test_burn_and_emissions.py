"""E2E: burn and subnet-emission metrics against a real chain.

Covers the burn/emission analyst stories (§2). Two things only a real chain can
prove, and which the unit tests deliberately mock away:

- ``BittensorProvider`` reads real storage — the ``SubtensorModule.SubnetEmissionEnabled``
  map and block timestamps — and decodes the SCALE values the runtime actually
  returns.
- ``BurnService`` files those values against the right meta epoch and lands rows
  in Postgres.

Burn itself is not covered here (see QA.md): its calculation and anchor-block
recovery policy are DB/contact-boundary behavior covered through the public
service in tests/metagraph/test_burn_service.py.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from apps.metagraph.block_tasks import sync_subnet_emissions_for_block
from apps.metagraph.models import Block, MetaEpoch, SubnetEmission
from apps.metagraph.utils import get_epoch_containing_block

from .conftest import GENESIS_NETUID, Localnet

pytestmark = pytest.mark.django_db

# Root epochs are 361 blocks long, so the chain's first one starts at block 359 and
# there is no meta epoch before that. The daemon samples emissions *at* the anchor
# block as the chain reaches it; a node that prunes state cannot serve one much older,
# so the fixture below waits for a fresh anchor rather than reaching back for one.
MAX_ANCHOR_LAG_BLOCKS = 200
META_EPOCH_WAIT_SECONDS = 900


@pytest.fixture
def recent_meta_epoch_anchor(localnet: Localnet) -> int:
    """The latest root-epoch start block, once it is recent enough to still be readable."""
    deadline = time.monotonic() + META_EPOCH_WAIT_SECONDS
    while True:
        head = localnet.head()
        # A couple of blocks back from head is settled and inside the pruning window.
        anchor = get_epoch_containing_block(head - 2, 0).start
        if anchor >= 0 and head - anchor <= MAX_ANCHOR_LAG_BLOCKS:
            return anchor
        if time.monotonic() > deadline:
            raise AssertionError(f"no root-epoch start within {MAX_ANCHOR_LAG_BLOCKS} blocks of head {head}")
        time.sleep(5)


def test_block_timestamp_is_read_from_the_chain(localnet: Localnet) -> None:
    """§2 — the provider turns real chain timestamp storage into a sane UTC datetime."""
    timestamp = localnet.provider.get_block_timestamp(localnet.head() - 2)

    assert timestamp is not None

    assert timestamp.tzinfo is not None
    # The localnet mints blocks continuously, so any block near head is recent.
    assert abs((datetime.now(UTC) - timestamp).total_seconds()) < 3600


def test_subnet_emission_enabled_is_read_for_every_subnet(localnet: Localnet) -> None:
    """§2 — SubnetEmissionEnabled is read as a per-netuid map covering every subnet."""
    enabled = localnet.provider.get_subnet_emission_enabled(localnet.head() - 2)

    assert enabled is not None
    assert GENESIS_NETUID in enabled, "expected the genesis subnet in the emission map"
    assert all(isinstance(value, bool) for value in enabled.values())
    # The provider reports what the chain holds; dropping the root subnet is the
    # BurnService's policy, asserted in test_emission_sample_... below.
    assert set(enabled) == set(localnet.provider.get_all_subnets_netuids())


def test_emission_sample_lands_in_postgres_under_a_meta_epoch(
    localnet: Localnet, recent_meta_epoch_anchor: int
) -> None:
    """§2 — sampling at a meta-epoch start writes one row per subnet, keyed on that epoch."""
    anchor = recent_meta_epoch_anchor
    chain_values = localnet.provider.get_subnet_emission_enabled(anchor)
    chain_timestamp = localnet.provider.get_block_timestamp(anchor)
    assert chain_values is not None
    assert chain_timestamp is not None
    expected = {netuid: enabled for netuid, enabled in chain_values.items() if netuid != 0}
    ingested_block = Block.objects.create(number=anchor, timestamp=chain_timestamp)

    recorded = sync_subnet_emissions_for_block(anchor, localnet.provider)

    assert recorded == len(expected)
    meta_epoch = MetaEpoch.objects.get()
    assert meta_epoch.block == ingested_block
    assert meta_epoch.block.timestamp == chain_timestamp
    stored = dict(SubnetEmission.objects.filter(meta_epoch=meta_epoch).values_list("subnet_id", "emission_enabled"))
    assert stored == expected
    assert GENESIS_NETUID in stored


def test_non_meta_epoch_block_records_nothing(localnet: Localnet, recent_meta_epoch_anchor: int) -> None:
    """§2 — emissions are sampled once per meta epoch, not on every block."""
    anchor = recent_meta_epoch_anchor

    assert sync_subnet_emissions_for_block(anchor + 1, localnet.provider) == 0
    assert SubnetEmission.objects.count() == 0
    assert MetaEpoch.objects.count() == 0
