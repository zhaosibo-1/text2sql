"""text2sql：自然语言查数据库。

安全层是主角，生成器是可替换的：
  schema.py    表结构目录与名称解析（含中文别名）
  guard.py     SQL 安全层 —— 词法切分 / 语句类型白名单 / 标识符校验 / 行数上限
  sandbox.py   SQLite 只读沙箱（URI mode=ro + authorizer + 进度超时）
  generator.py 生成器：规则版（离线可测）+ LLM 版（OpenAI 兼容，可选）
  pipeline.py  编排：schema 检索 → 生成 → 校验 → 执行 → 带守卫日志返回
  seed.py      演示数据库（电商：客户 / 商品 / 订单 / 订单明细）
  main.py      FastAPI 路由（唯一允许第三方依赖的模块）
"""

from __future__ import annotations

__version__ = "1.0.0"
