# Grafana Query Timing dashboard

Where a Grafana instance spends time answering dashboard queries, split into
the whole request and the part spent inside the data source plugin, per
plugin. It is the Grafana-side complement of the DB Query Performance
dashboard: Postgres only records statements that finish, while Grafana records
every request, including the ones that were cancelled because the client gave
up.

Provisioned from
[`grafana/provisioning/dashboards/grafana-query-timing.json`](../grafana/provisioning/dashboards/grafana-query-timing.json),
reachable at `/grafana/d/grafana-query-timing/`.

## Two Grafana instances

The **Grafana instance** dropdown at the top selects whose metrics the panels
show:

| Value | Instance | Why it matters |
|---|---|---|
| `grafana-external` (default) | grafana.bactensor.io, the metagraph-grafana stack | Its dashboards query this database over the mTLS proxy on port 5432. This is where the slow, cancelled panel queries come from. |
| `grafana` | this stack's own Grafana under `/grafana/` | Serves the provisioned dashboards in this repo. |

Both are scrape jobs in [`prometheus/prometheus.yml`](../prometheus/prometheus.yml).
The external one is scraped over HTTPS on the ALB name
`internal.grafana.bactensor.io`; the CloudFront name only answers with a
redirect. That endpoint answers without authentication.

## Data source

Grafana exposes Prometheus metrics on `/metrics` by default. Each job scrapes
its instance every 30 s. The dashboard reads two histograms:

| Metric | What it measures |
|---|---|
| `grafana_http_request_duration_seconds{handler="/api/ds/query"}` | the whole query request: auth, macro expansion, the plugin call, response encoding |
| `grafana_plugin_request_duration_seconds{endpoint="queryData"}` | the time inside one data source plugin call; for Postgres that is SQL execution plus result conversion |

The gap between the two is Grafana's own overhead. Labels identify the plugin
type, not a dashboard, panel or statement. The Postgres plugin reports as
`postgres` on Grafana 10 and `grafana-postgresql-datasource` on Grafana 11;
the panels accept both.

## Deploying a prometheus.yml change

The Prometheus container mounts `./prometheus` read-only and reads the file
only at start, so `deploy.sh` alone does not pick up a new job. After the
files are on the host:

```sh
curl -X POST http://127.0.0.1:9090/-/reload
```

The top banner of the dashboard turns from red "NOT SCRAPED" to green once the
first scrape for the selected instance lands. Grafana itself picks the
dashboard file up within 10 s, no restart needed.

## Reading it

- The percentile panels come from a histogram whose top bucket is 10 s.
  Anything slower is reported as 10 s. The mean panel and the "Slow query
  share" panel are not capped; use them for queries that run for tens of
  seconds.
- A failing SQL statement and a statement cancelled because the client went
  away are both recorded as HTTP 400 in the request panels. On Grafana 10.2
  (this stack's own instance) the plugin status stays `ok` for those; on
  Grafana 11 (the external instance) they also show up in the plugin-level
  failures panel as `error` and `cancelled`. Verified on Grafana 10.2.0:
  `GF_DATAPROXY_TIMEOUT` does not cancel SQL queries, and a client disconnect
  cancels the statement in Postgres and is counted as a 400.
- The `/grafana/` location in nginx has no explicit `proxy_read_timeout`, so
  the default 60 s applies. A panel query that runs longer is cut off there,
  which is what the "canceling statement due to user request" lines in the
  Postgres log record.
- No history exists before the scrape job went live.
