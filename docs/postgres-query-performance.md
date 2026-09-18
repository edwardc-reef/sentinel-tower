# PostgreSQL query performance

Where query-performance data lives, how to read the **DB Query Performance** Grafana dashboard, the log recipes for what the dashboard cannot show, and the findings of the first analysis on 2026-09-02.
Companion to [postgres-tuning.md](postgres-tuning.md), which documents the server settings themselves.

## Where the data lives

| Source | Shows | Cannot show |
|---|---|---|
| `pg_stat_statements` (dashboard row *Statement statistics*) | Cumulative cost per statement shape since the last reset: calls, time, I/O wait, temp spill | Statements that never completed (cancelled by a Grafana timeout), entries evicted from the 50,000-slot table, *when* something was slow |
| `pg_stat_activity` (row *Right now*) | What is running or blocked at this instant, with role and application name | History |
| `pg_stat_database`, `pg_stat_bgwriter`, catalog (rows *Database & statistics health* and *Indexes*) | Cache hit ratio, read-wait share, checkpoint pressure, index coverage and redundancy | Per-statement attribution |
| Postgres log (journald on the host, shipped to Loki by Alloy) | Every statement over 2 s with its text, every plan over 5 s with buffer counts, cancels, lock waits, temp files | Aggregates |

The dashboard is `grafana/provisioning/dashboards/db-query-performance.json`.
It is provisioned on the on-box Grafana and can be imported into the external one; there the datasource role needs `pg_read_all_stats` to see other roles' statement text.
After editing a dashboard that uses neither template variables nor `${__from}`/`${__to}` (this one and DB Size & Retention), run `python3 scripts/check_dashboard_queries.py grafana/provisioning/dashboards/<file>.json` against a local Grafana (`docker compose up -d db` then `docker compose up -d --no-deps grafana`) to execute every panel query through the provisioned datasource.

## Reading the dashboard

**Right now.**
Backend counts, statements running longer than 2 s, and sessions blocked on a lock.
Both tables are normally empty.
Idle-in-transaction sessions are listed on purpose: they hold locks and block vacuum, and `age_s` for them is time since their last statement.

**Statement statistics.**
Five top-20 tables from `pg_stat_statements`, grouped so that parameter-count variants of one Django query (`bulk_create` batches, `IN` lists) land on one row.
Transaction-control and session-setting statements and this dashboard's own catalog queries are excluded (DB Size & Retention's catalog queries can still appear); on prod, `track_utility=off` also hides DDL, `VACUUM` and `REFRESH MATERIALIZED VIEW`, which still reach the slow log.
`s_per_day` and `calls_per_day` divide by days since the `pg_stat_statements` reset, so numbers stay comparable across resets.
Two caveats apply to every panel here: entries can be evicted when the table is full, and a statement that was cancelled before it finished is never recorded.
The second caveat is why the worst prod offenders on 2026-09-02 did not appear here at all; use the log recipes below for those.

**Database & statistics health.**
Cache hit ratio and read-wait share say whether slowness is memory-bound; the `io_timing` tile shows whether `track_io_timing` is on at all.
Checkpoints and buffers repeat the two counters that drove the August 2026 tuning.
`pg_stat_statements health` says whether the statistics row can be trusted: `fill_pct` near 100 plus growing evictions means rare statements are being dropped, and `savepoint_pct` above 0 means `track_utility` is still on.

**Indexes.**
Foreign keys without a leading-column index are definitive gaps for parent deletes and reverse lookups; only valid, non-partial indexes count as cover, and `in_other_index` and `parent_deletes` say whether a gap matters.
Redundant indexes are definitive too: the listed index is a leading prefix of one of `covered_by` with the same access method, operator classes, collations and ordering, or an exact duplicate.
Sequential scans on tables over 100 MB are candidates, not proof, and cancelled queries still count there.
Indexes by blocks read from disk show cost, not coverage: which indexes are cold.
Proof that one specific statement needs an index is its plan; see the recipes.

## Log recipes

All on the prod host.
The database container logs to journald; `--since` accepts phrases like `"24 hours ago"`.

```sh
pglog() { journalctl -o cat CONTAINER_NAME=bittensor_sentinel-db-1 --since "${1:-24 hours ago}"; }

pglog | grep 'duration: ' | grep -vc 'plan:'    # statements over log_min_duration_statement (2 s); plans are logged with their own duration line
pglog | grep -c 'canceling statement due to user request'   # client-side cancels, mostly Grafana data-proxy timeouts
pglog | grep -A2 'canceling statement' | grep -A1 'STATEMENT:' | cut -c1-160   # what was cancelled
pglog | grep -c 'temporary file'                # work_mem spills over log_temp_files
pglog | grep -c 'still waiting for'             # lock waits over deadlock_timeout (1 s)
pglog | grep -A60 'plan:' | grep -A57 'DELETE FROM metagraph_validator_apy_epoch' | head -60   # one plan; the query text starts on the line after "Query Text:"
pglog "7 days ago" | grep -c 'checkpoint complete'   # the function takes a journalctl --since phrase
```

Multi-line statements continue on tab-indented journal lines, so use `-A` context rather than single-line greps when you need the text.
With the `log_line_prefix` from the tuning doc each session line also carries `user@db app=<application_name>`; the app services do not set an application name yet (follow-up in [todo.md](todo.md)).

