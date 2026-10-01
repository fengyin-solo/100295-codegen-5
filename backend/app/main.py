"""特种设备安全管理平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.store import store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时通过正式业务动作生成租赁演示台账；已有数据时跳过，保证可重复启动。
    from app.services.rental import RentalService
    from app.services.rental_demo import bootstrap_demo

    bootstrap_demo(RentalService())
    yield


app = FastAPI(title="特种设备安全管理平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    data = store.overview()
    # 租赁概览挂在同一份返回里：在租台数、应收已收都取自租赁服务的汇总口径。
    from app.services.rental import RentalService

    data["rental"] = RentalService().summary()
    return data
