"""SQL 生成器：两种实现，同一个接口。

**为什么规则版还要存在** —— 项目里已经有 LLM 了，为什么还写规则：

1. **可测**。LLM 的输出每次都不一样，没法在 CI 里断言
   「这个问题就该生成这条 SQL」；规则版可以。
2. **兜底**。API 挂了、额度用完了、内网部署没有外网 ——
   安全层和执行层照常工作，只是生成质量降级。
3. **对照**。评审 Text2SQL 系统时最有说服力的问题是
   "你的 LLM 到底带来了什么？" 有一个确定性的基线才答得上来。

规则版刻意**承认自己的局限**：每个问题都带 ``confidence`` 和
``matched``（命中了哪些模式）。低置信度就明说，不装懂 ——
宁可让上层提示"换个问法"，也不生成一条看似合理的错误 SQL。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .schema import Catalog


class LLMNotConfigured(RuntimeError):
    """没有配置 LLM —— 调用方应降级到规则生成器。"""


@dataclass
class GenerationResult:
    sql: str
    method: str                    # "rule" | "llm"
    confidence: float              # 0~1，规则版的自评
    matched: list[str] = field(default_factory=list)
    explanation: str = ""
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {"sql": self.sql, "method": self.method,
                "confidence": self.confidence, "matched": self.matched,
                "explanation": self.explanation, "warnings": self.warnings}


class RuleBasedGenerator:
    """面向演示 schema 的中文问题 → SQL。

    实现方式是**模式匹配 + 组装**，不是通用 NL2SQL：
    覆盖计数 / 聚合 / 分组 / 排序 TOP-N / 过滤 / 连接这些主干形态，
    不覆盖的形态会返回低置信度而不是硬凑。
    """

    #: 数字量词前的"前N个"约束
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        # 预编译：把每个表和列的所有别名都变成可匹配的正则片段
        self._table_terms: dict[str, str] = self._table_terms()

    # -- 术语表 -----------------------------------------------------------

    def _table_terms(self) -> dict[str, str]:
        terms: dict[str, str] = {}
        for t in self.catalog.tables.values():
            for key in (t.name, *t.aliases):
                terms[key] = t.name
        # 按长度倒序：先匹配长词（"订单明细" 优先于 "订单"）
        return dict(sorted(terms.items(), key=lambda kv: -len(kv[0])))

    def detect_table(self, question: str) -> tuple[str | None, str]:
        """找出问题指向的表。返回 (表名, 命中词)。

        多个表被提到时取**最后一次出现**的那个：
        中文偏正结构「A 的 B」里，B（中心词）才是要查的对象 ——
        「上海客户的订单」查订单，「订单的客户」查客户。
        取第一次出现会做出相反的判断。
        """
        best: tuple[int, int, str, str] = (-1, 0, "", "")  # pos, len, term, real
        for term, real in self._table_terms.items():
            pos = question.rfind(term)
            if pos >= 0 and (pos, len(term)) > (best[0], best[1]):
                best = (pos, len(term), term, real)
        if best[2] == "":
            return None, ""
        return best[3], best[2]

    # -- 主入口 -----------------------------------------------------------

    def generate(self, question: str) -> GenerationResult:
        q = question.strip()
        matched: list[str] = []
        table, table_hit = self.detect_table(q)

        # ---- 分组统计：每个X的Y ------------------------------------
        group = self._detect_group(q)
        if group:
            return self._gen_group(q, group, table, matched)

        # ---- TOP-N：金额最高的前5个 --------------------------------
        topn = self._detect_topn(q)
        if topn:
            return self._gen_topn(q, topn, table, table_hit, matched)

        # ---- 标量聚合：平均/最大/最小/总 ----------------------------
        agg = self._detect_scalar_agg(q)
        if agg:
            return self._gen_scalar_agg(q, agg, table, matched)

        # ---- 计数：有多少 / 几个 ------------------------------------
        if re.search(r"多少|几个|几名|几条|几笔|数量", q):
            return self._gen_count(q, table, table_hit, matched)

        # ---- 列表 / 查详情 ------------------------------------------
        return self._gen_select(q, table, table_hit, matched)

    # -- 模式识别 ---------------------------------------------------------

    def _detect_group(self, q: str) -> tuple[str, str] | None:
        """「每个城市的客户数」「按类目统计平均价格」→ (表, 分组列)。"""
        m = re.search(r"每个|各个|各|按|分别", q)
        if not m:
            return None
        after = q[m.end():]
        for col_name in ("城市", "类目", "分类", "状态", "客户"):
            if col_name in after or col_name in q:
                col = self._col_for(col_name)
                if col:
                    # _col_for 返回 (表, 列)，直接返回即可 ——
                    # 曾经在这里又包了一层 tuple，SQL 里出现了
                    # SELECT ('customers','city').城市 这种鬼东西
                    return col
        return None

    def _col_for(self, mention: str) -> tuple[str, str] | None:
        """列的说法 → (表, 列)。"""
        rs = self.catalog.resolve_column(mention)
        return (rs[0].table, rs[0].column) if rs else None

    def _detect_topn(self, q: str) -> tuple[str, str, str, int] | None:
        """返回 (排序列, 方向, 聚合或None, N)。"""
        m = re.search(r"前\s*(\d+)\s*(?:个|名|条|笔)|最(高|大|贵|多|低|小|便宜)"
                      r"[的]?\s*(?:前\s*)?(\d*)\s*(?:个|名|条|笔)?", q)
        n = 5
        if m:
            if m.group(1):
                n = int(m.group(1))
            elif m.group(3):
                n = int(m.group(3)) or 5
        else:
            m2 = re.search(r"最(高|大|贵|多|低|小|便宜)", q)
            if not m2:
                return None
        desc = not re.search(r"最(低|小|便宜)|最少", q)
        # 排序列：金额/总价/价格 → total_amount/price
        col = None
        for word in ("金额", "总价", "总额", "价格", "单价", "库存"):
            c = self._col_for(word)
            if c and word in q:
                col = c
                break
        if col is None:
            col = ("orders", "total_amount")
        # "卖得最多" 类：按数量聚合
        agg = None
        if re.search(r"卖得最多|销量|卖出|购买数量|件数", q):
            agg = "SUM"
        return col, ("DESC" if desc else "ASC"), agg, n

    def _detect_scalar_agg(self, q: str) -> tuple[str, tuple[str, str]] | None:
        m = re.search(r"平均|最高|最大|最低|最小|总共|一共|总额|总金额|总和|合计",
                      q)
        if not m:
            return None
        fn = {"平均": "AVG", "最高": "MAX", "最大": "MAX", "最低": "MIN",
              "最小": "MIN", "总共": "SUM", "总额": "SUM", "总金额": "SUM",
              "总和": "SUM", "合计": "SUM"}.get(m.group(0), None)
        if fn is None:
            return None
        col = None
        for word in ("金额", "总价", "总额", "价格", "单价", "库存", "数量"):
            if word in q:
                c = self._col_for(word)
                if c:
                    col = c
                    break
        if col is None:
            col = ("orders", "total_amount")
        return fn, col

    # -- SQL 组装 ---------------------------------------------------------

    def _filters(self, q: str, table: str | None
                 ) -> tuple[list[str], list[str], float]:
        """从问题里抽 WHERE 条件。返回 (条件SQL, 警告, 置信度加成)。"""
        conds: list[str] = []
        warns: list[str] = []
        bonus = 0.0

        # 城市
        for city in ["北京", "上海", "广州", "深圳", "杭州", "成都",
                     "武汉", "南京"]:
            if city in q:
                conds.append(f"customers.city = '{city}'")
                bonus += 0.15
                break
        # 类目
        for cat in ["数码", "家电", "图书", "服饰", "食品"]:
            if cat in q:
                conds.append(f"products.category = '{cat}'")
                bonus += 0.15
                break
        # 订单状态
        sm = re.search(r"(?:状态|订单状态)(?:是|为|等于)?\s*[「']?(\w+)[」']?", q)
        if sm:
            conds.append(f"orders.status = '{sm.group(1)}'")
            bonus += 0.15
        elif "已取消" in q or "取消的" in q:
            conds.append("orders.status = 'cancelled'")
            bonus += 0.15
        elif "已完成" in q:
            conds.append("orders.status = 'done'")
            bonus += 0.15
        # 金额/价格阈值
        pm = re.search(r"(金额|总价|价格|单价)(?:超过|大于|高于|不低于|>=?)\s*"
                       r"(\d+(?:\.\d+)?)", q)
        if pm:
            op = ">=" if ("不低于" in q or ">=" in q) else ">"
            conds.append(f"{pm.group(1)}相关列 {op} {pm.group(2)}")  # 占位，下面替换
            col = self._col_for(pm.group(1))
            if col:
                conds[-1] = f"{col[0]}.{col[1]} {op} {pm.group(2)}"
                bonus += 0.15
        # 时间（年份）
        ym = re.search(r"(20\d{2})\s*年\s*(\d{1,2})?\s*月?", q)
        if ym and table in ("orders", "order_items", None):
            year, month = ym.group(1), ym.group(2)
            if month:
                conds.append(
                    f"orders.created_at >= '{year}-{int(month):02d}-01' "
                    f"AND orders.created_at < "
                    f"'{year}-{int(month) + 1:02d}-01'")
            else:
                conds.append(
                    f"orders.created_at >= '{year}-01-01' "
                    f"AND orders.created_at < '{int(year) + 1}-01-01'")
            bonus += 0.15
        return conds, warns, bonus

    def _joins_needed(self, conds: list[str], table: str | None,
                      agg_col: tuple[str, str] | None) -> list[str]:
        """按条件引用到的表决定要 JOIN 什么，并用外键写出 ON。"""
        need: set[str] = set()
        for c in conds:
            for tn in ("customers", "products", "orders"):
                if c.startswith(tn + "."):
                    need.add(tn)
        if agg_col:
            need.add(agg_col[0])
        if table:
            need.add(table)
        # 明细 → 订单 → 客户 的连接链
        joins: list[str] = []
        base = "orders" if "orders" in need or need - {"order_items"} else "orders"
        if "order_items" in need:
            joins.append("order_items")
        if "orders" in need or joins:
            if "orders" not in joins:
                joins.append("orders")
        if "customers" in need and "customers" not in joins:
            joins.append("customers")
        if "products" in need and "products" not in joins:
            joins.append("products")
        # 排序：先到先连接，保持确定性
        return joins

    def _from_clause(self, joins: list[str]) -> str:
        if not joins:
            return "FROM orders"
        order = ["orders", "order_items", "customers", "products"]
        ordered = [t for t in order if t in joins]
        base = ordered[0]
        clauses = [f"FROM {base}"]
        for t in ordered[1:]:
            if t == "order_items":
                clauses.append("JOIN order_items ON order_items.order_id = orders.id")
            elif t == "customers":
                clauses.append("JOIN customers ON customers.id = orders.customer_id")
            elif t == "products":
                clauses.append(
                    "JOIN products ON products.id = order_items.product_id"
                    if "order_items" in ordered else
                    "JOIN products ON 1=0")
        return " ".join(clauses)

    def _gen_count(self, q: str, table: str | None, table_hit: str,
                   matched: list[str]) -> GenerationResult:
        matched.append("count")
        joins = self._joins_needed(*self._filters(q, table)[:2], None)
        # 计数的目标表优先看命中词
        target = table or "orders"
        if table_hit in ("客户", "客户信息", "会员", "用户", "customers"):
            target = "customers"
        elif table_hit in ("商品", "商品信息", "货品", "products"):
            target = "products"
        joins = self._joins_needed(self._filters(q, target)[0], target, None)
        conds, _, bonus = self._filters(q, target)
        # 去掉与目标表无关却带上的 JOIN
        joins = self._prune_joins(joins, conds, target)
        sql = f"SELECT COUNT(*) AS cnt FROM {target}"
        # 目标表为基表时，JOIN 顺序要重排
        join_sql = self._rebase_joins(target, conds)
        sql = f"SELECT COUNT(*) AS cnt FROM {target} {join_sql}"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += ";"
        return GenerationResult(
            sql=sql, method="rule",
            confidence=min(0.85, 0.55 + bonus),
            matched=matched,
            explanation=f"识别为计数问题，目标表 {target}，"
                        f"{'带过滤条件' if conds else '无过滤条件'}")

    def _prune_joins(self, joins: list[str], conds: list[str],
                     target: str) -> list[str]:
        need = {target}
        for c in conds:
            for tn in ("customers", "products", "orders", "order_items"):
                if c.startswith(tn + "."):
                    need.add(tn)
        return [j for j in joins if j in need]

    def _rebase_joins(self, base: str, conds: list[str]) -> str:
        need: set[str] = {base}
        for c in conds:
            for tn in ("customers", "products", "orders", "order_items"):
                if c.startswith(tn + "."):
                    need.add(tn)
        parts: list[str] = []
        # 连接链：orders ← order_items / customers；order_items → products
        if "orders" in need and base != "orders" and base == "customers":
            parts.append("JOIN orders ON orders.customer_id = customers.id")
            need.add("orders")
        if "orders" in need and base != "orders" and base == "order_items":
            parts.append("JOIN orders ON orders.id = order_items.order_id")
            need.add("orders")
        if "customers" in need and "customers" not in (base,):
            if base == "orders":
                parts.append(
                    "JOIN customers ON customers.id = orders.customer_id")
        if "order_items" in need and "order_items" != base:
            parts.append(
                "JOIN order_items ON order_items.order_id = orders.id")
        if "products" in need and "products" != base:
            if "order_items" in need:
                parts.append(
                    "JOIN products ON products.id = order_items.product_id")
        return " ".join(parts)

    def _gen_group(self, q: str, group: tuple[str, str],
                   table: str | None, matched: list[str]) -> GenerationResult:
        matched.append("group")
        gtable, gcol = group   # group 是 (表, 列)
        # 统计什么？默认计数；问了"平均金额"就聚合
        fn, agg_col = "COUNT", None
        am = re.search(r"平均(\w*)|总(?:共|额|和)?(\w*)", q)
        if am and (am.group(1) or am.group(2)):
            word = am.group(1) or am.group(2) or "金额"
            c = self._col_for(word)
            if c:
                fn = "AVG" if am.group(1) else "SUM"
                agg_col = c
        conds, _, bonus = self._filters(q, gtable)
        join_sql = self._rebase_joins(gtable, conds)
        if agg_col and agg_col[0] != gtable:
            # 聚合列在另一张表：把它也连接进来
            extra = self._rebase_joins(agg_col[0], conds)
            if agg_col[0] not in join_sql:
                join_sql += " " + extra
        value = (f"{fn}({agg_col[0]}.{agg_col[1]}) AS value"
                 if agg_col else "COUNT(*) AS value")
        grp_expr = f"{gtable}.{gcol}"
        sql = f"SELECT {grp_expr} AS grp, {value} FROM {gtable} {join_sql}"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" GROUP BY {grp_expr} ORDER BY value DESC;"
        return GenerationResult(
            sql=sql, method="rule", confidence=min(0.85, 0.6 + bonus),
            matched=matched,
            explanation=f"识别为分组统计：按 {gtable}.{gcol} 分组，{fn} 聚合")

    def _gen_topn(self, q: str, topn: tuple, table: str | None,
                  table_hit: str, matched: list[str]) -> GenerationResult:
        matched.append("topn")
        (col_table, col_name), direction, agg, n = topn
        conds, _, bonus = self._filters(q, col_table)
        target = table or col_table
        if table_hit in ("客户", "会员", "用户"):
            target = "customers"
        elif table_hit in ("商品", "货品"):
            target = "products"
        if agg == "SUM" and target != "order_items":
            # 销量要按明细聚合
            sql = (f"SELECT products.name AS name, "
                   f"SUM(order_items.quantity) AS total_qty "
                   f"FROM products "
                   f"JOIN order_items ON order_items.product_id = products.id "
                   f"JOIN orders ON orders.id = order_items.order_id")
            if conds:
                sql += " WHERE " + " AND ".join(
                    c for c in conds if not c.startswith("products."))
            sql += (f" GROUP BY products.name "
                    f"ORDER BY total_qty {direction} LIMIT {n};")
            return GenerationResult(
                sql=sql, method="rule", confidence=min(0.85, 0.6 + bonus),
                matched=matched, explanation=f"按销量聚合的 TOP-{n}")
        select = f"{target}.*"
        sql = f"SELECT {select} FROM {target} "
        sql += self._rebase_joins(target, conds)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" ORDER BY {col_table}.{col_name} {direction} LIMIT {n};"
        return GenerationResult(
            sql=sql, method="rule", confidence=min(0.85, 0.6 + bonus),
            matched=matched,
            explanation=f"识别为 TOP-{n} 查询，按 {col_table}.{col_name} "
                        f"{'降序' if direction == 'DESC' else '升序'}")

    def _gen_scalar_agg(self, q: str, agg: tuple, table: str | None,
                        matched: list[str]) -> GenerationResult:
        matched.append("agg")
        fn, (col_table, col_name) = agg
        conds, _, bonus = self._filters(q, col_table)
        sql = f"SELECT {fn}({col_table}.{col_name}) AS value FROM {col_table} "
        sql += self._rebase_joins(col_table, conds)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += ";"
        return GenerationResult(
            sql=sql, method="rule", confidence=min(0.85, 0.6 + bonus),
            matched=matched,
            explanation=f"识别为聚合查询：{fn}({col_table}.{col_name})")

    def _gen_select(self, q: str, table: str | None, table_hit: str,
                    matched: list[str]) -> GenerationResult:
        matched.append("select")
        target = table or "orders"
        if table_hit in ("客户", "会员", "用户"):
            target = "customers"
        elif table_hit in ("商品", "货品"):
            target = "products"
        conds, warns, bonus = self._filters(q, target)
        joins = self._rebase_joins(target, conds)
        sql = f"SELECT {target}.* FROM {target} {joins}".strip()
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY " + (
            "orders.created_at DESC" if target == "orders"
            else f"{target}.id ASC") + ";"
        conf = 0.5 + bonus if conds else 0.35
        if not conds:
            warns.append("没有识别出过滤条件，将返回整表的前若干行")
        return GenerationResult(
            sql=sql, method="rule", confidence=min(0.8, conf),
            matched=matched,
            explanation=f"识别为列表查询，目标表 {target}",
            warnings=warns)


class LLMGenerator:
    """OpenAI 兼容接口的生成器。没配 key 就明确说没配，不装。"""

    def __init__(self, catalog: Catalog, *, model: str | None = None) -> None:
        self.catalog = catalog
        self.model = model or os.environ.get("TEXT2SQL_MODEL", "gpt-4o-mini")
        self.base_url = os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.api_key = os.environ.get("OPENAI_API_KEY", "")
        self.timeout = float(os.environ.get("TEXT2SQL_TIMEOUT", "30"))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    SYSTEM_PROMPT = (
        "你是 SQL 生成器。根据给出的表结构，把用户的中文问题转成一条 "
        "SQLite 查询。硬性规则：\n"
        "1. 只输出一条 SELECT 语句，不要解释，不要 markdown 代码块；\n"
        "2. 只允许使用给出的表和列，禁止编造；\n"
        "3. 带上 LIMIT，不得超过 200；\n"
        "4. 字符串值直接写进 SQL（这里没有绑定参数机制）。\n")

    def generate(self, question: str) -> GenerationResult:
        if not self.configured:
            raise LLMNotConfigured(
                "没有配置 OPENAI_API_KEY —— 设置后再用 LLM 生成器，"
                "或降级到规则生成器")
        prompt = self.catalog.to_prompt()
        body = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"表结构：\n{prompt}\n\n问题：{question}"},
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMNotConfigured(
                f"LLM 接口返回 {exc.code}：{exc.read().decode('utf-8', 'replace')[:200]}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMNotConfigured(f"LLM 接口不可达：{exc}") from exc
        raw = data["choices"][0]["message"]["content"].strip()
        # 剥掉可能被加上的 markdown 围栏
        raw = re.sub(r"^```(?:sql)?\s*|\s*```$", "", raw, flags=re.I).strip()
        return GenerationResult(
            sql=raw, method="llm", confidence=0.7,
            matched=["llm"],
            explanation=f"由 {self.model} 生成（温度 0），"
                        "仍需通过同一道安全闸门")


def make_generator(catalog: Catalog, prefer: str = "auto") -> tuple[Any, str]:
    """按配置挑生成器。返回 (生成器, 实际用了哪个)。"""
    llm = LLMGenerator(catalog)
    if prefer in ("auto", "llm") and llm.configured:
        return llm, "llm"
    return RuleBasedGenerator(catalog), "rule"
