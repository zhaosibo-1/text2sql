"""表结构目录：Text2SQL 的"地图"。

LLM 生成 SQL 最常见的失败不是语法错，而是**编造不存在的列**
（幻觉列名在 SQLite 里只会报错，但在某些数据库里会静默返回错误结果）。
所以这里的第一个职责是把「数据库里到底有什么」说清楚，
第二个职责是把「用户嘴里的说法」对齐到「数据库里的真名」——
用户说"订单金额"，schema 里叫 ``total_amount``；
说"客户"，表叫 ``customers``。

名称解析刻意做成**可解释的**：每次解析都留下「为什么这么对齐」的依据，
而不是返回一个黑盒结果。生成错了要能追责到是别名缺失还是歧义。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


class SchemaError(ValueError):
    """schema 定义不合法。"""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    #: 人类可读说明，会进 LLM 的 schema 提示 —— 对生成质量影响极大
    comment: str = ""
    #: 主键 / 外键，生成 JOIN 条件时要用
    primary_key: bool = False
    references: str | None = None      # "orders.customer_id"
    #: 中文别名。"金额"、"总价" 都能指到 total_amount
    aliases: tuple[str, ...] = ()

    def describe(self) -> dict:
        return {"name": self.name, "type": self.type, "comment": self.comment,
                "primary_key": self.primary_key, "references": self.references,
                "aliases": list(self.aliases)}


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    comment: str = ""
    aliases: tuple[str, ...] = ()      # "订单表"、"orders"

    def column(self, name: str) -> Column | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def describe(self) -> dict:
        return {"name": self.name, "comment": self.comment,
                "aliases": list(self.aliases),
                "columns": [c.describe() for c in self.columns]}


@dataclass
class Resolved:
    """一次名称解析的结果与依据。"""

    kind: str                  # "table" | "column"
    table: str
    column: str | None
    score: float
    #: 命中途径：exact / alias / fuzzy / foreign
    via: str


class Catalog:
    """一组表的结构目录。"""

    def __init__(self, tables: list[Table]) -> None:
        self.tables: dict[str, Table] = {}
        for t in tables:
            if t.name in self.tables:
                raise SchemaError(f"表 {t.name!r} 重复定义")
            names = {c.name for c in t.columns}
            if not names:
                raise SchemaError(f"表 {t.name!r} 没有任何列")
            for c in t.columns:
                if c.references and "." not in c.references:
                    raise SchemaError(
                        f"{t.name}.{c.name} 的外键引用 {c.references!r} "
                        "必须写成 表名.列名"
                    )
            self.tables[t.name] = t
        self._validate_references()
        self._build_alias_index()

    def _validate_references(self) -> None:
        for t in self.tables.values():
            for c in t.columns:
                if not c.references:
                    continue
                ref_table, ref_col = c.references.split(".", 1)
                rt = self.tables.get(ref_table)
                if rt is None:
                    raise SchemaError(
                        f"{t.name}.{c.name} 引用的表 {ref_table!r} 不存在"
                    )
                if rt.column(ref_col) is None:
                    raise SchemaError(
                        f"{t.name}.{c.name} 引用的列 {c.references!r} 不存在"
                    )

    def _build_alias_index(self) -> None:
        """别名 → 候选列表。一个别名指向多个表 = 歧义，解析时必须报出来。"""
        self._table_alias: dict[str, list[str]] = {}
        self._column_alias: dict[str, list[tuple[str, str]]] = {}
        for t in self.tables.values():
            for key in (t.name, *t.aliases):
                self._table_alias.setdefault(key.lower(), []).append(t.name)
            for c in t.columns:
                for key in (c.name, *c.aliases):
                    self._column_alias.setdefault(
                        key.lower(), []).append((t.name, c.name))

    # -- 查询 ------------------------------------------------------------

    def table(self, name: str) -> Table:
        t = self.tables.get(name)
        if t is None:
            raise SchemaError(f"表 {name!r} 不存在；现有：{', '.join(self.tables)}")
        return t

    def resolve_table(self, mention: str) -> list[Resolved]:
        """把用户的说法对齐到表。可能多个候选 —— 歧义要交出去，不要擅自猜。"""
        key = mention.strip().lower().rstrip("表")
        out: list[Resolved] = []
        for alias, tables in self._table_alias.items():
            if alias == key or alias == mention.strip().lower():
                for t in tables:
                    out.append(Resolved("table", t, None, 1.0,
                                        "exact" if alias == t else "alias"))
        if out:
            return out
        # 模糊兜底：编辑距离太高就是真不认识，别硬凑
        for name in self.tables:
            ratio = difflib.SequenceMatcher(
                None, key, name.lower().rstrip("s")).ratio()
            if ratio >= 0.72:
                out.append(Resolved("table", name, None, round(ratio, 3),
                                    "fuzzy"))
        return sorted(out, key=lambda r: -r.score)

    def resolve_column(self, mention: str,
                       table_hint: str | None = None) -> list[Resolved]:
        """把列的说法对齐到（表, 列）。``table_hint`` 用来消歧。"""
        key = mention.strip().lower()
        out: list[Resolved] = []
        for alias, pairs in self._column_alias.items():
            if alias != key:
                continue
            for t, c in pairs:
                out.append(Resolved(
                    "column", t, c, 1.0,
                    "exact" if alias == c else "alias"))
        if not out:
            # 模糊兜底，但阈值比表严格 —— 列名编错的代价更高
            for t in self.tables.values():
                for c in t.columns:
                    ratio = difflib.SequenceMatcher(
                        None, key, c.name.lower()).ratio()
                    if ratio >= 0.8:
                        out.append(Resolved("column", t.name, c.name,
                                            round(ratio, 3), "fuzzy"))
        if table_hint and len(out) > 1:
            preferred = [r for r in out if r.table == table_hint]
            if preferred:
                return preferred
        return sorted(out, key=lambda r: -r.score)

    # -- 提示词 -----------------------------------------------------------

    def to_prompt(self, only: list[str] | None = None) -> str:
        """给 LLM 的 schema 描述。

        刻意包含别名和说明：生成质量的最大杠杆不是模型，
        是「模型看不看得得到正确的地图」。
        """
        tables = [self.tables[n] for n in (only or list(self.tables))]
        blocks = []
        for t in tables:
            lines = [f"表 {t.name} —— {t.comment}"]
            if t.aliases:
                lines.append(f"  （也叫：{'、'.join(t.aliases)}）")
            for c in t.columns:
                mark = " 主键" if c.primary_key else ""
                ref = f" 外键→{c.references}" if c.references else ""
                alias = (f"（别名：{'、'.join(c.aliases)}）"
                         if c.aliases else "")
                lines.append(
                    f"  {c.name} {c.type}{mark}{ref} {c.comment}{alias}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def join_path(self, a: str, b: str) -> list[tuple[str, str, str]] | None:
        """找出 a、b 之间基于外键的连接条件（只走一层，够用且诚实）。"""
        if a == b:
            return None
        for t in self.tables.values():
            for c in t.columns:
                if not c.references:
                    continue
                rt, _rc = c.references.split(".", 1)
                if {a, b} == {t.name, rt}:
                    return [(t.name, c.name, rt)]
        return None

    def describe(self) -> dict:
        return {"tables": [t.describe() for t in self.tables.values()],
                "table_count": len(self.tables)}


def load_demo_catalog() -> Catalog:
    """演示库的结构目录。别名来自真实用户会怎么说。"""
    return Catalog([
        Table(
            name="customers", comment="客户",
            aliases=("客户", "客户信息", "会员", "用户"),
            columns=(
                Column("id", "INTEGER", "客户ID", primary_key=True,
                       aliases=("客户ID", "客户编号")),
                Column("name", "TEXT", "客户姓名", aliases=("姓名", "名字")),
                Column("city", "TEXT", "所在城市", aliases=("城市", "地区")),
                Column("signup_date", "TEXT", "注册日期 YYYY-MM-DD",
                       aliases=("注册日期", "注册时间")),
            ),
        ),
        Table(
            name="products", comment="商品",
            aliases=("商品", "商品信息", "货品"),
            columns=(
                Column("id", "INTEGER", "商品ID", primary_key=True,
                       aliases=("商品ID", "商品编号")),
                Column("name", "TEXT", "商品名称", aliases=("商品名", "品名")),
                Column("category", "TEXT", "类目", aliases=("类目", "分类")),
                Column("price", "REAL", "单价（元）", aliases=("单价", "价格", "售价")),
                Column("stock", "INTEGER", "库存数量", aliases=("库存", "存货")),
            ),
        ),
        Table(
            name="orders", comment="订单（主表）",
            aliases=("订单", "订单信息", "购买记录"),
            columns=(
                Column("id", "INTEGER", "订单ID", primary_key=True,
                       aliases=("订单ID", "订单编号", "订单号")),
                Column("customer_id", "INTEGER", "下单客户",
                       references="customers.id", aliases=("客户", "下单人")),
                Column("status", "TEXT", "订单状态：paid/shipped/done/cancelled",
                       aliases=("状态", "订单状态")),
                Column("created_at", "TEXT", "下单时间 YYYY-MM-DD HH:MM",
                       aliases=("下单时间", "创建时间", "时间")),
                Column("total_amount", "REAL", "订单总金额（元）",
                       aliases=("金额", "总价", "订单金额", "总额")),
            ),
        ),
        Table(
            name="order_items", comment="订单明细（一个订单多行商品）",
            aliases=("订单明细", "明细", "订单项"),
            columns=(
                Column("id", "INTEGER", "明细ID", primary_key=True),
                Column("order_id", "INTEGER", "所属订单",
                       references="orders.id", aliases=("订单",)),
                Column("product_id", "INTEGER", "商品",
                       references="products.id", aliases=("商品",)),
                Column("quantity", "INTEGER", "购买数量",
                       aliases=("数量", "件数")),
                Column("unit_price", "REAL", "成交单价（元）",
                       aliases=("成交价", "单价")),
            ),
        ),
    ])
