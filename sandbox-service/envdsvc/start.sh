#!/bin/sh
# Feature-driven entrypoint: SBX_FEATURES is a comma separated list of
# envd (files + commands), jupyter (run_code), browser (Chromium CDP)
# Port assignment is configurable via env vars (needed when running with --network=host
# so each container can bind to its own unique host port; defaults match the
# container-port convention for bridge-mode).
# P4 路径沙箱: /workspace 由 base 镜像预先 chown 给 user,这里只是兜底。
mkdir -p /home/user/workspace
cd /app

FEATURES="${SBX_FEATURES:-envd,jupyter}"
ENVD_PORT="${ENVD_PORT:-49983}"
JUPYTER_PORT="${JUPYTER_PORT:-49999}"
BROWSER_PORT="${BROWSER_PORT:-3000}"
PIDS=""

has() {
  case ",$FEATURES," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

if has envd; then
  python -m uvicorn mini_envd:app --host 0.0.0.0 --port "$ENVD_PORT" --log-level info &
  PIDS="$PIDS $!"
  echo "[start.sh] envd pid=$! (port $ENVD_PORT)"
fi

if has jupyter; then
  python -m uvicorn jupyter_svc:app --host 0.0.0.0 --port "$JUPYTER_PORT" --log-level info &
  PIDS="$PIDS $!"
  echo "[start.sh] jupyter pid=$! (port $JUPYTER_PORT)"
fi

if has browser; then
  python -m uvicorn browser_svc:app --host 0.0.0.0 --port "$BROWSER_PORT" --log-level info &
  PIDS="$PIDS $!"
  echo "[start.sh] browser pid=$! (port $BROWSER_PORT)"
fi

if [ -z "$PIDS" ]; then
  echo "[start.sh] no features enabled, SBX_FEATURES=$FEATURES"
  exit 1
fi

while true; do
  alive=0
  for p in $PIDS; do
    if kill -0 "$p" 2>/dev/null; then
      alive=1
    fi
  done
  if [ "$alive" = "0" ]; then
    echo "[start.sh] all services exited"
    break
  fi
  sleep 5
done
exit 1
