
# Base cookiecutter-rt-django README

- docker with [compose plugin](https://docs.docker.com/compose/install/linux/)
- python 3.14
- [uv](https://docs.astral.sh/uv/)
- [nox](https://nox.thea.codes)

# Setup development environment

```sh
./setup-dev.sh
docker compose up -d
cd app/src
uv run manage.py wait_for_database --timeout 10
uv run manage.py migrate
uv run manage.py runserver
```

# Setup production environment (git deployment)

<details>

This sets up "deployment by pushing to git storage on remote", so that:

- `git push origin ...` just pushes code to Github / other storage without any consequences;
- `git push production master` pushes code to a remote server running the app and triggers a git hook to redeploy the application.

```
Local .git ------------> Origin .git
                \
                 ------> Production .git (redeploy on push)
```

- - -

Use `ssh-keygen` to generate a key pair for the server, then add read-only access to repository in "deployment keys" section (`ssh -A` is easy to use, but not safe).

```sh
# remote server
mkdir -p ~/repos
cd ~/repos
git init --bare --initial-branch=master sentinel_tower.git

mkdir -p ~/domains/sentinel_tower
```

```sh
# locally
git remote add production root@<server>:~/repos/sentinel_tower.git
git push production master
```

```sh
# remote server
cd ~/repos/sentinel_tower.git

cat <<'EOT' > hooks/post-receive
#!/bin/bash
unset GIT_INDEX_FILE
export ROOT=/root
export REPO=sentinel_tower
while read oldrev newrev ref
do
    if [[ $ref =~ .*/master$ ]]; then
        export GIT_DIR="$ROOT/repos/$REPO.git/"
        export GIT_WORK_TREE="$ROOT/domains/$REPO/"
        git checkout -f master
        cd $GIT_WORK_TREE
        ./deploy.sh
    else
        echo "Doing nothing: only the master branch may be deployed on this server."
    fi
done
EOT

chmod +x hooks/post-receive
./hooks/post-receive
cd ~/domains/sentinel_tower
sudo bin/prepare-os.sh
./setup-prod.sh

# adjust the `.env` file

mkdir letsencrypt
./letsencrypt_setup.sh
./deploy.sh
```

### Deploy another branch

Only `master` branch is used to redeploy an application.
If one wants to deploy other branch, force may be used to push desired branch to remote's `master`:

```sh
git push --force production local-branch-to-deploy:master
```

</details>


# Background tasks with Celery

## Dead letter queue

<details>
There is a special queue named `dead_letter` that is used to store tasks
that failed for some reason.

A task should be annotated with `on_failure=send_to_dead_letter_queue`.
Once the reason of tasks failure is fixed, the task can be re-processed
by moving tasks from dead letter queue to the main one ("celery"):

    manage.py move_tasks "dead_letter" "celery"

If tasks fails again, it will be put back to dead letter queue.

To flush add tasks in specific queue, use

    manage.py flush_tasks "dead_letter"
</details>

# Monitoring

Running the app requires proper certificates to be put into `nginx/monitoring_certs`,
see [nginx/monitoring_certs/README.md](nginx/monitoring_certs/README.md) for more details.

## Database roles

`pg_stat_statements` / `pg_stat_activity` attribute queries to the **database role**
that ran them, and the *PostgreSQL* Grafana dashboard groups by that role — so each
consumer gets its own:

| role | used by | password |
|---|---|---|
| `postgres` (superuser) | app, celery, sync daemons | `POSTGRES_PASSWORD` |
| `grafana_reader` (`GRAFANA_READER_USER`) | Grafana's PostgreSQL datasource — read-only, `statement_timeout`, `application_name=grafana` | `GRAFANA_READER_PASSWORD` |
| `postgres_exporter` (`POSTGRES_EXPORTER_USER`) | postgres-exporter container — `pg_monitor` | `POSTGRES_EXPORTER_PASSWORD` |

Both dedicated roles are **opt-in**: left blank in `.env`, the exporter and Grafana
fall back to `POSTGRES_USER` and everything works, with all load shown as one user.
To opt in on an existing database, create the roles by hand once, fill the variables
in `.env` (each pair: both or neither), and recreate the two containers — the app
itself always uses `POSTGRES_USER`:

```sh
docker compose exec db psql -U postgres -d project -v ON_ERROR_STOP=1 <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE ROLE <POSTGRES_EXPORTER_USER> WITH LOGIN PASSWORD '<POSTGRES_EXPORTER_PASSWORD>';
GRANT pg_monitor TO <POSTGRES_EXPORTER_USER>;

CREATE ROLE grafana_reader WITH LOGIN PASSWORD '<GRAFANA_READER_PASSWORD>';
GRANT CONNECT ON DATABASE project TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;            -- includes views and materialized views
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_reader;  -- tables added by future migrations
GRANT pg_read_all_stats TO grafana_reader;                                -- query text of other roles in pg_stat_statements (PostgreSQL dashboard)
ALTER ROLE grafana_reader SET default_transaction_read_only = on;
ALTER ROLE grafana_reader SET statement_timeout = '120s';                 -- matches GF_DATAPROXY_TIMEOUT
ALTER ROLE grafana_reader SET application_name = 'grafana';
EOSQL
docker compose up -d --force-recreate grafana postgres-exporter
```

`ALTER DEFAULT PRIVILEGES` covers tables created by `postgres` (which runs
migrations). External Grafana instances reaching the database over mTLS (see below)
should get a role like `grafana_reader`, not the superuser.

# Remote PostgreSQL access (mTLS)

Prod nginx exposes port `5432` with mutual TLS; postgres has no host port binding.
See [docs/postgres-mtls.md](postgres-mtls.md) for setup, adding a new client,
testing, and troubleshooting. Cert issuance commands live in
[db_access_certs/README.md](../db_access_certs/README.md).

## Monitoring execution time of code blocks

Somewhere, probably in `metrics.py`:

```python
some_calculation_time = prometheus_client.Histogram(
    'some_calculation_time',
    'How Long it took to calculate something',
    namespace='django',
    unit='seconds',
    labelnames=['task_type_for_example'],
    buckets=[0.5, 1, *range(2, 30, 2), *range(30, 75, 5), *range(75, 135, 15)]
)
```

Somewhere else:

```python
with some_calculation_time.labels('blabla').time():
    do_some_work()
```


# Backups

<details>
<summary>Click to for backup setup & recovery information</summary>

Backups are managed by `backups` container.

## Local volume

By default, backups will be created [periodically](backups/backup.cron) and stored in `backups` volume.

### Backups rotation
Set env var:
- `BACKUP_LOCAL_ROTATE_KEEP_LAST`

### Email

Local backups may be sent to email manually. Set env vars:
- `EMAIL_HOST`
- `EMAIL_PORT`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_FROM_EMAIL`

Then run:
```sh
docker compose run --rm -e EMAIL_TARGET=youremail@domain.com backups ./backup-db.sh
```

## B2 cloud storage

> In these examples we assume that backups will be stored inside `folder`. If you want to store backups in the root folder, just use empty string instead of `folder`.

First, create a Backblaze B2 account and a bucket for backups (with [lifecycle rules](https://www.backblaze.com/docs/cloud-storage-configure-and-manage-lifecycle-rules)):

```sh
b2 bucket create --lifecycle-rule '{"daysFromHidingToDeleting": 30, "daysFromUploadingToHiding": 30, "fileNamePrefix": "folder/"}' "sentinel_tower-backups" allPrivate
```

> If you want to add backups to already existing bucket, use `b2 bucket update` command and don't forget to list all previous lifecycle rules as well as adding the new one.

Create an application key with restricted access to a single bucket:

```sh
b2 key create --bucket "sentinel_tower-backups" --namePrefix "folder/" "sentinel_tower-backups-key" listBuckets,listFiles,readFiles,writeFiles
```

Fill in `.env` file:
- `BACKUP_B2_BUCKET=sentinel_tower-backups`
- `BACKUP_B2_FOLDER=folder`
- `BACKUP_B2_APPLICATION_KEY_ID=0012345abcdefgh0000000000`
- `BACKUP_B2_APPLICATION_KEY=A001bcdefgHIJKLMNOPQRSTUxx11x22`

## List all available backups

```sh
docker compose run --rm backups ./list-backups.sh
```

## Restoring system from backup after a catastrophical failure

1. Follow the instructions above to set up a new production environment
2. Restore the database using one of
```sh
docker compose run --rm backups ./restore-db.sh /var/backups/{backup-name}.dump.zstd

docker compose run --rm backups ./restore-db.sh b2://{bucket-name}/{backup-name}.dump.zstd
docker compose run --rm backups ./restore-db.sh b2id://{ID}
```
3. See if everything works
4. Make sure everything is filled up in `.env`, error reporting integration, email accounts etc

## Monitoring

`backups` container runs a simple server which [exposes essential metrics about backups](backups/bin/serve_metrics.py).

</details>

# cookiecutter-rt-django

Skeleton of this project was generated using [cookiecutter-rt-django](https://github.com/reef-technologies/cookiecutter-rt-django).
Use `cruft update` to update the project to the latest version of the template with all current bugfixes and [features](https://github.com/reef-technologies/cookiecutter-rt-django/blob/master/features.md).
