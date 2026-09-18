"""Run every panel query of a provisioned Grafana dashboard through Grafana's query API.

Usage:
    python3 scripts/check_dashboard_queries.py grafana/provisioning/dashboards/<file>.json

Environment:
    GRAFANA_URL       default http://localhost:3001
    GRAFANA_USER      default admin
    GRAFANA_PASSWORD  default admin

Exits 1 when the file is invalid (duplicate panel ids, wrong datasource, empty SQL),
when any query returns an error from the datasource, or when Grafana itself cannot be
reached or refuses the request. The first such failure aborts the run, so a wrong
password never trips Grafana's login lockout.

Limitation: Grafana's frontend interpolates dashboard variables (`$netuid`, `${subnet}`)
and the global `${__from}` / `${__to}`; the query API does not, so panels that use them
fail here with a syntax error near `$`. Datasource macros such as `$__timeFilter` work.
Only dashboards without template variables are fully checkable.

Postgres only: every target must use the provisioned `postgresql` datasource, so a
Prometheus dashboard reports every target as a datasource error.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

EXPECTED_DS_UID = "postgresql"


def iter_panels(panels):
    for panel in panels:
        yield panel
        yield from iter_panels(panel.get("panels", []))


def validate_structure(dashboard):
    errors = []
    seen = set()
    for panel in iter_panels(dashboard["panels"]):
        pid = panel.get("id")
        if pid in seen:
            errors.append(f"duplicate panel id {pid}")
        seen.add(pid)
        for target in panel.get("targets", []):
            uid = (target.get("datasource") or {}).get("uid")
            if uid != EXPECTED_DS_UID:
                errors.append(f"panel {pid} ({panel.get('title')}): datasource uid {uid!r}")
            if not target.get("rawSql"):
                errors.append(f"panel {pid} ({panel.get('title')}): empty rawSql")
    return errors


def run_query(base_url, auth_header, sql):
    body = json.dumps(
        {
            "from": "now-1h",
            "to": "now",
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "postgres", "uid": EXPECTED_DS_UID},
                    "rawSql": sql,
                    "format": "table",
                }
            ],
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - local Grafana over http by design
        f"{base_url}/api/ds/query",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": auth_header},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local Grafana over http by design
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise SystemExit(f"ERROR HTTP {exc.code} from {exc.url}: {text[:200]}") from exc
        if "results" not in payload:
            raise SystemExit(f"ERROR HTTP {exc.code}: {payload.get('message', text[:200])}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"ERROR cannot reach {base_url}: {exc.reason}") from exc
    return payload.get("results", {}).get("A", {}).get("error")


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1]) as fh:
        dashboard = json.load(fh)
    if "panels" not in dashboard:
        raise SystemExit("ERROR not a dashboard file: no top-level 'panels' key (API exports wrap it in 'dashboard')")
    errors = validate_structure(dashboard)
    base_url = os.environ.get("GRAFANA_URL", "http://localhost:3001").rstrip("/")
    credentials = f"{os.environ.get('GRAFANA_USER', 'admin')}:{os.environ.get('GRAFANA_PASSWORD', 'admin')}"
    auth_header = "Basic " + base64.b64encode(credentials.encode()).decode()
    checked = 0
    for panel in iter_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            if not target.get("rawSql"):
                continue
            checked += 1
            error = run_query(base_url, auth_header, target["rawSql"])
            if error:
                errors.append(f"panel {panel.get('id')} ({panel.get('title')}): {error}")
    for error in errors:
        print(f"ERROR {error}")
    print(f"{checked} targets checked, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
