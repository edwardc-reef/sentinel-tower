#!/bin/sh
# Copyright 2024, Reef Technologies (reef.pl), All rights reserved.
set -eux

if [ ! -f ".env" ]; then
    echo "\e[31mPlease setup the environment first!\e[0m";
    exit 1;
fi

docker compose build

docker compose up -d db  # in case it hasn't been launched before
# backup db before any database changes
# docker compose run --rm backups ./backup-db.sh

# collect static files to external storage while old app is still running
# docker compose run --rm app sh -c "python manage.py collectstatic --no-input"

# Stop running services built from an application image. Profiled services are
# restarted explicitly below because a plain `compose up` does not enable them.
APP_SERVICES='^(app|block-scheduler|sync-extrinsics|sync-metagraph|backfill-metagraph|historical-metagraph-backfill|backfill-validator-apy-epochs|backfill-extrinsics|prune-retention|celery-worker|celery-beat|celery-flower)$'
SERVICES=$(docker compose ps --services 2>/dev/null | grep -E "$APP_SERVICES" || true)
if [ -n "$SERVICES" ]; then
    # Build profiled service images before stopping their existing containers.
    # shellcheck disable=2086
    docker compose build $SERVICES
    # shellcheck disable=2086
    docker compose stop $SERVICES
fi
# All shared metrics writers are stopped, so clearing every known writer directory is safe.
# This remains necessary while runtime pickle compaction is deliberately disabled.
docker compose run --rm --no-deps app sh -c '
    set -e
    for metrics_dir in \
        /prometheus-multiproc-dir \
        /prometheus-multiproc-dir/celery-worker \
        /prometheus-multiproc-dir/block-scheduler
    do
        PROMETHEUS_MULTIPROC_DIR="$metrics_dir" ./prometheus-cleanup.sh
    done
'

# start everything; migrations are NOT run automatically (long CONCURRENTLY
# index builds would keep the whole stack down) — run them manually while the
# app serves traffic:
docker compose up -d

if [ -n "$SERVICES" ]; then
    # shellcheck disable=2086
    docker compose up -d $SERVICES
fi

# nginx resolves upstream container names once at config load; containers
# recreated above (grafana, postgres-exporter, …) get new IPs, so re-resolve
docker compose exec nginx nginx -s reload || true

echo "Deploy done. If this release contains migrations, apply them now with:"
echo "  docker compose run --rm app sh -c 'unset PROMETHEUS_MULTIPROC_DIR PROMETHEUS_USE_FLOCK; python manage.py wait_for_database --timeout 10 && python manage.py migrate'"

# Clean up older dangling images without killing recent build cache
docker image prune -f --filter "until=168h" || true
