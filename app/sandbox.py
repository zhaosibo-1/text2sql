"""SQLite 只读沙箱。

三层防线，缺一不可：

1. **URI ``mode=ro``** —— 打开层面就写不进去。
   只靠 ``PRAGMA query_only`` 不够：它是在连接之后设置的，
   设置之前的窗口里什么都可能发生；而且它挡不住 ATTACH。
2. **``set_authorizer``** —— 语句层面再挡一次写操作与 PRAGMA。
   防线要有冗余：URI 是"门"，authorizer 是"墙里的铁栅栏"。
3. **进度处理器超时** —— 一条合法但恶心的查询（多层笛卡尔积）
   不会写坏数据，但能把进程拖死。每 N 条虚拟机指令检查一次时钟。

**每次执行开一个独立连接**，而不是复用一个长连接。

原因很实际：FastAPI 的同步接口跑在线程池里，而 sqlite3 连接默认
只能在创建它的那个线程用（``check_same_thread=True``）。
复用长连接要么得关掉这个保护，要么得加锁 ——
关掉保护意味着多个线程共享一份可变的连接状态（临时表、事务、
准备好的语句），加锁意味着读查询互相排队。
每次开新连接两个问题都没有：SQLite 打开文件是微秒级的开销，
而每个请求拿到的是一个干净的、只属于它自己的只读连接。

为什么不用内存库：内存库当然写不坏磁盘，但那样防线就成了摆设 ——
换到真实文件库上全失效。这个沙箱必须**在真实文件上**也站得住。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .guard import GuardError, GuardReport

DEFAULT_ROW_CAP = 200
DEFAULT_TIMEOUT_MS = 3000


class SandboxBlocked(RuntimeError):
    """SQLite authorizer 拦下了某个动作。理论上 guard 之后不该发生 ——
    发生了就是防线出了洞，必须大声报出来。"""


class Sandbox:
    """一个文件库的只读执行器。"""

    def __init__(self, db_path: str | Path, *, row_cap: int = DEFAULT_ROW_CAP,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在：{self.db_path}")
        self.row_cap = row_cap
        self.timeout_ms = timeout_ms
        # 启动时验证一次：文件存在 ≠ 是个能打开的 SQLite 库
        _ = self.table_count_probe()

    # -- 连接 -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # uri=True：mode=ro 在**打开**层面禁止写入
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.set_authorizer(self._authorizer)
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """给需要直接操作连接的场景（测试、元数据查询）用。"""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _authorizer(action: int, arg1, arg2, db_name, trigger_or_view) -> int:
        # action 常量见 sqlite3 文档。按"白名单"思路：
        # 只放行读路径，其余一律拒绝（这里用 raise，等价于 DENY 且带消息）。
        allowed = {
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
            sqlite3.SQLITE_ANALYZE,
        }
        if action in allowed:
            return sqlite3.SQLITE_OK
        raise SandboxBlocked(
            f"沙箱拦截了非只读操作 action={action} "
            f"arg1={arg1!r} arg2={arg2!r}")

    # -- 执行 -----------------------------------------------------------

    def execute(self, report: GuardReport) -> tuple[list[str], list[dict],
                                                    float]:
        """执行已通过 guard 的 SQL。返回 (列名, 行, 执行耗时秒)。"""
        sql = report.sql
        with self.connection() as conn:
            deadline = time.monotonic() + self.timeout_ms / 1000.0

            def _progress() -> int:
                # 返回非 0 表示中断。每 1000 条虚拟机指令看一次钟
                return 1 if time.monotonic() > deadline else 0

            conn.set_progress_handler(_progress, 1000)
            started = time.perf_counter()
            try:
                cur = conn.execute(sql)
            except sqlite3.DatabaseError as exc:
                # OperationalError 是 DatabaseError 的子类，
                # 超时中断和 authorizer 拒绝都从这里走
                msg = str(exc)
                low = msg.lower()
                if "interrupt" in low:
                    raise GuardError(
                        f"查询超过 {self.timeout_ms}ms 被强制中断 —— "
                        "通常是笛卡尔积或缺索引的全表扫描", msg) from exc
                if "not authorized" in low:
                    # authorizer 抛的 SandboxBlocked 的消息会被 sqlite3 吞掉
                    # 换成 "not authorized"；翻译回来，并保留告警级别 ——
                    # 到这里说明 guard 漏了，那是必须上报的事故。
                    raise SandboxBlocked(
                        "沙箱拦截了非只读操作 —— 如果这条 SQL 已经通过 "
                        "guard 校验，说明 guard 有漏洞，请立刻上报") from exc
                raise GuardError(f"SQL 执行失败：{msg}", msg) from exc
            rows_raw = cur.fetchmany(self.row_cap + 1)
            elapsed = time.perf_counter() - started
            columns = ([d[0] for d in cur.description]
                       if cur.description else [])
            conn.set_progress_handler(None, 0)

        truncated = len(rows_raw) > self.row_cap
        rows = rows_raw[:self.row_cap]
        out = [
            {(k): (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
             for k, v in dict(r).items()}
            for r in rows
        ]
        if truncated:
            report.notes.append(
                f"结果超过 {self.row_cap} 行，已截断 —— "
                "要更多请加更紧的过滤条件")
        return columns, out, elapsed

    # -- 元数据 ---------------------------------------------------------

    def table_count_probe(self) -> int:
        """启动自检：确认文件真能被 SQLite 打开。"""
        with self.connection() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master").fetchone()["n"])

    def table_count(self, table: str) -> int:
        with self.connection() as conn:
            cur = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"')
            return int(cur.fetchone()["n"])

    def close(self) -> None:
        """保留这个接口只为兼容调用方；每次执行都自带连接，无需关闭。"""
