"""启动入口：python -m shopmgr [--port 8765] [--no-open]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import webbrowser

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="闲鱼多店铺管理工具")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开管理页面")
    args = ap.parse_args()
    if args.port:
        os.environ["SHOPMGR_PORT"] = str(args.port)

    from .config import Settings
    from .web.app import create_app

    settings = Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(settings)
    url = f"http://{settings.host}:{settings.port}/"

    # Playwright 需要支持子进程的事件循环；Windows 下显式使用 Proactor，并让 uvicorn 复用当前循环
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    config = uvicorn.Config(app, host=settings.host, port=settings.port, loop="none", log_level="info")
    server = uvicorn.Server(config)

    async def run():
        if settings.open_browser and not args.no_open:
            async def _open():
                while not server.started:
                    await asyncio.sleep(0.2)
                webbrowser.open(url)
            asyncio.create_task(_open())
        print(f"闲鱼多店铺管理已启动（无头浏览器模式，不弹出任何窗口）：{url}")
        await server.serve()

    asyncio.run(run())


if __name__ == "__main__":
    main()
