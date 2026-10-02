"""SQL 安全层的测试 —— 这个项目最重要的一组测试。

每一条都对应一个**真实的攻击面或事故面**：
字符串里的分号、藏在注释里的写操作、幻觉列、歧义列、无 LIMIT 全表扫。
"""
from __future__ import annotations

import pytest

from app.guard import (GuardError, Token, tokenize, strip_comments,
                       validate)


def ok(sql: str, catalog, **kw):
    return validate(sql, catalog, **kw)


class TestComments:
    def test_line_comment_stripped(self, catalog):
        r = ok("SELECT id FROM orders -- 注释", catalog)
        assert r.sql.startswith("SELECT id FROM orders")

    def test_block_comment_stripped(self, catalog):
        r = ok("SELECT /* 订单 */ id FROM orders", catalog)
        assert "订单" not in r.sql

    def test_unterminated_block_comment_rejected(self, catalog):
        with pytest.raises(GuardError, match="未闭合"):
            ok("SELECT id FROM orders /* 没写完", catalog)

    def test_write_hidden_in_comment_is_not_flagged(self, catalog):
        # 注释里的 "DROP" 不该误伤，但也不该被当成 SQL 放过 —— 它就不存在了
        r = ok("SELECT id FROM orders -- DROP TABLE x", catalog)
        assert r.tables_used == ["orders"]


class TestTokenizer:
    def test_string_literal_is_one_token(self):
        toks = tokenize("'a; DROP TABLE x'")
        assert len(toks) == 1 and toks[0].kind == "string"

    def test_escaped_quote(self):
        toks = tokenize("'it''s'")
        assert toks[0].value == "'it''s'"

    def test_unterminated_string_rejected(self):
        with pytest.raises(GuardError, match="没有闭合"):
            tokenize("'abc")

    def test_quoted_identifier_closed_check(self):
        with pytest.raises(GuardError, match="没有闭合"):
            tokenize('SELECT "abc')

    def test_parameter_placeholder_rejected(self):
        with pytest.raises(GuardError, match="占位符"):
            tokenize("SELECT * FROM orders WHERE id = ?")

    def test_illegal_char_rejected(self):
        with pytest.raises(GuardError, match="不允许的字符"):
            tokenize("SELECT ~ FROM orders")

    def test_chinese_identifier_supported(self):
        toks = tokenize("SELECT 金额 FROM 订单")
        assert toks[1].value == "金额"

    def test_unterminated_comment_reported_with_position(self, catalog):
        with pytest.raises(GuardError) as ei:
            ok("SELECT id FROM orders /* abc", catalog)
        assert "位置" in ei.value.detail


