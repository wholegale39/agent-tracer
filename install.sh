#!/bin/sh
# Install dependencies for agent-tracer
set -e

# Create venv if not exists
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# Install deps
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "DONE"
echo ""
echo "Run with:"
echo "  ./venv/bin/python -m src.api"
