# PostgreSQL tuning & query observability

All settings live in the `db` service `command:` block of
[`envs/prod/docker-compose.yml`](../envs/prod/docker-compose.yml) as `-c` flags,
each overridable from `.env`. Command-line flags outrank both `postgresql.conf`
and `postgresql.auto.conf`, so this file is the single source of truth for
anything listed there.

## Why

Measured on prod (2026-08-01):

| Symptom | Measurement |
|---|---|
| Database size vs host RAM | 233 GB vs 7.7 GB (3.3%) |
| `shared_buffers` | 128 MB — stock default, never tuned |
| Cache hit ratio | **72.97%** (healthy OLTP is >99%) |
| Checkpoint write time | 614,199 s of ~777,600 s elapsed — **~79% of wall-clock** |
| `buffers_backend` vs `buffers_checkpoint` | 38.3 M vs 2.95 M — **13x** |
| Forced checkpoints (`checkpoints_req`) | 115 |
| `pg_stat_statements` evictions | **89,281/day** against a 5,000-entry cap |
| Slow query logging | disabled (`log_min_duration_statement = -1`) |

Consequences: the same query takes wildly different times depending on what
happens to be in the page cache (measured 22 ms cold → 4 ms warm on a *trivial*
query), backends stall evicting their own dirty buffers mid-query, and there is
no record of what any client actually ran.

## What changed

| Setting | Was | Now | Why |
|---|---|---|---|
| `shared_buffers` | 128MB | **1GB** | Backends were doing 13x more buffer eviction than the checkpointer |
| `effective_cache_size` | 4GB | **3GB** | Must reflect memory the host *actually* has free, else the planner assumes data is cached when it isn't |
| `work_mem` | 4MB | **32MB** | Dashboard aggregates were spilling to disk as external merge sorts |
| `random_page_cost` | 4 | **1.5** | Data is on SSD, not spinning disk. Not 1.1 — the Hetzner Cloud Volume is network-attached |
| `max_wal_size` | 1GB | **4GB** | 115 forced checkpoints; near-permanent checkpointing |
| `min_wal_size` | 80MB | **1GB** | Reduces WAL segment recycling churn |
| `shm_size` (docker) | 256mb | **1gb** | Parallel query DSM comes from `/dev/shm`; tight once `work_mem` grows |
| `shared_preload_libraries` | `pg_stat_statements` | `pg_stat_statements,auto_explain` | See below |
| `pg_stat_statements.max` | 5000 | **50000** | Stops occasional dashboard queries being evicted before anyone sees them |
| `track_io_timing` | off | **on** | Makes I/O wait measurable rather than inferred |
| `log_min_duration_statement` | -1 (off) | **2000** ms | Records full text + duration of slow queries |
| `log_checkpoints` | off | **on** | Instruments the checkpoint storm directly |
| `log_lock_waits` | off | **on** | Catches contention (e.g. during MV refresh) |
| `log_temp_files` | -1 (off) | **10240** kB | Catches `work_mem` overflow spilling to disk |
| `auto_explain.*` | absent | see below | Logs the *actual plan* for anything over 5 s |

Two values are set via `ALTER SYSTEM` on prod and are deliberately **not**
repeated in the compose file, so they keep working untouched:
`maintenance_work_mem = 256MB`, `autovacuum_vacuum_cost_limit = 2000`.

### `shared_preload_libraries` — do not drop this

Removing `pg_stat_statements` from that list disables the extension at startup,
and every query against the `pg_stat_statements` view starts erroring. The
extension is already installed in the `project` database.

This flag previously existed **only on the deployed server**, not in the repo —
a redeploy from a clean checkout would have silently dropped it. It is now
version-controlled.

Both `.so` files ship with `postgres:14.0-alpine` (verified at
`/usr/local/lib/postgresql/`). A missing library in `shared_preload_libraries`
prevents Postgres from starting at all, so verify before bumping the image.

### `auto_explain` settings

```
auto_explain.log_min_duration = 5000   # ms
auto_explain.log_analyze      = on
auto_explain.log_buffers      = on
auto_explain.log_timing       = off
```

`log_timing=off` is deliberate. Per-node timing is the expensive part of
`log_analyze`, and this host is already CPU/IO saturated. `log_buffers` gives
`shared hit=X read=Y` per node — that is what distinguishes *a bad plan* from
*a cold cache*, which is the open question this whole change exists to answer.

> `log_analyze=on` still instruments **every** query, not just slow ones,
> because Postgres cannot know in advance which will be slow. Overhead with
> `log_timing=off` is mostly row counting, but it is not zero. Consider turning
> `log_analyze` off again once the investigation is done.

## Memory budget

The host has **7.7 GB RAM and no swap**. At the time of measurement ~4.3 GB was
already in use by the app stack, with ~198 MB free and ~3 GB available.

```
  1.0 GB   shared_buffers
+ 4.3 GB   app stack (celery, sync-*, app, redis, nginx, grafana, prometheus)
+ ~2.4 GB  OS page cache (what's left)
= 7.7 GB
```

