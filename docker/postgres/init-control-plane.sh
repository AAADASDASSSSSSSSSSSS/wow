#!/usr/bin/env bash
set -Eeuo pipefail

app_password="${RATSNEST_DB_PASSWORD:-${POSTGRES_PASSWORD:-postgres}}"

psql --set ON_ERROR_STOP=1 \
    --username "${POSTGRES_USER:-postgres}" \
    --dbname "${POSTGRES_DB:-agent_service}" \
    --set app_password="$app_password" <<'SQL'
SELECT 'CREATE ROLE ratsnest_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ratsnest_app')\gexec
SELECT format('ALTER ROLE ratsnest_app PASSWORD %L', :'app_password')\gexec
CREATE SCHEMA IF NOT EXISTS control_plane;
SQL
