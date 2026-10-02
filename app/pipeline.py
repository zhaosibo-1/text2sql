"""编排：问题 → schema 检索 → 生成 → 校验 → 执行 → 带完整过程返回。

设计上最重要的一条：**返回里带过程**。
Text2SQL 的失败模式几乎都不是"没结果"，而是"结果不对" ——
用户看不出 SQL 错在哪，除非把「识别成了什么、为什么这么对齐、
校验拦了什么、截断了没有」全部摆在明面上。
这就是 ``pipeline.trace`` 的存在理由。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .generator import GenerationResult, LLMNotConfigured, \
    LLMGenerator, RuleBasedGenerator
from .guard import GuardError, GuardReport, validate
from .sandbox import Sandbox
from .schema import Catalog, SchemaError


class PipelineError(ValueError):
    """流水线的业务级失败（区别于 GuardError 这种安全级失败）。"""


@dataclass
class Trace:
    """一次查询的完整过程。给前端的，也是给排障的。"""

    question: str
    generator: str = ""
    matched: list[str] = field(default_factory=list)
    confidence: float = 0.0
    tables_used: list[str] = field(default_factory=list)
    columns_used: list[str] = field(default_factory=list)
    guard_notes: list[str] = field(default_factory=list)
    row_truncated: bool = False
    elapsed: dict[str, float] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "generator": self.generator,
            "matched": self.matched,
            "confidence": round(self.confidence, 3),
            "tables_used": self.tables_used,
            "columns_used": self.columns_used,
            "guard_notes": self.guard_notes,
            "row_truncated": self.row_truncated,
            "elapsed": {k: round(v * 1000, 2)
                        for k, v in self.elapsed.items()},
        }


class Pipeline:
    def __init__(self, catalog: Catalog, sandbox: Sandbox, *,
                 llm: LLMGenerator | None = None) -> None:
        self.catalog = catalog
        self.sandbox = sandbox
        self.rule = RuleBasedGenerator(catalog)
        self.llm = llm

    # -- schema 检索 -------------------------------------------------------

    def relevant_tables(self, question: str) -> list[str]:
        """问题里提到哪些表。用于给 LLM 的提示瘦身、给规则版消歧。"""
        hits: list[str] = []
        for t in self.catalog.tables.values():
            for key in (t.name, *t.aliases):
                if key in question and t.name not in hits:
                    hits.append(t.name)
                    break
        return hits

    # -- 主流程 ------------------------------------------------------------

    def run(self, question: str, *, prefer_llm: bool = False,
            row_cap: int = 200) -> dict[str, Any]:
        if not question or not question.strip():
            raise PipelineError("问题不能为空")
        if len(question) > 300:
            raise PipelineError("问题太长（>300 字符）—— 一次问一件事")

        trace = Trace(question=question.strip())
        t0 = time.perf_counter()

        # 1. 生成
        gen = self._generate(question, prefer_llm, trace)
        trace.generator = gen.method
        trace.matched = gen.matched
        trace.confidence = gen.confidence
        trace.elapsed["generate"] = time.perf_counter() - t0

        # 2. 校验（安全层，独立于生成器）
        t1 = time.perf_counter()
        try:
            report = validate(gen.sql, self.catalog, row_cap=row_cap)
        except GuardError as exc:
            trace.elapsed["guard"] = time.perf_counter() - t1
            # 校验失败要带原因返回，而不是抛 500 ——
            # 用户需要知道为什么没结果，以及该怎么换个问法
            return {
                "ok": False,
                "stage": "guard",
                "error": exc.reason,
                "detail": exc.detail,
                "sql": gen.sql,
                "trace": trace.describe(),
            }
        trace.elapsed["guard"] = time.perf_counter() - t1
        trace.tables_used = report.tables_used
        trace.columns_used = report.columns_used
        trace.guard_notes = report.notes

        # 3. 执行
        t2 = time.perf_counter()
        try:
            columns, rows, elapsed = self.sandbox.execute(report)
        except GuardError as exc:
            trace.elapsed["execute"] = time.perf_counter() - t2
            return {
                "ok": False, "stage": "execute",
                "error": exc.reason, "detail": exc.detail,
                "sql": report.sql, "trace": trace.describe(),
            }
        trace.elapsed["execute"] = elapsed
        trace.row_truncated = any("截断" in n for n in report.notes)

        return {
            "ok": True,
            "sql": report.sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "row_cap": row_cap,
            "trace": trace.describe(),
            "guard": report.describe(),
            "generation": gen.describe(),
        }

    def _generate(self, question: str, prefer_llm: bool,
                  trace: Trace) -> GenerationResult:
        llm_errors: list[str] = []
        if prefer_llm and self.llm is not None:
            try:
                return self.llm.generate(question)
            except LLMNotConfigured as exc:
                llm_errors.append(str(exc))
                trace.matched.append("llm_unavailable")
        # 规则版永远可用：它是兜底，不是备胎
        return self.rule.generate(question)


def build_default(db_path: str, *, row_cap: int = 200) -> Pipeline:
    from .schema import load_demo_catalog

    catalog = load_demo_catalog()
    sandbox = Sandbox(db_path, row_cap=row_cap)
    llm = LLMGenerator(catalog)
    return Pipeline(catalog, sandbox, llm=llm)
