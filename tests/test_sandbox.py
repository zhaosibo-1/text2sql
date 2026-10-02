"""只读沙箱的测试 —— 防线必须在**真实文件**上验证。"""
from __future__ import annotations

import sqlite3

import pytest

from app.guard import GuardError, GuardReport
from app.sandbox import Sandbox, SandboxBlocked
from app.schema import load_demo_catalog


@pytest.fixture()
def sandbox(db_path):
    s = Sandbox(db_path)
    yield s
    s.close()


def report_of(sql: str, catalog, row_cap=200) -> GuardReport:
    from app.guard import validate
    return validate(sql, catalog, row_cap=row_cap)


class TestReadOnly:
    def test_select_works(self, sandbox, catalog):
        r = report_of("SELECT COUNT(*) AS n FROM customers", catalog)
        cols, rows, _ = sandbox.execute(r)
        assert cols == ["n"] and rows[0]["n"] == 60

    def test_uri_mode_ro_blocks_write(self, db_path):
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM customers")
        conn.close()

    def test_authorizer_blocks_write_even_if_guard_misses(
            self, sandbox, db_path):
        """防线冗余的直接验证：绕过 guard 直接对沙箱下发写语句。

        authorizer 里 raise 的 SandboxBlocked 的消息会被 sqlite3 吞掉，
        统一变成 DatabaseError("not authorized")；
        沙箱的 execute() 负责把它翻译回人话。
        """
        with sandbox.connection() as conn:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute("DELETE FROM customers")

    def test_execute_translates_deny_into_sandbox_blocked(
            self, sandbox, catalog):
        r = report_of("SELECT id FROM orders", catalog)
        r.sql = "DELETE FROM customers;"
        with pytest.raises(SandboxBlocked, match="guard 有漏洞"):
            sandbox.execute(r)

    def test_pragma_blocked_by_authorizer(self, sandbox):
        with sandbox.connection() as conn:
            with pytest.raises(Exception):
                conn.execute("PRAGMA journal_mode = DELETE")

    def test_attach_blocked(self, sandbox, db_path):
        with sandbox.connection() as conn:
            with pytest.raises(Exception):
                conn.execute(f"ATTACH DATABASE '{db_path}' AS other")

    def test_file_unchanged_after_reads(self, sandbox, db_path, catalog):
        import hashlib
        before = hashlib.md5(db_path.read_bytes()).hexdigest()
        r = report_of("SELECT id FROM orders WHERE status = 'paid'", catalog)
        sandbox.execute(r)
        after = hashlib.md5(db_path.read_bytes()).hexdigest()
        assert before == after


class TestRowCap:
    def test_rows_capped(self, db_path, catalog):
        # SQL 里的 LIMIT 由 guard 补成 200；
        # 沙箱的 row_cap 更小时，截断由**沙箱**兜底 —— 两层限制独立生效
        s = Sandbox(db_path, row_cap=50)
        r = report_of("SELECT id FROM orders", catalog)
        _, rows, _ = s.execute(r)
        assert len(rows) == 50
        assert any("截断" in n for n in r.notes)
        s.close()

    def test_custom_cap(self, db_path, catalog):
        s = Sandbox(db_path, row_cap=7)
        r = report_of("SELECT id FROM orders", catalog, row_cap=7)
        _, rows, _ = s.execute(r)
        assert len(rows) == 7
        s.close()


class TestTimeout:
    def test_cartesian_killed(self, db_path, catalog):
        """合法但恶心的查询：三层笛卡尔积 ≈ 60*34*260 行。

        guard 放它过去（语法和标识符都没问题），
        超时保护必须在它拖死进程之前把它掐掉。
        """
        s = Sandbox(db_path, timeout_ms=800)
        # 60*60*60*260 ≈ 5600 万行，加一个防优化的 WHERE，
        # 实测 2.9s —— 800ms 的超时必须在它拖死进程之前把它掐掉
        r = report_of(
            "SELECT COUNT(*) AS n FROM customers a, customers b, "
            "customers c, orders d "
            "WHERE a.id + b.id + c.id + d.id > 0", catalog)
        with pytest.raises(GuardError, match="中断"):
            s.execute(r)
        s.close()


class TestErrors:
    def test_missing_db_file(self):
        with pytest.raises(FileNotFoundError):
            Sandbox("does_not_exist_zzz.db")

    def test_sql_error_wrapped(self, sandbox, catalog):
        # 语法错会漏到执行层（guard 只做安全校验，不做语法校验）
        r = report_of("SELECT id FROM orders", catalog)
        r.sql = "SELEC id FROM orders;"
        with pytest.raises(GuardError, match="执行失败"):
            sandbox.execute(r)


class TestSeedDirectory:
    """build() 必须自己建父目录。

    `data/` 被 .gitignore 排除 —— clone 下来的仓库、CI 的干净 checkout、
    Docker 构建上下文里都没有这个目录。不自动建的话 sqlite3.connect 报
    "unable to open database file"，而这个错误完全不提示「目录不存在」。
    CI 上第一次撞到时排查了很久，所以钉一条测试在这。
    """

    def test_creates_missing_parent_dirs(self, tmp_path):
        from app.seed import build
        target = tmp_path / "a" / "b" / "demo.db"
        out = build(str(target))
        assert out.exists() and out.stat().st_size > 0

    def test_repeated_build_is_idempotent(self, tmp_path):
        from app.seed import build
        p = tmp_path / "again.db"
        build(str(p))
        first = p.read_bytes()
        build(str(p))
        assert p.read_bytes() == first      # 固定 seed：逐字节可复现
