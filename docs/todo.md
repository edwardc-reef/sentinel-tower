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
**Why deferred:** the point-in-time dashboard already answers the immediate "which tables/indexes are biggest" question, and adding an exporter is a separate deploy-touching change (new container, new scrape target, secrets). Bundling them would slow the dashboard ship.

## Sample `pg_stat_activity` into a history table

The DB Query Performance dashboard is point-in-time, and `pg_stat_statements` never records a statement that was cancelled before it finished.
On 2026-09-02 that blind spot hid the worst prod offenders: about 190 Grafana panel queries a day cancelled at the 60 s data-proxy timeout (see [postgres-query-performance.md](postgres-query-performance.md)).

**Action:** add a `sample_db_activity` management command run as a compose profile service that samples `pg_stat_activity` every few seconds into a table keyed on `(pid, query_start)` with `usename`, `application_name`, `wait_event`, `state` and the statement text, with 7-day retention hooked into `cleanup_expired_data`.
Then add "slow statements over time" panels to the dashboard.

**Why deferred:** it is app code plus a migration, and the cancelled queries it would have caught are one known external dashboard that can be fixed directly.

## PostgreSQL dashboard: replace the read-wait SQL tile, fix the retention panel's cost

Two follow-ups from folding DB Size & Retention into the PostgreSQL dashboard (September 2026).

**Read-wait tile.** "Read wait, share of active time" was copied from DB Query Performance as is: a SQL tile, cumulative since the stats reset, next to 5-minute Prometheus tiles.
A share cannot be rebuilt from `pg_stat_statements`: parallel workers add their read waits to a statement while its execution time stays the leader's wall clock, so on a quiet database the ratio exceeds 100 % (221 % on a dev box, from one parallel `MIN(created_at)` scan).
**Action:** replace it with a Prometheus tile "Waiting on disk reads", `sum(rate(pg_stat_statements_block_read_seconds_total[5m]))`, processes waiting at any instant, absolute thresholds (yellow above 1, red above the core count). If a percentage is wanted, derive it from `pg_stat_database` (`blk_read_time` over `active_time`), which counts workers consistently; check first that the exporter publishes `active_time`.

**Retention panel cost.** "Retention focus (per major table)" finds the oldest row of four tables with `MIN(created_at)`; without an index on those columns each run is a parallel sequential scan, about 16 s on a 10 GB dev database.
It was harmless on DB Size & Retention, which refreshed every 5 minutes, but the PostgreSQL dashboard refreshes every minute.
**Action:** one of: an index on each `created_at`/`timestamp`/`finished_at` column used, a cheaper source for the oldest row, or a per-panel interval of 1 h or more so it stops following the board's refresh. Decide before the move is deployed.

## Dashboard query checker: generalise or delete

`scripts/check_dashboard_queries.py` runs a dashboard's SQL through Grafana's query API, but only for boards with no template variables and only `postgresql` targets.
The two boards it was written for were folded into the PostgreSQL dashboard in September 2026, which has both variables and Prometheus panels, so it currently checks nothing in the repo.

**Action:** either extend it or delete it. Extending needs three changes: send each target to its own datasource (`expr` + `instant` for Prometheus, `rawSql` + `format` for Postgres) instead of asserting `postgresql`; substitute dashboard variables from each variable's `current` value in the file, rendering `$var` and `${var}` as a regex alternation for PromQL and `${var:sqlstring}` as a quoted list for SQL, plus `$__range` as `1h`; and accept a directory so one run covers every provisioned board.
It would still not exercise transformations (joins, calculated columns, ordering), which is where the September 2026 breakages were, and it has no place to run: wire it into the nox lint session or the deploy notes, or it will not be run.

## Rewrite the APY-epoch reconcile DELETE to drive from the snapshot id range

`_RECONCILE_TEMPLATE` in `apps/metagraph/services/apy_epoch_ingest.py` runs every 15 minutes at 65 to 80 s, reads 2.1 M buffers and deleted 0 rows in every run inspected on 2026-09-02.
The planner sequential-scans the whole epoch table and probes snapshots per row, applying the id range only afterwards.

**Action:** drive the delete from the `{range_predicate}` (the id range for the beat tick, the block range for the backfill command; both callers share the template) with a materialized CTE over the range joined to `metagraph_neuron`, then the anti-join, and verify with `EXPLAIN (ANALYZE, BUFFERS)` that the outer node is the range scan (primary key for the beat, the FK auto-index `metagraph_neuron_snapshot_block_id_96edc0ac` on `block_id` for the backfill; migration 0014 keeps that index on purpose).

**Why deferred:** correctness-sensitive SQL in the ingest path; needs its own tests against the retention and overlap semantics.

## Derive snapshot-health coverage from `metagraph_dump`

`_compute_missing_snapshot_blocks` in `apps/metagraph/tasks.py` runs `SELECT DISTINCT block_id` over about 245 k snapshot rows per subnet, 892 times a day at 2 to 7 s each, almost all of it I/O.
`metagraph_dump` already records which `(netuid, block_id)` pairs were dumped.

