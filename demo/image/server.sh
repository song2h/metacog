#!/bin/sh
echo "api $VERSION listening on :8080"
if [ "$VERSION" = "v2" ]; then
  sleep 60
  echo "panic: config key payments.url missing" >&2
  exit 1
fi
while true; do sleep 3600; done
