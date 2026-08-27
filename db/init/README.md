# db/init

Scripts in this directory are mounted into the `db` container at
`/docker-entrypoint-initdb.d/` and run **once**, when the postgres data directory is
first initialised. They do not run again on restarts or on databases that already
exist (e.g. after `cruft update` on a deployed project).

## Applying to an existing database

Run the statements from `01-monitoring.sh` by hand, using the password from
`POSTGRES_EXPORTER_PASSWORD` in `.env`:

```sh
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE ROLE postgres_exporter WITH LOGIN PASSWORD '<POSTGRES_EXPORTER_PASSWORD>';
GRANT pg_monitor TO postgres_exporter;
EOSQL
```

`CREATE EXTENSION` only succeeds if `pg_stat_statements` is in
`shared_preload_libraries`, which the compose file sets on the `db` command — the
container must have been restarted with that config first
(`docker compose up -d --force-recreate db`).
