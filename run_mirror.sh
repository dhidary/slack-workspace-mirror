#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
set -a
. "$SCRIPT_DIR/mirror.env"
set +a

MODE=${1:-watch}
case "$MODE" in
    dry-run|once|watch) ;;
    *)
        echo "Usage: $0 [dry-run|once|watch]" >&2
        exit 2
        ;;
esac

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/slack_mirror.py" "$MODE"
