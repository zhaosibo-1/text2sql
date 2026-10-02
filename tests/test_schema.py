"""schema 目录的测试。

重点不是「别名能对上」，而是：
* 歧义要**报出来**而不是擅自猜；
* 外键引用错了要在**定义时**就拦住，而不是等到 JOIN 才炸。
"""
from __future__ import annotations

import pytest

from app.schema import (Catalog, Column, SchemaError, Table,
                        load_demo_catalog)


class TestDefinition:
    def test_demo_catalog_loads(self, catalog):
        assert set(catalog.tables) >= {"customers", "products",
                                       "orders", "order_items"}

    def test_duplicate_table_rejected(self):
        t = Table(name="x", columns=(Column("id", "INTEGER", primary_key=True),))
        with pytest.raises(SchemaError, match="重复"):
            Catalog([t, t])

    def test_table_without_columns_rejected(self):
        with pytest.raises(SchemaError, match="没有任何列"):
            Catalog([Table(name="x", columns=())])

    def test_bad_reference_format_rejected(self):
        t = Table(name="a", columns=(
            Column("id", "INTEGER", primary_key=True),
            Column("b", "INTEGER", references="no_dot"),
        ))
        with pytest.raises(SchemaError, match="表名.列名"):
            Catalog([t])

    def test_reference_to_missing_table_rejected(self):
        t = Table(name="a", columns=(
            Column("id", "INTEGER", primary_key=True),
            Column("b", "INTEGER", references="ghost.id"),
        ))
        with pytest.raises(SchemaError, match="不存在"):
            Catalog([t])

    def test_reference_to_missing_column_rejected(self):
        good = Table(name="b", columns=(
            Column("id", "INTEGER", primary_key=True),))
        bad = Table(name="a", columns=(
            Column("id", "INTEGER", primary_key=True),
            Column("b", "INTEGER", references="b.ghost"),
        ))
        with pytest.raises(SchemaError, match="不存在"):
            Catalog([good, bad])


class TestTableResolution:
    def test_exact_name(self, catalog):
        rs = catalog.resolve_table("customers")
        assert rs and rs[0].table == "customers" and rs[0].via == "exact"

    def test_chinese_alias(self, catalog):
        rs = catalog.resolve_table("订单")
        assert [r.table for r in rs] == ["orders"]

    def test_alias_with_suffix_表(self, catalog):
        assert catalog.resolve_table("订单表")[0].table == "orders"

    def test_unknown_returns_empty(self, catalog):
        assert catalog.resolve_table("宇宙飞船") == []

    def test_fuzzy_suggestion(self, catalog):
        rs = catalog.resolve_table("customer")
        assert rs and rs[0].table == "customers"
        assert rs[0].via == "fuzzy"


class TestColumnResolution:
    def test_exact(self, catalog):
        rs = catalog.resolve_column("total_amount")
        assert rs[0].table == "orders" and rs[0].column == "total_amount"

    def test_chinese_alias(self, catalog):
        rs = catalog.resolve_column("金额")
        assert (rs[0].table, rs[0].column) == ("orders", "total_amount")

    def test_price_alias_points_to_products(self, catalog):
        rs = catalog.resolve_column("单价")
        assert rs[0].table == "products" and rs[0].column == "price"

    def test_table_hint_disambiguates(self, catalog):
        # "name" 在 customers / products 里都有
        rs = catalog.resolve_column("name", table_hint="products")
        assert all(r.table == "products" for r in rs)

    def test_ambiguous_without_hint_keeps_all(self, catalog):
        rs = catalog.resolve_column("name")
        assert {r.table for r in rs} == {"customers", "products"}

    def test_unknown_column_empty(self, catalog):
        assert catalog.resolve_column("身高") == []


class TestJoinPath:
    def test_orders_to_customers(self, catalog):
        path = catalog.join_path("orders", "customers")
        assert path == [("orders", "customer_id", "customers")]

    def test_same_table_no_path(self, catalog):
        assert catalog.join_path("orders", "orders") is None

    def test_unrelated_tables(self, catalog):
        assert catalog.join_path("customers", "products") is None


class TestPrompt:
    def test_prompt_contains_tables_and_aliases(self, catalog):
        p = catalog.to_prompt()
        assert "表 customers" in p
        assert "total_amount" in p
        assert "订单金额" in p

    def test_prompt_can_be_limited(self, catalog):
        p = catalog.to_prompt(only=["products"])
        assert "表 products" in p
        assert "表 customers" not in p

    def test_describe_shape(self, catalog):
        d = catalog.describe()
        assert d["table_count"] == 4
        assert len(d["tables"]) == 4
        assert all("columns" in t for t in d["tables"])
