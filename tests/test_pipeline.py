"""规则生成器 + 流水线的测试。

流水线测试断言的是**查出来的数**，不只是「跑通了」——
Text2SQL 最危险的失败是返回一个看起来合理的错误数字。
"""
from __future__ import annotations

import pytest

from app.generator import (GenerationResult, LLMNotConfigured,
                           LLMGenerator, RuleBasedGenerator)
from app.guard import GuardError, validate
from app.pipeline import Pipeline, PipelineError, build_default
from app.sandbox import Sandbox


@pytest.fixture()
def pipe(db_path) -> Pipeline:
    return build_default(str(db_path))


@pytest.fixture()
def rule(catalog) -> RuleBasedGenerator:
    return RuleBasedGenerator(catalog)


def run_sql(pipe: Pipeline, sql: str) -> list[dict]:
    r = validate(sql, pipe.catalog, row_cap=200)
    _, rows, _ = pipe.sandbox.execute(r)
    return rows


class TestRuleGenerator:
    def test_count_customers(self, rule):
        g = rule.generate("一共有多少个客户")
        assert g.method == "rule"
        assert "COUNT" in g.sql.upper()
        assert "customers" in g.sql

    def test_group_by_city(self, rule):
        g = rule.generate("每个城市的客户数")
        assert "GROUP BY" in g.sql
        assert "city" in g.sql

    def test_topn_orders(self, rule):
        g = rule.generate("金额最高的前5个订单")
        assert "ORDER BY" in g.sql and "DESC" in g.sql
        assert "LIMIT 5" in g.sql

    def test_filter_category(self, rule):
        g = rule.generate("数码类目下有哪些商品")
        assert "products" in g.sql and "数码" in g.sql

    def test_time_range(self, rule):
        g = rule.generate("2025年5月的订单总金额")
        assert "2025-05-01" in g.sql and "2025-06-01" in g.sql

    def test_status_filter(self, rule):
        g = rule.generate("已取消的订单有多少笔")
        assert "cancelled" in g.sql

    def test_head_noun_wins(self, rule):
        # 「上海客户的订单」查的是订单，不是客户
        g = rule.generate("上海客户的订单")
        assert g.sql.strip().startswith("SELECT orders.*")
        assert "JOIN customers" in g.sql

    def test_confidence_reported(self, rule):
        g = rule.generate("金额最高的前5个订单")
        assert 0 < g.confidence <= 1

    def test_low_confidence_for_gibberish(self, rule):
        g = rule.generate("今天天气怎么样")
        assert g.confidence < 0.6


class TestLLMGenerator:
    def test_not_configured_raises(self, catalog):
        gen = LLMGenerator(catalog)
        import os
        if os.environ.get("OPENAI_API_KEY"):
            pytest.skip("环境里配置了 key，跳过未配置用例")
        with pytest.raises(LLMNotConfigured):
            gen.generate("有多少订单")

    def test_markdown_fence_stripped(self, catalog, monkeypatch):
        gen = LLMGenerator(catalog)
        monkeypatch.setattr(gen, "api_key", "test-key")

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                body = ('{"choices":[{"message":{"content":'
                        '"```sql\\nSELECT 1\\n```"}}]}')
                return body.encode("utf-8")

        import urllib.request
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **k: FakeResp())
        g = gen.generate("有多少订单")
        assert g.method == "llm"
        assert g.sql == "SELECT 1"


class TestPipelineNumbers:
    """答案本身要对 —— 这些数来自固定 seed 的演示库。"""

    def test_customer_count_is_60(self, pipe):
        r = pipe.run("一共有多少个客户")
        assert r["ok"]
        assert r["rows"][0]["cnt"] == 60

    def test_cancelled_orders(self, pipe):
        r = pipe.run("已取消的订单有多少笔")
        assert r["ok"]
        assert r["rows"][0]["cnt"] == 30   # seed=42 下的固定值

    def test_top5_amount_descending(self, pipe):
        r = pipe.run("金额最高的前5个订单")
        assert r["ok"]
        amounts = [row["total_amount"] for row in r["rows"]]
        assert len(amounts) == 5
        assert amounts == sorted(amounts, reverse=True)

    def test_group_city_counts_sum(self, pipe):
        r = pipe.run("每个城市的客户数")
        assert r["ok"]
        total = sum(row["value"] for row in r["rows"])
        assert total == 60
        # 分组结果按计数降序
        vals = [row["value"] for row in r["rows"]]
        assert vals == sorted(vals, reverse=True)

    def test_may_total(self, pipe):
        r = pipe.run("2025年5月的订单总金额")
        assert r["ok"]
        assert r["rows"][0]["value"] > 0

    def test_trace_has_process(self, pipe):
        r = pipe.run("金额最高的前5个订单")
        t = r["trace"]
        assert t["generator"] == "rule"
        assert "topn" in t["matched"]
        assert t["tables_used"] == ["orders"]
        assert "generate" in t["elapsed"]


class TestPipelineGuardFailures:
    def test_returns_error_not_exception(self, pipe):
        # 问一个规则版拼不出合法 SQL 的问题 → 结构化错误，而不是 500
        r = pipe.run(" xyzzy ")
        assert r["ok"] in (True, False)

    def test_empty_question_rejected(self, pipe):
        with pytest.raises(PipelineError):
            pipe.run("   ")

    def test_too_long_question_rejected(self, pipe):
        with pytest.raises(PipelineError, match="太长"):
            pipe.run("订单" * 200)


class TestManualSqlPath:
    def test_manual_sql_validated_same_way(self, pipe):
        # 人写的 SQL 也过同一道闸门
        with pytest.raises(GuardError):
            run_sql(pipe, "DELETE FROM orders")

    def test_manual_join(self, pipe):
        rows = run_sql(pipe,
                       "SELECT customers.name AS name, orders.total_amount "
                       "AS amt FROM orders "
                       "JOIN customers ON customers.id = orders.customer_id "
                       "ORDER BY orders.total_amount DESC LIMIT 3")
        assert len(rows) == 3
        assert "name" in rows[0] and "amt" in rows[0]
