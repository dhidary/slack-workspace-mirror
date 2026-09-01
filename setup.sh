#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

if [ ! -f mirror.env ]; then
    cp mirror.env.example mirror.env
    chmod 600 mirror.env
    echo "Created mirror.env. Add the two source tokens before running the mirror."
    echo "The destination token is optional and only needed for Slack mirroring."
else
    echo "Kept existing mirror.env."
fi

echo "Setup complete. Next: ./run_mirror.sh dry-run"
