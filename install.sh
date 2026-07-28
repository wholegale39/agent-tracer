#!/bin/sh
cd /opt/data/agent-tracer
/opt/data/agent-tracer/venv/bin/python3 -m pip install fastapi uvicorn httpx aiosqlite loguru
echo "DONE"
