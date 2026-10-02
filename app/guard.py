"""SQL 安全层：Text2SQL 的最后一道闸门。

为什么必须有这一层，而且必须**在生成器之外**：

Text2SQL 的风险不在「SQL 写错了」（大不了报错），而在「SQL 写对了但不是
用户想要的」—— 一条 `DELETE`、一次全表扫描、一个越权表。
LLM 的输出是不可信的文本，规则生成器同样可能因为模板 bug 拼出错误语句。
所以校验必须独立于生成器存在：**不管 SQL 从哪来，过的是同一道闸门。**

这一层做五件事，全部在**词法层**完成（不依赖任何 SQL 方言库）：

1. 注释剥离与终止检查（未闭合的块注释可以吞掉后面的真实语义）
2. 词法切分 —— 字符串字面量是单个 token，所以 ``'a; DROP TABLE x'``
   里的分号不会被当成语句分隔符（这是"字符串拼接防注入"的本质）
3. 语句类型白名单：只允许 SELECT / WITH
4. **标识符白名单**：每个表名、列名都必须在 schema 目录里真实存在，
   限定名（``t.col``）的限定词必须是 FROM/JOIN 里声明过的别名
5. 行数上限：没有 LIMIT 就补一个，超了就压到上限

额外收益：第 4 步同时把「LLM 幻觉列名」挡在了执行之前 ——
幻觉列在 SQLite 里只是报错，但提前拒绝能把错误从"运行时 500"
变成"422 + 一句人话"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import Catalog

#: 语句级白名单：只允许读
ALLOWED_HEADS = frozenset({"SELECT", "WITH"})

#: 这些词出现在**语句头部**之外的任何位置都直接拒绝。
#: 为什么这么严：SQLite 的 PRAGMA/VACUUM/ATTACH 都不写文件头，
#: 混在读路径里只会带来困惑；REPLACE 则是写操作。
FORBIDDEN_WORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE",
    "PRAGMA", "VACUUM", "ATTACH", "DETACH", "REINDEX", "GRANT", "REVOKE",
    "TRUNCATE", "MERGE", "EXECUTE", "EXEC", "INTO",
})

#: 允许出现的函数。SQLite 自带函数很多，但 Text2SQL 真正需要的就这些；
#: 白名单而不是黑名单，因为新函数进黑名单永远慢一步。
ALLOWED_FUNCTIONS = frozenset({
    "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "ABS", "LENGTH",
    "SUBSTR", "LOWER", "UPPER", "TRIM", "COALESCE", "IFNULL", "NULLIF",
    "CAST", "DATE", "STRFTIME", "JULIANDAY", "TOTAL", "GROUP_CONCAT",
})

#: 关键字（不是标识符，不能当列名用）
_KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT",
    "OFFSET", "AS", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS",
    "ON", "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE", "BETWEEN",
    "EXISTS", "CASE", "WHEN", "THEN", "ELSE", "END", "DISTINCT", "ASC",
    "DESC", "WITH", "UNION", "ALL", "EXCEPT", "INTERSECT", "TRUE",
    "FALSE", "OVER", "PARTITION",
})

MAX_SQL_LENGTH = 4000


class GuardError(ValueError):
    """SQL 不被允许。``reason`` 面向用户，``detail`` 面向日志。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass
class Token:
    kind: str      # ident | number | string | op | punct | param
    value: str
    pos: int

    def is_kw(self, *names: str) -> bool:
        return self.kind == "ident" and self.value.upper() in names


