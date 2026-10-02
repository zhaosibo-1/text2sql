"""HTTP 层。唯一允许第三方依赖的模块（CI 门禁强制）。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .generator import LLMNotConfigured
from .guard import ALLOWED_FUNCTIONS, GuardError, validate
from .pipeline import PipelineError, build_default
from .schema import SchemaError, load_demo_catalog

STARTED_AT = time.time()
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "demo.db"

catalog = load_demo_catalog()


def _ensure_db() -> None:
    """演示库不存在就现建（固定 seed，产物逐字节一致）。"""
    from .seed import build

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        build(DB_PATH)


_ensure_db()
pipeline = build_default(str(DB_PATH))
WEB_DIR = "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(
    title="text2sql",
    version=__version__,
    description="自然语言查数据库：白名单 SQL 安全层 + SQLite 只读沙箱",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class QueryPayload(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    prefer_llm: bool = False
    row_cap: int = Field(default=200, ge=1, le=1000)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "llm_configured": bool(pipeline.llm and pipeline.llm.configured),
        "db": DB_PATH.name,
        "tables": list(catalog.tables),
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
    }


@app.get("/api/options")
def options() -> dict[str, Any]:
    return {
        "row_cap": pipeline.sandbox.row_cap,
        "timeout_ms": pipeline.sandbox.timeout_ms,
        "allowed_functions": sorted(ALLOWED_FUNCTIONS),
        "statement_types": sorted({"SELECT", "WITH"}),
        "examples": [
            "一共有多少个客户",
            "每个城市的客户数",
            "金额最高的前5个订单",
            "数码类目下有哪些商品",
            "2025年5月的订单总金额",
            "已取消的订单有多少笔",
            "卖得最多的前3个商品",
            "上海客户的订单",
        ],
    }


@app.get("/api/schema")
def schema() -> dict[str, Any]:
    """表结构目录。Text2SQL 最大的杠杆是「模型看得见正确的地图」，
    所以这个接口是给前端和调试用的第一入口。"""
    return catalog.describe()


@app.get("/api/schema/prompt")
def schema_prompt() -> dict[str, Any]:
    """给 LLM 的 schema 提示词原样展示 —— 提示词不是秘密，是资产。"""
    return {"prompt": catalog.to_prompt()}


@app.post("/api/query")
def query(body: QueryPayload) -> dict[str, Any]:
    try:
        return pipeline.run(body.question,
                            prefer_llm=body.prefer_llm,
                            row_cap=body.row_cap)
    except PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SchemaError as exc:
        raise HTTPException(status_code=500, detail=f"schema 异常：{exc}") from exc


@app.post("/api/sql/check")
def check_sql(body: dict[str, Any]) -> dict[str, Any]:
    """手动跑一遍安全层。给「我自己写 SQL」的场景用 ——
    同一道闸门，不因为 SQL 是人写的就放宽。"""
    sql = str(body.get("sql", ""))
    try:
        report = validate(sql, catalog,
                          row_cap=int(body.get("row_cap", 200)))
    except GuardError as exc:
        return {"ok": False, "error": exc.reason, "detail": exc.detail}
    return {"ok": True, "report": report.describe()}


@app.get("/")
def index_page() -> FileResponse:
    return FileResponse(f"{WEB_DIR}/index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
