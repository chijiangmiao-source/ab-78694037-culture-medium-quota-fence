#!/bin/sh
# Wait until PostgreSQL accepts TCP connections, then exec the given command.
set -e

host="${DB_HOST:-db}"
port="${DB_PORT:-5432}"
retries="${DB_WAIT_RETRIES:-60}"

echo "waiting for database ${host}:${port} ..."
i=0
until python -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('${host}', ${port}))
except OSError:
    sys.exit(1)
" 2>/dev/null
do
    i=$((i + 1))
    if [ "$i" -ge "$retries" ]; then
        echo "database ${host}:${port} not reachable after ${retries} attempts" >&2
        exit 1
    fi
    sleep 1
done
echo "database is up"
exec "$@"
