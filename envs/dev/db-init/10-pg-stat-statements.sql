-- Runs once, on a fresh volume, against POSTGRES_DB (postgres image entrypoint).
-- Existing dev volumes: see README "Local Development Setup".
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
