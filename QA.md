# QA

Binding guidance for test work in this repository. Read before adding, deleting, or
auditing tests.

The project-wide testing rules — concrete-value assertions, mock and `Contact`
boundaries, public-API testing, real-contact integration, stateful infrastructure — live
in [engineering-standards.md](engineering-standards.md) and are not repeated here. This
file records only what is *specific to Sentinel Tower*: how to run the suites, what
deserves an e2e test here, the localnet constraints, and the QA decision log. Where the
two ever disagree, engineering-standards.md wins.

## Test suites and how to run them

Two suites, both under `app/src`:

- **Unit / integration** (`app/src/tests/`, `app/src/project/**/tests/`): fast, hit
  Postgres + Redis, mock the chain. Run with `nox -s test`.
- **End-to-end** (`app/src/tests/e2e/`): drive a real subtensor localnet — sign and
  submit real extrinsics, then assert Sentinel Tower's pipeline recorded the
  consequences. Run with `nox -s test_e2e`.

Prerequisites (manual, per the repo convention that test setup never starts containers):

```bash
docker compose up -d db redis                          # unit + e2e need these
docker compose --profile e2e up -d localnet            # e2e also needs the chain
```

The e2e localnet is pinned by digest to `ghcr.io/raofoundation/subtensor-localnet:latest`.

## What deserves an e2e test here

E2e tests exist to prove the seam unit tests cannot: that **real chain data flows
through our parsing and persistence correctly**. Unit tests feed synthetic dicts to
parsers and handlers; only e2e proves the bittensor SDK's actual output shape matches
what the code expects. So an e2e test earns its place when it exercises a real
chain → ingestion → DB/notification path end to end. Anything that is pure DB or pure
formatting logic belongs in a unit test, not here.

The e2e suite drives only the `bittensor` provider, not `pylon` — keeping the two
providers equivalent is the SDK's concern, and this avoids running a pylon instance.

## E2e design rules (learned the hard way)

The localnet is a **single, long-lived, shared** chain with one sudo account (`//Alice`).
That shapes the whole design:

- **Serial only.** Tests share one account; parallel workers collide on nonces. The
  `test_e2e` nox session forces `-n0`. Never run the e2e suite under `-n auto`.
- **Submit once, ingest per-test.** Chain writes are slow and append-only; DB writes roll
  back per test. Module-scoped fixtures submit the extrinsics once; each test re-ingests
  the blocks into a fresh transaction-isolated DB.
- **Self-heal at session start.** The chain accumulates state across runs, and any aborted
  run can leave it poisoned. The `localnet` fixture clears stale coldkey-swap
  announcements and resets the subnet-registration lock cost so the suite is idempotent —
  it passes run after run without manual chain resets.
- **Prove topology, don't assume it.** The fixture asserts the runtime version, that
  `//Alice` really is the sudo key, and that the genesis subnet exists, before any test
  depends on those facts.

## Decision log

- **Metagraph/APY/explorer are covered e2e** against runtime 424 after we found the
  localnet image (not the SDK) was the blocker. `v3.4.9-424` is the required image.
- **Coldkey-swap announcement alerting (§4.2) IS covered e2e.** A real
  `announce_coldkey_swap` locks the signing account until it matures
  (`InitialColdkeySwapAnnouncementDelay` = 50 blocks, ~20s at the localnet's block time).
  The test asserts the alert on the central channel, then waits for the announcement to
  mature and clears it so the account is unlocked for later tests. Two subtleties the
  clear must respect: a pre-maturity clear is *included but fails on-chain* (so check the
  extrinsic's on-chain result, not just that it was submitted), and the session fixture
  best-effort-clears any stale announcement up front so an aborted run self-heals.
- **§4.6 "only successful extrinsics notify" needs a failed extrinsic that _matches a
  handler_.** An earlier version asserted the failed `burned_register`'s hash was absent
  from all webhook content — vacuous, since `burned_register` matches no handler and only
  the registration handler ever emits a hash. The success filter (`base.py`) only runs
  after a handler matches, so the test now submits a **direct** `AdminUtils.sudo_set_tempo`
  (`failed_handled` in the batch): AdminUtils calls require root, so submitted un-wrapped
  it is included-but-failed with `BadOrigin` while still matching `AdminUtilsNotification`.
  A Sudo-_wrapped_ failure would not work — the outer sudo extrinsic succeeds, so its
  recorded `success` is True. Its block is ingested in isolation and the hyperparam
  channel must stay empty.
- **The real assigned netuid is captured via `substrate.get_events(block_hash)`, not the
  receipt.** `register_network` carries no netuid arg; the chain assigns one and reports
  it in the `NetworkAdded` event. `SubmittedExtrinsic.netuid` captures it so tests assert
  the ingested value _equals_ it (not just `> genesis`). The receipt's `triggered_events`
  decode lazily and return `None` right after submission — `substrate.get_events` returns
  fully-decoded events reliably.
- **Per-subnet webhook routing / enable-disable toggle (§4.3/§4.4) stays unit-tested.**
  That is pure DB logic (`DatabaseWebhookChannel` filtering `enabled=True`), thoroughly
  covered in `tests/notifications/test_channels.py`; a real chain adds nothing to it.
- **Lite-vs-full metagraph (§2.3) is not distinguishable e2e** on this localnet: subnet 1
  carries one neuron and no weights/bonds, so both modes yield the same rows. The lite
  path is covered by the metagraph sync-service unit tests.
- **APY (§2.4) is not e2e**: it needs multi-epoch dividend history the localnet does not
  accrue. The APY view is unit-tested (`tests/metagraph/test_apy_epoch_view.py`).
- **Burn/emission metrics are covered e2e only for the chain reads.**
  `tests/e2e/test_burn_and_emissions.py` proves `BittensorProvider.get_subnet_emission_enabled`
  and `get_block_timestamp` decode the real runtime's values correctly and
  that a sample lands as `MetaEpoch` + `SubnetEmission` rows using an ingested anchor
  `Block`. Burn itself is **not** e2e because its arithmetic is pure DB logic; the same
  public-service unit tests cover that calculation, reuse of an ingested block, creation
  of a missing block with empty dump metadata, and live-to-archive timestamp fallback.
- **The emission fixture waits for a fresh anchor rather than reaching back for one.**
  Root-epoch starts are 361 blocks apart, so the latest one can be up to 360 blocks behind
  head and unreadable on a pruning node. `recent_meta_epoch_anchor` waits until an anchor
  is within 200 blocks of head — the same position the daemon sees it from.
- **Error-code seed was fixed, not just tested.** The e2e failure-decoding test
  (`§1.5`) surfaced that migration 0010 seeded `subtensor_error_codes` from a stale enum
  ordering — off by one from index 23, so 94 of 135 codes decoded to the wrong name.
  Migration 0013 regenerates all 147 codes from the runtime-424 metadata.