`shared_buffers=2GB` would leave only ~1.4 GB of page cache and risks the OOM
killer on a swapless host. **1GB is an 8x improvement over the 128 MB default
and is the safe starting point.** Raise it once the host has more RAM:

```sh
# in envs/prod/.env
POSTGRES_SHARED_BUFFERS=2GB
POSTGRES_EFFECTIVE_CACHE_SIZE=4GB   # keep these two consistent
```

`work_mem` is **per sort node per connection**, not global. With
`max_connections=100`, a pathological workload could multiply 32 MB many times
over. Actual concurrent connections are currently <10, so this is safe today —
but do not raise it casually.

## Rollout

Every setting here needs a **full restart**; none are reload-only
(`shared_preload_libraries`, `shared_buffers` and `track_io_timing` all require
it, and command-line flags only apply at process start).

```sh
cd /root/domains/bittensor_sentinel
docker compose -f docker-compose.yml up -d db
docker compose logs -f db      # watch for a clean startup
```

Expect a short outage (seconds to ~a minute). The app, celery and
`sync-*` containers will throw connection errors and reconnect.

> The deployed `docker-compose.yml` on the server has drifted from
> `envs/prod/docker-compose.yml` in this repo. Reconcile before deploying, or
> the `command:` block will not match what is documented here.

## Verification

```sh
docker compose exec db psql -U postgres -d project \
  -c 'SHOW shared_buffers;' \
  -c 'SHOW shared_preload_libraries;' \
  -c 'SHOW random_page_cost;' \
  -c 'SHOW track_io_timing;' \
  -c 'SELECT count(*) FROM pg_stat_statements;'
```

The last one erroring means `pg_stat_statements` failed to preload — check the
startup log and roll back.

## Rollback

Revert the `command:` block and restart. No on-disk format changes are involved,
so rollback is clean and immediate. The one-line safety net if the container
will not start at all:

```sh
docker compose run --rm db postgres -c shared_preload_libraries=pg_stat_statements
```

## What to look at afterwards

Once this has been running through a few dashboard views:

```sql
-- Slowest statements, now with I/O time attributed
SELECT round(mean_exec_time::numeric,1) AS mean_ms,
       round(max_exec_time::numeric,1)  AS max_ms,
       round(stddev_exec_time::numeric,1) AS stddev_ms,
       calls,
       round(blk_read_time::numeric)    AS io_read_ms,
       left(query, 80) AS q
FROM pg_stat_statements
WHERE calls > 5
ORDER BY mean_exec_time DESC LIMIT 20;
```

A high `stddev_ms` relative to `mean_ms`, plus large `io_read_ms`, is the
signature of a cold-cache problem rather than a bad query.

For plans, grep the container log for `auto_explain`:

```sh
docker compose logs db --since 24h | grep -A40 'duration:.*plan:'
```

Then compare `Buffers: shared hit=` against `read=`. Mostly `read=` means the
query is I/O bound (cache/RAM problem); mostly `hit=` with a slow runtime means
the plan itself is bad (query/index problem).

## Known constraints

- **The data volume is 88% full** — 36 GB free of 295 GB, on a database that is
  still growing. `max_wal_size=4GB` is affordable against that, but this needs
  its own remediation, and it currently blocks any `CREATE INDEX CONCURRENTLY`
  on the large tables (`extrinsics` is 64 GB).

  The volume is `/dev/sdb`, mounted at **both** `/mnt/sentinel-volume-hel1-1`
  and `/mnt/HC_Volume_104332859`. The live cluster is under the former.

- **There are two PostgreSQL data directories on this host. Only one is live.**

  | Path | Size | Status |
  |---|---|---|
  | `/mnt/sentinel-volume-hel1-1/db/data` | 235 GB | **live** — set via `POSTGRES_DATA_PATH` in `.env` |
  | `/root/domains/bittensor_sentinel/db/data` | 227 MB | abandoned PG14 cluster from 2026-01-21 |

  The second is the pre-migration data directory, left in place. It is a valid,
  complete cluster with no stale `postmaster.pid`.

  This matters because the compose fallback is
  `${POSTGRES_DATA_PATH:-./db/data}`, which resolves to exactly that abandoned
  cluster. If `POSTGRES_DATA_PATH` is ever missing from `.env`, Postgres starts
  **successfully** against 227 MB of January data instead of the 235 GB
  production database — no error, no failed healthcheck. Always confirm
  `POSTGRES_DATA_PATH` is set before deploying, and read config from inside the
  container (`docker exec ... cat /var/lib/postgresql/data/...`) rather than
  guessing the host path.
- Logs go to `journald` on `/` (13 GB free). `log_min_duration_statement=2000`
  plus `auto_explain` will increase log volume — watch it for the first day and
  raise the thresholds via `.env` if needed.
- `postgres:14.0-alpine` is pinned to a `.0` patch release from 2021 with many
  known fixes since. Worth a separate upgrade to the latest 14.x.
