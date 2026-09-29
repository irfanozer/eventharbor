"""Check worker presence and dependencies, without claiming a delivery heartbeat."""
import asyncio
import os
from pathlib import Path
import sys
import urllib.request

async def check():
    # init:true places the application under Docker's init process.
    running = any(
        b"eventharbor.deliveries.worker" in path.read_bytes()
        for path in Path("/proc").glob("[0-9]*/cmdline")
        if path.exists()
    )
    if not running:
        raise RuntimeError("worker process missing")
    # Keep the health process small; importing the full ORM/app would compete
    # with the worker inside the same 128 MiB cgroup.
    import asyncpg
    dsn = os.environ["EVENTHARBOR_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    connection = await asyncpg.connect(dsn, ssl="verify-full", timeout=3)
    try:
        await connection.execute("SELECT 1")
    finally:
        await connection.close(timeout=2)
    urllib.request.urlopen("http://eventharbor-receiver-prod/health", timeout=3).close()

try:
    asyncio.run(asyncio.wait_for(check(), timeout=8))
except Exception:
    # Docker stores healthcheck output; keep database details out of it.
    sys.exit(1)
