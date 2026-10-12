#!/bin/sh
# Crea un rol propio por dominio y sus bases lógicas. Se ejecuta una sola vez,
# cuando el volumen de datos está vacío. Ningún rol accede a bases de otro dominio.
set -eu

create_domain() {
  role="$1"
  password="$2"
  shift 2
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v role="$role" -v password="$password" <<'SQL'
CREATE ROLE :"role" LOGIN PASSWORD :'password';
SQL
  for database in "$@"; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
      -v role="$role" -v database="$database" <<'SQL'
CREATE DATABASE :"database" OWNER :"role";
REVOKE ALL ON DATABASE :"database" FROM PUBLIC;
SQL
  done
}

create_domain usuarios "$USUARIOS_DB_PASSWORD" usuarios_write usuarios_read
create_domain afiliacion "$AFILIACION_DB_PASSWORD" afiliacion
create_domain servicios "$SERVICIOS_DB_PASSWORD" servicios
create_domain entrenamientos "$ENTRENAMIENTOS_DB_PASSWORD" entrenamientos_write entrenamientos_read
create_domain eventos "$EVENTOS_DB_PASSWORD" eventos_write eventos_read