In a plan, compare `Buffers: shared hit=` against `read=`.
Mostly `read=` means the query is I/O bound (cache or RAM); mostly `hit=` with a slow runtime means the plan itself is bad (query or index).

To exercise the *Right now* panels on a dev database, open a transaction that holds a lock in one psql session and run a statement that needs it in another; the blocked one appears in *Blocked sessions* with the holder's pid.

## Findings, 2026-09-02

Measured over 28 days of `pg_stat_statements` (reset 2026-08-05) and the last 24 h of the log.

| Finding | Evidence |
|---|---|
| `pg_stat_statements` was 99.9 % savepoint noise | 47,953 of 48,014 entries were uniquely named `SAVEPOINT`/`RELEASE` from Django's per-row `update_or_create`; 221,520 evictions since the reset despite the 50,000 cap; about 60 real statement shapes survived |
| The APY-epoch reconcile DELETE burns about an hour a day and deletes nothing | 54 runs logged in 24 h at 65 to 80 s each, 2.1 M buffer hits per run, `actual rows=0`; the planner sequential-scans the whole 443 k-row epoch table and probes snapshots per row, applying the id range afterwards |
| External Grafana rank panels are cancelled at the 60 s data-proxy timeout | 187 of 196 cancels/day were the `danger`, `dereg`, `lowest`, `top immune` and hotkey-rank panels on subnet 33 with a 7-day window; the ones that complete take 26 to 29 s; these panels are not in the repo |
| Snapshot-health `DISTINCT block_id` query | 892 runs/day at 2 to 7 s, almost all I/O; `metagraph_dump` already holds the covered block set per subnet |
| Per-neuron `update_or_create` dominates the write path | 67.6 M lookups returning 0 rows cost 12 h; bond and weight inserts spent 31 h waiting on index page reads; foreign-key checks ran about 950 M times |
| 1.3 TB of temp spill since the reset, zero temp-file log lines in 24 h | Historical, from the retired materialized-view refresh |
| Cache hit 94.6 %, read wait 44 % of active time | Up from 73 % before the August tuning; reads are still the bottleneck |

Index coverage (foreign keys and sequential scans measured on 2026-09-02; redundant indexes re-derived on 2026-09-03 with the final query):

| Check | Result |
|---|---|
| Foreign keys without a leading-column index | 2 of 35: `metagraph_bond.target_neuron_id` and `metagraph_weight.target_neuron_id`, both deliberate drops from migration 0007, parent table never deleted from |
| Redundant indexes | 23, totalling 8.5 GB; `metagraph_mechanism_metrics_snapshot_id_d4dc12fb` (6.5 GB, prefix of `unique_snapshot_mech`) and `metagraph_neuron_snapshot_neuron_id_75a757dc` (2 GB, prefix of `unique_neuron_block`) |
| Sequential scans of tables over 100 MB since the reset | `metagraph_mechanism_metrics` 190 scans × 198 M rows, `metagraph_weight` 195 × 40 M, `metagraph_neuron_snapshot` 301 × 18 M |
| Cold indexes | `unique_neuron_block` 1.6 TB read from disk, `unique_bond` 1.2 TB at 86 % hit, `idx_ns_validator_block` 44 % hit; heap hit 63 % on `metagraph_mechanism_metrics`, 51 % on `extrinsics` |

## Recommended fixes, ranked

1. **`pg_stat_statements.track_utility=off`, then reset.**
   Shipped in the tuning doc's compose block; without it nothing else on the statistics row is trustworthy.
2. **Rewrite the reconcile DELETE in `apps/metagraph/services/apy_epoch_ingest.py` to drive from the snapshot id range** instead of the whole epoch table.
   Verify with `EXPLAIN (ANALYZE, BUFFERS)` that the outer node is the primary-key range scan and buffers drop from 2.1 M.
3. **Derive snapshot-health coverage from `metagraph_dump`** (`netuid` plus block range) instead of `DISTINCT block_id` over about 245 k snapshot rows per subnet in `apps/metagraph/tasks.py`.
   Confirm equivalence on prod for several subnets before switching.
4. **Fix the external Grafana rank panels and bring them into the repo.**
   Resolve the time window to a block-number range before joining, restrict to epoch blocks via `metagraph_dump`, and keep `mech_id = 0` on an index.
5. **Replace per-neuron `update_or_create` in `MetagraphSyncService` with a per-subnet `bulk_create(update_conflicts=True)`.**
   Then look at index page reads on bond and weight inserts: bloat versus random insert order.
6. **Set `application_name` per service** (compose environment into the `DATABASE_URL` options) so `pg_stat_activity` and the log prefix attribute statements to `sync-metagraph`, `celery-worker` and so on.
7. **Drop the redundant ForeignKey indexes, largest first.**
   Set `db_index=False` on the field and drop `CONCURRENTLY` in a migration, following `0007_drop_unused_indexes`; verify with `EXPLAIN` that leading-column lookups switch to the composite index.
   Recovers 8.5 GB on a volume that was 88 % full.

Each item is tracked in [todo.md](todo.md).
A history view (sampling `pg_stat_activity` into a table) is the follow-up that would remove the cancelled-statement blind spot; it is tracked there as well.
