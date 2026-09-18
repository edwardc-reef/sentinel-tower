# Postgres Slow Statements dashboard

Slow, cancelled and failed Postgres statements with their SQL, for any
selected time range. Provisioned from
[`grafana/provisioning/dashboards/postgres-slow-statements.json`](../grafana/provisioning/dashboards/postgres-slow-statements.json),
reachable at `/grafana/d/postgres-slow-statements/`.

It fills the two gaps the other performance dashboards leave:
`pg_stat_statements` (DB Query Performance) is cumulative since its last reset
and never sees a statement that was cancelled, and Grafana's own metrics
(Grafana Query Timing) carry no query text at all.

## Where the data comes from

Postgres writes to its log every statement slower than
`log_min_duration_statement` (2 s on prod) with its duration and full SQL,
and every failed or cancelled statement as an `ERROR` line followed by a
`STATEMENT` line with the SQL. A statement spans several lines: the first
carries the `log_line_prefix`, the rest are tab-indented.

[`alloy/config.alloy`](../alloy/config.alloy) ships every container's log to
the central Loki (`https://loki.reef.pl`). For the db container it joins the
continuation lines into the entry that started them (`stage.multiline`), so
each slow statement and each `STATEMENT` line arrives as one entry with its
whole SQL.

Grafana reads them back through the provisioned `Loki` data source. The
central Loki has two separate credential systems, so `.env` needs both:

| Variables | Opens | Where they come from |
|---|---|---|
| `LOKI_USER`, `LOKI_PASSWORD` | `/loki/api/v1/push` only (Alloy) | `add_loki_target.sh` on the monitoring server, see below |
| `LOKI_READER_USER`, `LOKI_READER_PASSWORD` | the query API (Grafana data source) | a reader token issued at <https://loki.reef.pl/token/> |

## Credentials

Nothing is shipped until `LOKI_URL`, `LOKI_USER` and `LOKI_PASSWORD` are set.
Push credentials are created on the monitoring server, one pair per server
group and environment (this project's group is `backend_developers_sentinel`),
as described in the README under "Log aggregation":

```sh
uvx cadm exec prometheus_and_grafana -- "cd /home/ubuntu/apps/prometheus-grafana-monitoring/scripts && ./add_loki_target.sh backend_developers_sentinel prod"
```

The script prints the username and password to put into `LOKI_USER` and
`LOKI_PASSWORD`.

The dashboard cannot query until `LOKI_READER_USER` and `LOKI_READER_PASSWORD`
are set. Open <https://loki.reef.pl/token/> in a browser, sign in, and copy the
token it shows; that is the password. The username is your e-mail with the `@`
URL-encoded, exactly as the token service stores it, for example
`jane.doe%40reef.pl`. The plain e-mail is rejected with 401. Every visit to the
token page issues a new token and invalidates the previous one for that
address, so do not reopen it to "check": the data source starts failing with
401 until `.env` carries the new value. Reader tokens are personal and shared
by all readers of the `rt` tenant, so the data source sees every project's
logs; the dashboard filters on the db container name.

After changing `.env`, run `docker compose up -d alloy grafana`: both read
their configuration at start, and compose recreates them when their
environment changed. A recreated grafana container gets a new address, and
nginx resolves its upstream only at start, so follow up with
`docker exec <nginx container> nginx -s reload` or `/grafana/` answers 502.

## Reading it

- The **DB user** dropdown filters every panel by the Postgres role that ran
  the statement (`postgres` is the application, the `*_ro` roles are the
  Grafana readers). Alloy takes it from the `log_line_prefix`, which prod
  writes since 2026-09-05 13:27 UTC; older lines have no user and only show
  under "All". `app=` stays `[unknown]` until the services set
  `application_name`.
- The tiles count completed slow statements and cancelled statements in the
  selected range and show the longest and the total time spent.
- The two charts bucket slow statements by duration band and show
  cancellations over time.
- "Slow statements, longest first" is the list: time, duration and SQL, up to
  1000 entries per query. Click a cell to expand the SQL.
- "Errored and cancelled statements" shows `ERROR` lines and the `STATEMENT`
  lines Postgres writes right after them, newest first. A cancellation by a
  client that gave up reads "canceling statement due to user request".
- Statements faster than the log threshold never appear here; use DB Query
  Performance for those.