**Action:** compute `covered` from `metagraph_dump` for the block range and confirm on prod for several subnets that the resulting missing-block counts match the current implementation.

**Why deferred:** the two sources disagree for a dump of a subnet with zero neurons (dump row, no snapshots), and the metric's meaning shifts from "snapshots exist" to "a dump was recorded"; equivalence must be checked on prod before switching.

## Bring the external Grafana rank panels into the repo and fix them

The `danger`, `dereg`, `lowest`, `top immune` and hotkey-rank panels exist only on the external Grafana.
They took 26 to 29 s when they completed and were cancelled about 187 times a day on 2026-09-02.

**Action:** copy the panels into a repo dashboard, resolve the time window to a block-number range before joining (the trick from the `mech_id` variable query in `grafana/provisioning/dashboards/metagraph.json`, commit cb55ff1), restrict to epoch blocks via `metagraph_dump`, keep `mech_id = 0` on an index, and re-import into the external Grafana.

**Why deferred:** needs access to the external Grafana to export the current panel JSON.

## Replace per-neuron `update_or_create` with a per-subnet bulk upsert

`MetagraphSyncService` calls `update_or_create` per neuron snapshot: a `SAVEPOINT`, a `SELECT ... FOR UPDATE` that returns nothing 67.6 M times, an `INSERT`, and a `RELEASE`.
The savepoints polluted `pg_stat_statements` and the lookups cost 12 h over 28 days.

**Action:** collect the subnet's snapshots and write them with `bulk_create(update_conflicts=True, unique_fields=..., update_fields=...)` (Django 5.2 returns the primary keys on PostgreSQL, so the mechanism metrics can be upserted in a second batch); then measure index page reads on the bond and weight inserts in `apps/metagraph/services/relation_bulk_syncer.py` (31 h of read wait) and decide between reindexing and ordering rows by index key within a batch.

**Why deferred:** touches the core ingestion path; `track_utility=off` already removes the statistics symptom.

## Set `application_name` per service

Slow-log lines and `pg_stat_activity` cannot tell `sync-metagraph` from `celery-worker` from Grafana; the new `log_line_prefix` prints `app=` but every service leaves it empty.

**Action:** pass `application_name` through the `DATABASE_URL` options (or `DATABASES["default"]["OPTIONS"]`) from a per-service compose environment variable.

**Why deferred:** small but touches every service definition in both compose files; the zero-compose alternative is to derive the name in `settings.py` from `sys.argv` (management command name, `celery`) into `DATABASES["default"]["OPTIONS"]`.

## Drop the redundant ForeignKey indexes

The dashboard's Redundant indexes panel lists 23 indexes (8.5 GB) that are a leading prefix of another valid index with the same access method, operator classes, collations and ordering.
The two that matter are `metagraph_mechanism_metrics_snapshot_id_d4dc12fb` (6.5 GB, prefix of `unique_snapshot_mech`) and `metagraph_neuron_snapshot_neuron_id_75a757dc` (2 GB, prefix of `unique_neuron_block`), both created automatically by Django for a ForeignKey and shadowed by the `UniqueConstraint` that owns the composite index.

**Action:** set `db_index=False` on those fields, drop the indexes `CONCURRENTLY` in a migration modelled on `0007_drop_unused_indexes` (`SeparateDatabaseAndState`, `db_index=False`, `atomic = False`), confirm the auto-generated index names on prod with `\di` first, and verify with `EXPLAIN` that leading-column lookups switch to the composite index.

**Why deferred:** each drop is a prod-only, disk-space-sensitive operation that deserves its own review; the volume was 88 % full when measured.

## Reset Django's database connection in the sync loops after a db restart

`sync_extrinsics` and `sync_metagraph` reopen the chain provider on error but never reset Django's dead database connection, so a db restart puts them into a reconnect loop that leaked about 20 MB per iteration on 2026-07-21 until the container was restarted.
The tuning doc now tells operators to restart them by hand after a manual `up -d db`; `deploy.sh` restarts them anyway.

**Action:** call `django.db.close_old_connections()` (or `connection.close()`) in the `except Exception` paths of both management commands so the next iteration reconnects.

**Why deferred:** touches the two long-running ingestion commands; needs a test that simulates a dropped connection.

## Fix the `readable` glob in the lint session

`noxfile.py` passes the markdown formatter the glob `![.]**/*.md`, which matches no files, so `readable check` has been a no-op in CI while several docs would fail it.

**Action:** decide whether the repo wants readable's style (it also splits lines after colons and pads table cells, so it is broader than one sentence per line); if yes, pass `list_files(".md")` minus `docs/3rd_party/` to the image the way the shellcheck step does, because the pinned image neither honours `!` exclusions nor skips `.nox`/`.venv`, and reformat the existing docs in one commit; if not, drop the step.

**Why deferred:** reformatting every doc is noisy and unrelated to any feature branch.
