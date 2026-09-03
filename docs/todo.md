# TODO

## Seed remaining `subtensor_error_codes` entries

Migration `0009_subtensor_error_code` introduced the `subtensor_error_codes` lookup table powering the **Top Error Types** panel on the Weight Setting dashboard. Only the highest-confidence entry is seeded so far:

- `0x1d000000` → `CommitRevealEnabled`

The next-most-frequent codes still showing as raw hex in the panel:

| code | empirical narrowing (which dispatchables emit it) | candidate names from `set-weights.md` |
|---|---|---|
| `0x4d000000` | timelock only (`commit_timelocked_*`) | `TooManyUnrevealedCommits` or `CommitRevealV3Disabled` |
| `0x51000000` | timelock only | the other of the above pair |
| `0x04000000` | direct + timelock (shared check) | likely `NotRegistered` |
| `0x4a000000` | timelock-mechanism only | mechanism-specific check |
| `0x0a000000` | mechanism-direct only | likely `MechanismDoesNotExist` |
| `0x0f000000` | mechanism-direct only | unknown |
| `0x35000000` | mechanism-direct only (single occurrence) | unknown |

**Action:** confirm each variant index against `pallets/subtensor/src/errors.rs` in the [opentensor/subtensor](https://github.com/opentensor/subtensor) repo, then add a follow-up Django migration that `INSERT … ON CONFLICT … DO UPDATE`s each row. Once the bulk are seeded, consider promoting to **Option C** (decode at ingestion using runtime metadata) so new variants don't require a migration with each chain upgrade.

## Batch weight-setting extrinsics: per-subnet attribution

The `bittensor-metrics` Grafana dashboard ("Weight-Setting Calls Analysis" row) groups by `extrinsics.netuid`, but three of the twelve weight-setting dispatchables carry a `Vec<NetUid>` rather than a single netuid:

- `batch_set_weights` (call index 80)
- `batch_commit_weights` (call index 100)
- `batch_reveal_weights` (call index 98)

The `extrinsics` table stores a single `netuid` per row, so when these batch calls land on chain their per-subnet attribution will be missing or collapsed in the dashboard panels.

**Action:** audit the extrinsic extractor in `apps.extrinsics` to confirm what it writes for batch calls (NULL? first netuid? row-per-netuid?). If a single batch row maps to many subnets, decide whether to:

1. Expand batch extrinsics into one row per `(extrinsic, netuid)` at ingest time, or
2. Add a side table (e.g. `extrinsic_netuids`) and join it from the dashboard queries.

Reference: [set-weights.md](../set-weights.md), dashboard [grafana/provisioning/dashboards/bittensor-metrics.json](../grafana/provisioning/dashboards/bittensor-metrics.json).

## Per-client read-only postgres role provisioning

Issuing a client cert via `db_access_certs/issue-client.sh` only gates *transport* — anyone with a valid client cert still needs a postgres role + password to actually query. Today this is a manual step on the prod host.

**Action:** add a companion `db_access_certs/create-readonly-user.sh` (run on the prod host, separate from `issue-client.sh`) that takes a CN, creates `r_<cn>` with a generated password, grants `CONNECT` + appropriate `USAGE`/`SELECT`, and prints the password once. Update [docs/postgres-mtls.md](postgres-mtls.md) to make the two gates explicit: cert proves "you reach the proxy," postgres role proves "you are this DB user."

**Why separate from `issue-client.sh`:** that script runs on a workstation holding the offline CA key; it must not need network access to prod or DB admin credentials. Coupling cert issuance with live-DB role creation mixes two trust domains.

## Upgrade PostgreSQL to 17+ so `pg_stat_statements` survives Django savepoints

`pg_stat_statements` (now exported to Prometheus by `postgres-exporter` and shown on the **PostgreSQL** dashboard) is flooded by Django savepoints. The sync daemons call `update_or_create` / `get_or_create` inside the outer `transaction.atomic()` in `apps/metagraph/services/metagraph_sync_service.py`; each of those opens its own savepoint, and Django names them uniquely (`SAVEPOINT "s<thread>_x<n>"`), so every one becomes a distinct `pg_stat_statements` entry. Measured locally: ~200 new entries/minute, 9,670 of 9,730 rows were `SAVEPOINT`/`RELEASE SAVEPOINT`, 58 were real queries, and real queries were being evicted within hours. This is the mechanism behind the 89k evictions/day in [postgres-tuning.md](postgres-tuning.md); raising `pg_stat_statements.max` only delays it.

Verified against `postgres:{14,16,17,18}-alpine`: 14 and 16 keep one row per savepoint name; **17 and 18 normalise them to a single `savepoint $1` row** (PostgreSQL ≥ 17 ignores the savepoint name when computing the query id).

**Interim workaround (PostgreSQL 14):** `pg_stat_statements.track_utility=off` on the `db` command. Savepoints stop being recorded and real queries stay in the table permanently; `pg_stat_statements.max` can come back down from 50000 (the exporter reads the whole table every scrape, so a smaller table is cheaper). Cost: `REFRESH MATERIALIZED VIEW` and `VACUUM` no longer appear in `pg_stat_statements` — they still appear in the prod slow-query log (`log_min_duration_statement=2000`, `auto_explain`).

**Action:**

1. Plan a major-version upgrade 14 → 17 (`pg_upgrade` or dump/restore of the multi-GB data directory; note that the `postgres` image changes its default `PGDATA` path from 18 on). When done, remove `track_utility=off`.
2. Until then, expose the APY materialized-view refresh duration directly from `apps/metagraph/tasks.py` (elapsed time on the existing "Refreshed …" log line and a `django-business-metrics` gauge/histogram scraped via `/business-metrics`) so the refresh — historically the DB's most fragile operation — has a first-class metric independent of `pg_stat_statements`.

Alternative that avoids both: rewrite the per-neuron writes as bulk upserts (`bulk_create(update_conflicts=True)`) so no savepoints are emitted. Larger change with different error semantics; not preferred.