@dataclass
class GuardReport:
    """校验通过后的产物：清洗过的 SQL + 校验过程都看见了什么。"""

    sql: str
    tables_used: list[str] = field(default_factory=list)
    columns_used: list[str] = field(default_factory=list)
    functions_used: list[str] = field(default_factory=list)
    has_limit: bool = False
    limit_clamped: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict:
        return {
            "sql": self.sql,
            "tables_used": self.tables_used,
            "columns_used": self.columns_used,
            "functions_used": self.functions_used,
            "has_limit": self.has_limit,
            "limit_clamped": self.limit_clamped,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# 1. 注释
# ---------------------------------------------------------------------------

def strip_comments(sql: str) -> str:
    """剥掉 ``--`` 行注释与 ``/* */`` 块注释。

    未闭合的块注释必须报错而不是"剥到结尾"：
    ``SELECT 1 /* 注释`` 的语义被截断成什么样，没人说得清，
    而说不清的东西不能拿去执行。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j      # 保留换行符本身，行号不错位
        elif ch == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            if j < 0:
                raise GuardError(
                    "SQL 里有未闭合的块注释 /* —— 请补上 */",
                    f"位置 {i} 起的块注释没有结束")
            i = j + 2
            out.append(" ")
        elif ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql[j + 1:j + 2] != "'":
                    break
                j += 2 if sql[j] == "'" else 1
            if j >= n:
                raise GuardError("SQL 里有未闭合的字符串")
            out.append(sql[i:j + 1])
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 2. 词法
# ---------------------------------------------------------------------------

_IDENT = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_OP = re.compile(r"<=|>=|!=|<>|\|\||[=<>+\-*/%]")


def tokenize(sql: str) -> list[Token]:
    """切成 token。字符串字面量是**一个** token —— 这是防注入的根基。"""
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql[j + 1:j + 2] == "'":
                        j += 2          # '' 是转义的单引号
                        continue
                    break
                j += 1
            if j >= n:
                raise GuardError("字符串没有闭合", f"位置 {i}")
            tokens.append(Token("string", sql[i:j + 1], i))
            i = j + 1
            continue
        if ch in ('"', "`", "["):
            close = {'"': '"', "`": "`", "[": "]"}[ch]
            j = sql.find(close, i + 1)
            if j < 0:
                raise GuardError(f"带引号的标识符 {ch!r} 没有闭合")
            tokens.append(Token("ident", sql[i + 1:j], i))
            i = j + 1
            continue
        if ch == "?" or ch == ":" and sql[i + 1:i + 2].isalnum():
            raise GuardError(
                "SQL 不允许参数占位符 —— 值应该已经写进字面量里",
                f"位置 {i}")
        if ch == ";":
            tokens.append(Token("punct", ";", i))
            i += 1
            continue
        if ch in "(),.":
            tokens.append(Token("punct", ch, i))
            i += 1
            continue
        if ch.isdigit():
            m = _NUMBER.match(sql, i)
            tokens.append(Token("number", m.group(0), i))
            i = m.end()
            continue
        if ch.isalpha() or ch == "_" or "\u4e00" <= ch <= "\u9fff":
            m = _IDENT.match(sql, i)
            tokens.append(Token("ident", m.group(0), i))
            i = m.end()
            continue
        m = _OP.match(sql, i)
        if m:
            tokens.append(Token("op", m.group(0), i))
            i = m.end()
            continue
        raise GuardError(
            f"SQL 里有不允许的字符 {ch!r}", f"位置 {i}")
    return tokens


# ---------------------------------------------------------------------------
# 3/4/5 校验
# ---------------------------------------------------------------------------

def _collect_ctes(tokens: list[Token]) -> tuple[dict[str, list[str]],
                                                set[int]]:
    """收集 CTE（WITH 名字 AS (...)）的名字和输出列。

    CTE 的名字会像真表一样出现在 FROM 后面，不登记就会报
    「表 't' 不存在」；输出列不登记，``SELECT t.id FROM t`` 又过不了
    列校验。这里从 CTE 的 SELECT 列表里把输出列推断出来 ——
    推断不出来就不登记那一列，宁可保守。
    """
    out: dict[str, list[str]] = {}
    skip: set[int] = set()
    if not tokens or not tokens[0].is_kw("WITH"):
        return out, skip
    skip.add(0)
    depth = 0
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.value == "(":
            depth += 1
        elif t.value == ")":
            depth -= 1
        elif (depth == 0 and t.kind == "ident"
              and t.value.upper() not in _KEYWORDS
              and i + 2 < len(tokens)
              and tokens[i + 1].is_kw("AS")
              and tokens[i + 2].value == "("):
            # 找配对的右括号
            j = i + 2
            d = 0
            while j < len(tokens):
                if tokens[j].value == "(":
                    d += 1
                elif tokens[j].value == ")":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            inner = tokens[i + 3:j]
            out[t.value] = _select_output_columns(inner)
            skip.add(i)        # CTE 名字本身不是列
            skip.add(i + 1)    # AS 也不是
            i = j + 1
            continue
        i += 1
    return out, skip


def _select_output_columns(toks: list[Token]) -> list[str]:
    """一个 SELECT 列表输出了哪些列名（够用即可，不追求完美）。"""
    cols: list[str] = []
    depth = 0
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.value == "(":
            depth += 1
            i += 1
            continue
        if t.value == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0 and t.is_kw("FROM"):
            break
        if (depth == 0 and t.is_kw("AS") and i + 1 < len(toks)
                and toks[i + 1].kind == "ident"):
            cols.append(toks[i + 1].value)
            i += 2
            continue
        if (depth == 0 and t.kind == "ident"
                and t.value.upper() not in _KEYWORDS):
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            nxt2 = toks[i + 2] if i + 2 < len(toks) else None
            if nxt is not None and nxt.value == "(":
                i += 1          # 函数名，不是列
                continue
            if nxt is not None and nxt.value == "." and nxt2 is not None \
                    and nxt2.kind == "ident":
                cols.append(nxt2.value)
                i += 3
                continue
            cols.append(t.value)
            i += 1
            continue
        i += 1
    return cols


def _must_be_table(name: str, catalog: Catalog, pos: int,
                   ctes: dict[str, list[str]]) -> str:
    """表名必须真实存在。模糊接近但不相等也要拒绝 ——
    「差一点」的表名意味着生成器或模型在编造。"""
    if name in ctes:
        return name                     # CTE 名字当表名用，合法
    rs = catalog.resolve_table(name)
    if not rs:
        raise GuardError(
            f"表 {name!r} 不存在于数据库 —— 可用表见 /api/schema",
            f"位置 {pos} 引用了未知表 {name}")
    exact = [r for r in rs if r.via in ("exact", "alias")]
    if not exact:
        raise GuardError(
            f"表名 {name!r} 太接近但不等于任何已知表（最像：{rs[0].table}）",
            f"位置 {pos} 模糊匹配 {name} → {rs[0].table}")
    if len({r.table for r in exact}) > 1:
        raise GuardError(
            f"表名 {name!r} 有歧义，可能指："
            + "、".join(sorted({r.table for r in exact})),
            f"位置 {pos}")
    return exact[0].table


def _find_table_refs(tokens: list[Token], catalog: Catalog,
                     ctes: dict[str, list[str]]
                     ) -> tuple[dict[str, str], list[str], set[int]]:
    """扫 FROM / JOIN（递归进入子查询），返回表引用信息。

    为什么递归：``SELECT * FROM (SELECT id FROM orders)`` 里的
    ``orders`` 也是一次表引用，也要过白名单 —— 只扫顶层的话，
    子查询就成了白名单的逃生门。
    """
    resolved: dict[str, str] = {}
    used: list[str] = []
    skip: set[int] = set()
    n = len(tokens)

    def _derived_alias(close_pos: int, end: int) -> tuple[str, int, set[int]]:
        """解析派生表后面的可选别名，返回 (别名, 下一个位置, 跳过位)。"""
        k = close_pos + 1
        extra: set[int] = set()
        if k < end and tokens[k].is_kw("AS"):
            extra.add(k)
            k += 1
            if k < end and tokens[k].kind == "ident":
                extra.add(k)
                return tokens[k].value, k + 1, extra
            raise GuardError("AS 后面必须是别名")
        if (k < end and tokens[k].kind == "ident"
                and tokens[k].value.upper() not in _KEYWORDS):
            extra.add(k)
            return tokens[k].value, k + 1, extra
        return f"@derived{len(resolved)}", k, extra

    def scan(start: int, end: int) -> None:
        i = start
        while i < end:
            t = tokens[i]
            if not t.is_kw("FROM", "JOIN"):
                i += 1
                continue
            i += 1
            # FROM/JOIN 后面：子查询、表名，逗号分隔可重复
            while i < end:
                if tokens[i].value == "(":
                    j = i
                    d = 0
                    while j < end:
                        if tokens[j].value == "(":
                            d += 1
                        elif tokens[j].value == ")":
                            d -= 1
                            if d == 0:
                                break
                        j += 1
                    if d != 0:
                        raise GuardError("子查询括号不闭合")
                    scan(i + 1, j)              # 里面的表引用也要过白名单
                    cols = _select_output_columns(tokens[i + 1:j])
                    alias, nxt, extra = _derived_alias(j, end)
                    resolved[alias] = alias
                    ctes[alias] = cols          # 派生表的输出列
                    skip.update(extra)
                    i = nxt
                elif tokens[i].kind == "ident" \
                        and tokens[i].value.upper() not in _KEYWORDS:
                    name = tokens[i].value
                    real = _must_be_table(name, catalog, tokens[i].pos, ctes)
                    skip.add(i)
                    alias = name
                    j = i + 1
                    if j < end and tokens[j].is_kw("AS"):
                        skip.add(j)
                        j += 1
                        if j >= end or tokens[j].kind != "ident":
                            raise GuardError("AS 后面必须是别名")
                        alias = tokens[j].value
                        skip.add(j)
                        j += 1
                    elif (j < end and tokens[j].kind == "ident"
                          and tokens[j].value.upper() not in _KEYWORDS):
                        alias = tokens[j].value      # 裸别名
                        skip.add(j)
                        j += 1
                    resolved[alias] = real
                    if real not in used and not real.startswith("@"):
                        used.append(real)
                    elif real.startswith("@"):
                        pass
                    i = j
                else:
                    break
                if i < end and tokens[i].value == ",":
                    i += 1
                    continue
                break

    scan(0, n)
    return resolved, used, skip


def _collect_aliases(tokens: list[Token]) -> set[str]:
    """收集 ``AS <名字>`` 声明的输出别名。

    ``SELECT COUNT(*) AS cnt`` 里的 ``cnt`` 不是列，
    但 ``ORDER BY cnt`` 又合法地引用它。不处理就会出现
    「列 'cnt' 在用到的表里都不存在」这种自己打自己脸的报错。

    只认 ``AS`` 显式声明的别名，不认裸别名（``COUNT(*) cnt``）：
    裸别名和「忘写逗号的两列」在词法上无法区分，
    宁可要求多写两个字母，也不要静默猜错。
    """
    aliases: set[str] = set()
    last_kw = ""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        # AS 本身也是关键字，必须**先**判断，否则永远走不进来
        if t.is_kw("AS") and i + 1 < len(tokens) \
                and tokens[i + 1].kind == "ident":
            if last_kw not in ("FROM", "JOIN"):
                aliases.add(tokens[i + 1].value)   # FROM 后的 AS 是表别名
            i += 2
            continue
        if t.kind == "ident" and t.value.upper() in _KEYWORDS:
            last_kw = t.value.upper()
        i += 1
    return aliases


def _validate_columns(tokens: list[Token], catalog: Catalog,
                      resolved: dict[str, str], report: GuardReport,
                      aliases: set[str], skip: set[int],
                      ctes: dict[str, list[str]]) -> None:
    """每个非关键字标识符必须是：函数（白名单内）、已声明的表别名限定列、
    或能在已用表里唯一定位的列。"""
    used_tables = sorted(set(resolved.values()))

    def has_column(table: str, col: str) -> bool:
        if table in ctes:
            return col in ctes[table]
        return catalog.table(table).column(col) is not None

    def has_table(table: str) -> bool:
        return table in ctes or table in catalog.tables
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind != "ident" or i in skip:
            i += 1
            continue
        upper = t.value.upper()

        if upper in _KEYWORDS:
            i += 1
            continue

        # 输出别名（SELECT ... AS x 里的 x、ORDER BY x 里的 x）
        # 不是列，也不能拿去当列用 —— 直接跳过
        if t.value in aliases:
            i += 1
            continue

        # 函数调用：后面紧跟 "("
        if i + 1 < len(tokens) and tokens[i + 1].value == "(":
            if upper not in ALLOWED_FUNCTIONS:
                raise GuardError(
                    f"函数 {t.value} 不在允许列表里。可用："
                    + ", ".join(sorted(ALLOWED_FUNCTIONS)),
                    f"位置 {t.pos} 调用了未授权函数")
            if upper not in report.functions_used:
                report.functions_used.append(upper)
            i += 1
            continue

        # 限定列 t.col / t.*
        if (i + 1 < len(tokens) and tokens[i + 1].value == "."):
            qualifier = t.value
            if qualifier not in resolved:
                raise GuardError(
                    f"{qualifier!r} 不是这条 SQL 里声明过的表或别名",
                    f"位置 {t.pos} 限定词 {qualifier} 不在 {sorted(resolved)}")
            real = resolved[qualifier]
            col_tok = tokens[i + 2] if i + 2 < len(tokens) else None
            # t.* ：* 是运算符 token，不是 ident
            if col_tok is not None and col_tok.kind == "op" \
                    and col_tok.value == "*":
                i += 3
                continue
            if col_tok is None or col_tok.kind != "ident":
                raise GuardError(f"{qualifier}. 后面必须是列名")
            if col_tok.value == "*":
                i += 3
                continue
            if real in ctes:
                if col_tok.value not in ctes[real]:
                    raise GuardError(
                        f"CTE {real} 没有输出列 {col_tok.value!r}",
                        f"位置 {col_tok.pos} 幻觉列 {real}.{col_tok.value}")
                report.columns_used.append(f"{real}.{col_tok.value}")
                i += 3
                continue
            table = catalog.table(real)
            if table.column(col_tok.value) is None:
                raise GuardError(
                    f"表 {real} 里没有列 {col_tok.value!r}。"
                    f"它有：{', '.join(table.column_names)}",
                    f"位置 {col_tok.pos} 幻觉列 {real}.{col_tok.value}")
            report.columns_used.append(f"{real}.{col_tok.value}")
            i += 3
            continue

        # 裸列名：必须能在已用表里唯一定位（歧义要报出来，不能猜）
        hits_real = [tn for tn in used_tables
                     if tn not in ctes and has_column(tn, t.value)]
        hits_cte = [tn for tn in used_tables
                    if tn in ctes and has_column(tn, t.value)]
        # 真实表优先：CTE 的输出列是从它的 SELECT 列表猜出来的，
        # 拿它去制造歧义会让内层查询被外层定义误伤
        hits = hits_real or hits_cte
        if not hits:
            raise GuardError(
                f"列 {t.value!r} 在用到的表（{'、'.join(used_tables) or '无'}）"
                "里都不存在",
                f"位置 {t.pos} 未知列 {t.value}")
        if len(hits) > 1:
            raise GuardError(
                f"列 {t.value!r} 同时存在于 {'、'.join(hits)}，"
                f"请写成 表名.{t.value} 消除歧义",
                f"位置 {t.pos} 歧义列 {t.value}")
        report.columns_used.append(f"{hits[0]}.{t.value}")
        i += 1


def _enforce_limit(sql: str, tokens: list[Token], cap: int,
                   report: GuardReport) -> str:
    """没有 LIMIT 补一个；LIMIT 超上限压到上限。

    为什么不依赖 SQLite 的 `sqlite3` 游标 rowcount：
    那是"拉回来之后数"，一条没有 LIMIT 的 join 大表照样先把内存吃满。
    在 SQL 文本上限制才是源头治理。
    """
    # 找**最外层**的 LIMIT（只在括号深度 0 处找）
    depth = 0
    limit_idx = -1
    for i, t in enumerate(tokens):
        if t.value == "(":
            depth += 1
        elif t.value == ")":
            depth -= 1
        elif depth == 0 and t.is_kw("LIMIT"):
            limit_idx = i
    report.has_limit = limit_idx >= 0

    if limit_idx >= 0:
        num = tokens[limit_idx + 1] if limit_idx + 1 < len(tokens) else None
        if num is None or num.kind != "number":
            raise GuardError("LIMIT 后面必须是数字", str(num))
        if float(num.value) > cap:
            report.limit_clamped = True
            report.notes.append(
                f"LIMIT {num.value} 超过上限，已压到 {cap}")
            # 用正则替换最外层那个 LIMIT 数字，避免重排 token
            head = sql[:num.pos]
            tail = sql[num.pos + len(num.value):]
            return head + str(cap) + tail
        return sql

    report.notes.append(f"原 SQL 没有 LIMIT，已自动补 LIMIT {cap}")
    stripped = sql.rstrip().rstrip(";")
    return f"{stripped} LIMIT {cap}"


def validate(sql: str, catalog: Catalog, *, row_cap: int = 200
             ) -> GuardReport:
    """完整校验。返回 GuardReport 或抛 GuardError。"""
    if not sql or not sql.strip():
        raise GuardError("SQL 是空的")
    if len(sql) > MAX_SQL_LENGTH:
        raise GuardError(
            f"SQL 超过 {MAX_SQL_LENGTH} 字符 —— 查询逻辑这么长"
            "应该拆成多次查询，而不是一条巨型语句")
    if "\x00" in sql:
        raise GuardError("SQL 里有 NUL 字符")

    cleaned = strip_comments(sql)
    tokens = tokenize(cleaned)

    # 只允许一条语句：分号只能出现在结尾（或者干脆没有）
    semis = [t for t in tokens if t.value == ";"]
    if len(semis) > 1 or (len(semis) == 1 and tokens[-1].value != ";"):
        raise GuardError(
            "只允许一条查询语句 —— 多条语句是写操作最常见的入口")

    # 语句头
    head = next((t for t in tokens if t.value != ";"), None)
    if head is None or head.kind != "ident":
        raise GuardError("看不懂这条 SQL：没有有效的语句头")
    if head.value.upper() not in ALLOWED_HEADS:
        raise GuardError(
            f"只允许 SELECT / WITH 查询，收到 {head.value.upper()}",
            f"语句头 {head.value} 被拒绝")

    # 黑名单词（注释剥掉之后再看，避免 "/* DROP */" 误伤 ——
    # 也避免 "-- INSERT" 这种把写操作藏在注释里试探的写法漏过去）
    for t in tokens:
        if t.kind == "ident" and t.value.upper() in FORBIDDEN_WORDS:
            raise GuardError(
                f"SQL 里有不允许的词 {t.value.upper()} —— "
                "这里只能做只读查询",
                f"位置 {t.pos} 命中黑名单 {t.value.upper()}")
        # 关键字当标识符的伪装写法：quoted identifier 里藏着黑名单词
        # tokenize 对 quoted ident 也给 kind="ident"，这里一并拦住
    for t in tokens:
        if t.kind == "ident" and t.value.upper() in _KEYWORDS \
                and not t.is_kw("SELECT", "WITH"):
            # 关键字正常放行（上面已经处理过语句头）
            pass

    ctes, cte_skip = _collect_ctes(tokens)
    resolved, used, skip = _find_table_refs(tokens, catalog, ctes)
    skip |= cte_skip
    # @derived* 是匿名派生表，不是真实表名，不该出现在对外报告里
    real_used = [u for u in used if not u.startswith("@")]
    if not resolved:
        raise GuardError("没有找到 FROM/JOIN —— 不允许无表查询（如 SELECT 1+1）"
                         "，这里只回答数据问题")

    report = GuardReport(sql="", tables_used=real_used)
    aliases = _collect_aliases(tokens)
    _validate_columns(tokens, catalog, resolved, report, aliases, skip,
                      ctes)

    final = _enforce_limit(cleaned, tokens, row_cap, report)
    report.sql = final.rstrip(";") + ";"
    return report
