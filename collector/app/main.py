"""铜期货盘口采集器 API。

- GET /book                已同步时返回序号 + 按价排序的买卖盘口；失步返回 503
- GET /status              任意时刻可用：同步状态、序号、世代、统计计数
- GET /events/short-dwell  最近 100 条短驻留大额挂单事件（新的在前，失步也可查）
- GET /docs                Swagger UI 在线查看/调用
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .config import Settings
from .sync_manager import SyncManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = Settings()
manager = SyncManager(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(
    title="Copper Futures Order Book Collector",
    version="1.0.0",
    description="单合约盘口重建：快照 + 连续增量衔接，失步返回 503，迟到快照自动丢弃。",
    lifespan=lifespan,
)


@app.get("/", summary="导航")
async def index() -> dict:
    return {
        "service": app.title,
        "symbol": settings.symbol,
        "links": {
            "book": "/book",
            "status": "/status",
            "short_dwell_events": "/events/short-dwell",
            "docs": "/docs",
        },
    }


@app.get(
    "/book",
    summary="查询当前盘口",
    responses={503: {"description": "失步：启动中、断流或发现缺号，盘口不可信"}},
)
async def get_book():
    async with manager.lock:
        if not manager.synced:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "book_not_synchronized",
                    "detail": "盘口未同步（启动/断流/缺号恢复中），暂不可信",
                    "synced": False,
                    "last_error": manager.last_error,
                },
            )
        return manager.book.view()


@app.get("/status", summary="查询同步状态与统计")
async def get_status() -> dict:
    async with manager.lock:
        return manager.status_view()


@app.get(
    "/events/short-dwell",
    summary="查询最近的短驻留大额挂单事件",
    responses={
        200: {
            "description": "最近 N 条事件（新的在前）。"
            "事件 = 同侧某价位从无档变为 >=min_qty 手，"
            "随后 max_dwell 条增量内归零且期间未改量；失步期间历史事件仍可查。"
        }
    },
)
async def get_short_dwell_events(
    limit: int = Query(default=100, ge=1, le=1000, description="返回条数上限（不超过容量）"),
) -> dict:
    async with manager.lock:
        return manager.short_dwell_view(limit)
