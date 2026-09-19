#!/bin/sh
# Render/Docker entrypoint: POT sidecar (optional) + API server.
# POT_ENABLED=1 nahi hai to sirf uvicorn chalta hai — purana behavior same.
if [ "$POT_ENABLED" = "1" ]; then
  echo "POT: starting PO-Token sidecar on 127.0.0.1:4416 ..."
  node /opt/bgutil/server/build/main.js >>/tmp/potserver.log 2>&1 &
  i=0
  while [ $i -lt 40 ]; do
    if python3 -c "import socket; s=socket.create_connection(('127.0.0.1',4416), timeout=1); s.close()" 2>/dev/null; then
      echo "POT: sidecar UP"
      break
    fi
    i=$((i + 1))
    sleep 1
  done
  if [ $i -ge 40 ]; then
    echo "POT: WARNING sidecar did not come up — YouTube POT path fail hoga, baaki sab normal"
  fi
else
  echo "POT: disabled (POT_ENABLED!=1) — cookie-based YouTube path"
fi
exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}
