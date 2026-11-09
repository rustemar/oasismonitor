#!/bin/sh
# Kept as the stable executable path for existing installations.
exec python3 "$(dirname "$0")/metrics_collector.py" "$@"