class TestStatementType:
    def test_select_allowed(self, catalog):
        assert ok("SELECT id FROM orders", catalog).tables_used == ["orders"]

    def test_with_allowed(self, catalog):
        sql = ("WITH t AS (SELECT id FROM orders) "
               "SELECT t.id FROM t")
        r = ok(sql, catalog)
        assert "orders" in r.tables_used

    def test_insert_rejected(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("INSERT INTO orders VALUES (1)", catalog)

    def test_update_rejected(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("UPDATE orders SET status = 'x'", catalog)

    def test_delete_rejected(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("DELETE FROM orders", catalog)

    def test_drop_rejected(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("DROP TABLE orders", catalog)

    def test_create_rejected(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("CREATE TABLE x (id INT)", catalog)

    def test_pragma_rejected(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("PRAGMA table_info(orders)", catalog)

    def test_attach_rejected(self, catalog):
        with pytest.raises(GuardError, match="不允许的词"):
            ok("SELECT * FROM orders ATTACH DATABASE 'x' AS y", catalog)

    def test_multiple_statements_rejected(self, catalog):
        with pytest.raises(GuardError, match="一条"):
            ok("SELECT id FROM orders; DROP TABLE orders", catalog)

    def test_trailing_semicolon_ok(self, catalog):
        assert ok("SELECT id FROM orders;", catalog).tables_used == ["orders"]

    def test_semicolon_inside_string_is_safe(self, catalog):
        # 字符串里的分号不是语句分隔符 —— 这是 tokenize 的核心价值
        r = ok("SELECT id FROM orders WHERE status = 'a;b'", catalog)
        assert r.tables_used == ["orders"]

    def test_empty_rejected(self, catalog):
        with pytest.raises(GuardError, match="空"):
            ok("   ", catalog)

    def test_too_long_rejected(self, catalog):
        with pytest.raises(GuardError, match="超过"):
            ok("SELECT " + "1," * 3000 + "1 FROM orders", catalog)


class TestIdentifierWhitelist:
    def test_unknown_table_rejected(self, catalog):
        with pytest.raises(GuardError, match="不存在"):
            ok("SELECT id FROM users", catalog)

    def test_hallucinated_column_rejected(self, catalog):
        # LLM 最典型的失败：编一个不存在的列
        with pytest.raises(GuardError, match="没有列"):
            ok("SELECT orders.wechat_id FROM orders", catalog)

    def test_hallucinated_table_suggests_closest(self, catalog):
        # "order" 是保留字，SQLite 本身就会语法错；
        # 用真正的拼写错误来测模糊提示
        with pytest.raises(GuardError, match="最像"):
            ok("SELECT id FROM orderz", catalog)

    def test_unknown_qualifier_rejected(self, catalog):
        with pytest.raises(GuardError, match="不是这条 SQL 里声明过的"):
            ok("SELECT o.id FROM orders", catalog)

    def test_qualified_column_must_exist(self, catalog):
        with pytest.raises(GuardError, match="没有列"):
            ok("SELECT customers.phone FROM customers", catalog)

    def test_ambiguous_column_rejected(self, catalog):
        # name 在 customers 和 products 里都有
        with pytest.raises(GuardError, match="歧义"):
            ok("SELECT name FROM customers, products", catalog)

    def test_qualified_resolves_ambiguity(self, catalog):
        r = ok("SELECT customers.name FROM customers, products "
               "WHERE products.id = customers.id", catalog)
        assert "customers.name" in r.columns_used

    def test_star_alone_rejected(self, catalog):
        # SELECT * 合法（不涉及具体列）
        r = ok("SELECT * FROM orders", catalog)
        assert r.tables_used == ["orders"]

    def test_qualified_star_allowed(self, catalog):
        r = ok("SELECT orders.* FROM orders", catalog)
        assert r.tables_used == ["orders"]

    def test_column_alias_accepted(self, catalog):
        r = ok("SELECT COUNT(*) AS cnt FROM orders", catalog)
        assert "AS cnt" in r.sql

    def test_alias_in_order_by_accepted(self, catalog):
        r = ok("SELECT customers.city AS c, COUNT(*) AS n "
               "FROM customers GROUP BY customers.city ORDER BY n DESC",
               catalog)
        assert "ORDER BY n" in r.sql

    def test_bare_alias_rejected(self, catalog):
        # 不支持 COUNT(*) cnt 这种裸别名 —— 和「忘写逗号」无法区分
        with pytest.raises(GuardError):
            ok("SELECT COUNT(*) cnt FROM orders", catalog)

    def test_table_alias_then_qualified_column(self, catalog):
        r = ok("SELECT o.id FROM orders AS o", catalog)
        assert "orders.id" in r.columns_used

    def test_table_bare_alias(self, catalog):
        r = ok("SELECT o.id FROM orders o", catalog)
        assert "orders.id" in r.columns_used


class TestFunctions:
    def test_allowed_aggregates(self, catalog):
        r = ok("SELECT COUNT(*), SUM(total_amount), AVG(total_amount), "
               "MIN(total_amount), MAX(total_amount) FROM orders", catalog)
        assert set(r.functions_used) == {"COUNT", "SUM", "AVG", "MIN", "MAX"}

    def test_unknown_function_rejected(self, catalog):
        with pytest.raises(GuardError, match="不在允许列表"):
            ok("SELECT load_extension('x') FROM orders", catalog)

    def test_write_like_function_rejected(self, catalog):
        with pytest.raises(GuardError, match="不在允许列表"):
            ok("SELECT randomblob(100000) FROM orders", catalog)

    def test_string_functions_allowed(self, catalog):
        r = ok("SELECT UPPER(name) FROM customers", catalog)
        assert "UPPER" in r.functions_used


class TestLimit:
    def test_missing_limit_added(self, catalog):
        r = ok("SELECT id FROM orders", catalog)
        assert r.sql.rstrip(";").endswith("LIMIT 200")
        assert any("补" in n for n in r.notes)

    def test_limit_within_cap_kept(self, catalog):
        r = ok("SELECT id FROM orders LIMIT 50", catalog)
        assert "LIMIT 50" in r.sql
        assert not r.limit_clamped

    def test_limit_over_cap_clamped(self, catalog):
        r = ok("SELECT id FROM orders LIMIT 100000", catalog, row_cap=200)
        assert "LIMIT 200" in r.sql
        assert r.limit_clamped

    def test_custom_cap(self, catalog):
        r = ok("SELECT id FROM orders LIMIT 100", catalog, row_cap=10)
        assert "LIMIT 10" in r.sql

    def test_inner_limit_not_touched(self, catalog):
        # 内层子查询的 LIMIT 不该被改，只管最外层
        sql = ("SELECT * FROM (SELECT id FROM orders LIMIT 3) "
               "LIMIT 5")
        r = ok(sql, catalog)
        assert "LIMIT 3" in r.sql and "LIMIT 5" in r.sql

    def test_nonnumeric_limit_rejected(self, catalog):
        with pytest.raises(GuardError, match="数字"):
            ok("SELECT id FROM orders LIMIT ALL", catalog)


class TestRealWorldBypasses:
    """这些是真实攻击/事故里出现过的写法。"""

    def test_union_is_allowed_but_validated(self, catalog):
        # UNION 本身是读，但两边都必须过标识符白名单
        with pytest.raises(GuardError):
            ok("SELECT id FROM orders UNION SELECT password FROM users",
               catalog)

    def test_case_insensitive_keyword_detection(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("delete from orders", catalog)

    def test_mixed_case_drop(self, catalog):
        with pytest.raises(GuardError, match="只允许"):
            ok("DrOp TaBlE orders", catalog)

    def test_write_via_subquery_rejected(self, catalog):
        with pytest.raises(GuardError, match="不允许的词"):
            ok("SELECT * FROM orders WHERE id IN "
               "(DELETE FROM orders RETURNING id)", catalog)

    def test_no_from_rejected(self, catalog):
        with pytest.raises(GuardError, match="FROM"):
            ok("SELECT 1+1", catalog)

    def test_select_from_literal_rejected(self, catalog):
        with pytest.raises(GuardError):
            ok("SELECT * FROM (VALUES (1),(2))", catalog)
