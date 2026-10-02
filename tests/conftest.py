"""pytest 全局配置与共享夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from app.schema import Catalog, load_demo_catalog
from app.seed import build


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return load_demo_catalog()


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    """整个测试会话共用一个演示库（固定 seed，内容一致）。"""
    return build(tmp_path_factory.mktemp("db") / "demo.db")
