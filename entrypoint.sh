#!/bin/sh
set -eu
# A newly attached persistent disk is initially owned by root. Drop privileges
# before Python or Chromium starts; never run the browser as root.
mkdir -p "${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
    chown pwuser:pwuser "${DATA_DIR:-/data}"
    exec runuser -u pwuser -- python -m tracker.worker "$@"
fi
exec python -m tracker.worker "$@"
