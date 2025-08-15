# main.py
from __future__ import annotations

import asyncio
from hypercorn.asyncio import serve
from hypercorn.config import Config as HyperConfig
from projectwise import create_app


async def main():
    app = await create_app()

    cfg = HyperConfig()
    cfg.bind = ["0.0.0.0:8000"]

    await serve(app, cfg)


if __name__ == "__main__":
    asyncio.run(main())
