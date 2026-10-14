#!/usr/bin/env bash
# Build and (re)start Flight Alerter, then wait until the dashboard answers.
#
#   ./deploy.sh           rebuild the image and restart the container
#   ./deploy.sh --pull    git pull first
#   ./deploy.sh --logs    follow the logs afterwards
#   ./deploy.sh --down    stop and delete the container (settings and alert history are kept)
#
# Settings, secrets and alert history live in data/ (or DATA_PATH in .env). The service runs as the
# owner of that folder, or as uid 1000 if root owns it, never as root.
set -euo pipefail

cd "$(dirname "$0")"

SERVICE=flightalerter
PULL=false
FOLLOW_LOGS=false
DOWN=false

for arg in "$@"; do
  case "$arg" in
    --pull) PULL=true ;;
    --logs) FOLLOW_LOGS=true ;;
    --down) DOWN=true ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 1 ;;
  esac
done

if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  echo "Docker Compose is not installed." >&2
  exit 1
fi

if $DOWN; then
  if [ "$#" -gt 1 ]; then
    echo "--down can't be combined with other options." >&2
    exit 1
  fi
  echo "==> Stopping and removing the container"
  # Compose needs .env to exist to read the project, even when tearing down.
  [ -f .env ] || touch .env
  compose down --remove-orphans
  echo "Flight Alerter is stopped. Settings, secrets and alert history in data/ (or DATA_PATH) are kept."
  exit 0
fi

if [ ! -f .env ]; then
  # docker-compose.yml references .env, so it must exist even if empty.
  echo "No .env found; creating an empty one (all settings can be set in the dashboard)."
  touch .env
fi
chmod 600 .env

if $PULL; then
  echo "==> Pulling latest code"
  git pull --ff-only
fi

DATA_PATH=$(grep -E '^DATA_PATH=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)
DATA_PATH=${DATA_PATH:-./data}
mkdir -p "$DATA_PATH"

# Run as the data folder's owner, but never as root.
OWNER=$(stat -c '%u:%g' "$DATA_PATH" 2>/dev/null || stat -f '%u:%g' "$DATA_PATH")
[ "${OWNER%%:*}" = 0 ] && OWNER=1000:1000
export APP_UID=${OWNER%%:*} APP_GID=${OWNER##*:}

echo "==> Building image"
compose build

# Older versions kept these in this folder itself. Move them only once the new image is built, and
# stop the old container first: it reads them from here, and would otherwise serve the first-time
# setup page (and write fresh default settings) the moment they disappear.
TO_MOVE=""
for file in settings.json secrets.json sentAlerts.csv militaryFlights.csv monitoredFlights.csv; do
  [ -f "$file" ] || continue
  if [ -e "$DATA_PATH/$file" ]; then
    echo "Note: $file is in both $(pwd) and $DATA_PATH; using the one in $DATA_PATH." >&2
  else
    TO_MOVE="$TO_MOVE $file"
  fi
done
if [ -n "$TO_MOVE" ]; then
  echo "==> Stopping the old container to move its data"
  compose stop "$SERVICE"
  for file in $TO_MOVE; do
    echo "Moving $file into $DATA_PATH/"
    mv "$file" "$DATA_PATH/$file"
  done
fi

# Files written by older versions (which ran as root) must be readable by the service's user.
compose run --rm --no-deps --user 0:0 --entrypoint sh "$SERVICE" -c \
  "chown -R $APP_UID:$APP_GID /data && if [ -f /data/secrets.json ]; then chmod 600 /data/secrets.json; fi"

echo "==> Starting container"
compose up -d --remove-orphans

PORT=$(grep -E '^WEB_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '"' || true)
PORT=${PORT:-8095}
URL="http://localhost:${PORT}"

echo -n "==> Waiting for the dashboard on port ${PORT}"
STATUS=""
for _ in $(seq 1 60); do
  if STATUS=$(curl -fsS "${URL}/api/auth/status" 2>/dev/null); then
    break
  fi
  echo -n "."
  sleep 1
done
echo

CONTAINER=$(compose ps -q "$SERVICE")
if [ -z "$STATUS" ]; then
  echo "The dashboard did not respond within 60 seconds. Recent logs:" >&2
  [ -n "$CONTAINER" ] && docker logs --tail 40 "$CONTAINER" >&2
  exit 1
fi

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Flight Alerter is running: http://${HOST_IP:-localhost}:${PORT}"

if echo "$STATUS" | grep -q '"configured": *false'; then
  CODE=$(docker logs "$CONTAINER" 2>&1 | grep -o 'setup code: [A-F0-9]*' | tail -1 | awk '{print $3}')
  echo
  echo "First-time setup: open the dashboard and enter setup code ${CODE:-<not found, check docker logs>}"
fi

if $FOLLOW_LOGS; then
  compose logs -f "$SERVICE"
fi
