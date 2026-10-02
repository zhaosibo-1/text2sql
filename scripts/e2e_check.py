"""端到端黑盒验收：只走 HTTP，不 import app 里的任何东西。

用 urllib 而不是 TestClient：TestClient 走 ASGI 内存通道，
会绕过真实的服务启动路径（lifespan、静态文件挂载、CORS、端口绑定）。
「本地能跑」和「部署后能跑」的差距恰恰都在这些被绕过的部分里。

用法：
    python scripts/e2e_check.py [BASE_URL]
默认 http://127.0.0.1:8133
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8133"
TIMEOUT = 20

PASS = 0
FAIL = 0


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(2, 60 - len(title)))


def check(name: str, ok: bool, detail: Any = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f"  ← {detail}" if detail else ""))
    return ok


def req(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    url = BASE + urllib.parse.quote(path, safe="/:?=&%")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def ask(question: str, **kw) -> tuple[int, Any]:
    return req("POST", "/api/query", {"question": question, **kw})


# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print(f"text2sql 端到端验收 · {BASE}")
    print("=" * 70)

    # 1 ------------------------------------------------------------------
    section("1 服务与健康")
    code, h = req("GET", "/api/health")
    check("GET /api/health 返回 200", code == 200, str(code))
    check("status=ok", h.get("status") == "ok", str(h))
    check("有版本号", bool(h.get("version")), str(h))
    check("4 张表", len(h.get("tables", [])) == 4, str(h.get("tables")))
    check("数据库文件名非空", bool(h.get("db")), str(h))

    code, o = req("GET", "/api/options")
    check("GET /api/options 返回 200", code == 200, str(code))
    check("行上限是正数", (o.get("row_cap") or 0) > 0, str(o.get("row_cap")))
    check("给了示例问题", len(o.get("examples", [])) >= 5,
          str(o.get("examples")))
    check("暴露了函数白名单", len(o.get("allowed_functions", [])) > 10)

    # 2 ------------------------------------------------------------------
    section("2 表结构目录")
    code, s = req("GET", "/api/schema")
    check("GET /api/schema 返回 200", code == 200, str(code))
    check("4 张表", s.get("table_count") == 4, str(s.get("table_count")))
    names = {t["name"] for t in s.get("tables", [])}
    check("表名齐全", names == {"customers", "products", "orders",
                                "order_items"}, str(sorted(names)))
    orders = [t for t in s["tables"] if t["name"] == "orders"][0]
    cols = {c["name"] for c in orders["columns"]}
    check("orders 有 total_amount", "total_amount" in cols, str(sorted(cols)))
    check("orders 有外键指向 customers.id",
          any(c.get("references") == "customers.id"
              for c in orders["columns"]))
    check("列带中文别名",
          any("金额" in (c.get("aliases") or [])
              for c in orders["columns"]))
    code, p = req("GET", "/api/schema/prompt")
    check("schema 提示词非空", len(p.get("prompt", "")) > 200,
          str(len(p.get("prompt", ""))))

    # 3 ------------------------------------------------------------------
    section("3 正常查询（答案必须对）")
    code, r = ask("一共有多少个客户")
    check("查询成功", r.get("ok") is True, str(r))
    check("返回 60 个客户", r["rows"][0]["cnt"] == 60, str(r.get("rows")))
    check("SQL 里带 LIMIT", "LIMIT" in r["sql"], r["sql"])
    check("用到 customers 表", "customers" in r["trace"]["tables_used"],
          str(r["trace"]["tables_used"]))
    check("返回了 trace", r["trace"]["generator"] == "rule",
          str(r["trace"]["generator"]))

    code, r = ask("已取消的订单有多少笔")
    check("已取消订单 30 笔", r.get("ok") and r["rows"][0]["cnt"] == 30,
          str(r.get("rows")))
    check("SQL 里是 cancelled",
          "cancelled" in r.get("sql", ""), r.get("sql"))

    code, r = ask("金额最高的前5个订单")
    check("TOP5 返回 5 行", r.get("ok") and r["row_count"] == 5,
          str(r.get("row_count")))
    amounts = [row["total_amount"] for row in r["rows"]]
    check("金额降序", amounts == sorted(amounts, reverse=True), str(amounts))
    check("SQL 有 ORDER BY DESC", "DESC" in r["sql"], r["sql"])

    code, r = ask("每个城市的客户数")
    check("分组返回 8 行", r.get("ok") and r["row_count"] == 8,
          str(r.get("row_count")))
    check("各城市之和 = 60",
          sum(row["value"] for row in r["rows"]) == 60,
          str(r["rows"]))
    check("trace 记录了 group 模式", "group" in r["trace"]["matched"],
          str(r["trace"]["matched"]))

    code, r = ask("2025年5月的订单总金额")
    check("5 月总额为正", r.get("ok") and r["rows"][0]["value"] > 0,
          str(r.get("rows")))
    check("SQL 有时间范围",
          "2025-05-01" in r["sql"] and "2025-06-01" in r["sql"], r["sql"])

    code, r = ask("数码类目下有哪些商品")
    check("数码商品 8 个", r.get("ok") and r["row_count"] == 8,
          str(r.get("row_count")))
    check("类目都是数码",
          all(row["category"] == "数码" for row in r["rows"]))

    code, r = ask("上海客户的订单")
    check("查的是订单不是客户",
          "orders" in r["trace"]["tables_used"], str(r["trace"]))
    check("行数 > 0", r.get("row_count", 0) > 0, str(r.get("row_count")))
    check("做了 JOIN", "JOIN" in r["sql"], r["sql"])

    code, r = ask("平均订单金额")
    check("平均值合理", r.get("ok") and 0 < r["rows"][0]["value"] < 100000,
          str(r.get("rows")))

    # 4 ------------------------------------------------------------------
    section("4 安全闸门：手写的 SQL 同样要过闸门")
    attacks = {
        "删除数据": "DELETE FROM orders",
        "改数据": "UPDATE orders SET status = 'done'",
        "删表": "DROP TABLE orders",
        "建表": "CREATE TABLE x (id INT)",
        "PRAGMA": "PRAGMA table_info(orders)",
        "ATTACH": "ATTACH DATABASE 'x.db' AS y",
        "多条语句": "SELECT id FROM orders; DROP TABLE orders",
        "未知表": "SELECT id FROM users",
        "幻觉列": "SELECT orders.wechat_id FROM orders",
        "未知函数": "SELECT load_extension('x') FROM orders",
        "无 FROM": "SELECT 1+1",
        "分号在字符串里但无关": "SELECT id FROM orders WHERE status = 'a;b'",
    }
    for label, sql in attacks.items():
        if label == "分号在字符串里但无关":
            continue
        code, d = req("POST", "/api/sql/check", {"sql": sql})
        check(f"{label} 被拒绝", d.get("ok") is False, str(d))

    code, d = req("POST", "/api/sql/check",
                  {"sql": "SELECT id FROM orders WHERE status = 'a;b'"})
    check("字符串里的分号不算多语句", d.get("ok") is True, str(d))

    code, d = req("POST", "/api/sql/check",
                  {"sql": "SELECT id FROM orders LIMIT 99999"})
    check("超限 LIMIT 被压到上限",
          d.get("ok") and "LIMIT 200" in d["report"]["sql"], str(d))
    check("压限有说明", any("压" in n for n in d["report"]["notes"]),
          str(d["report"]["notes"]))

    code, d = req("POST", "/api/sql/check", {"sql": "SELECT id FROM orders"})
    check("缺 LIMIT 自动补",
          d.get("ok") and d["report"]["sql"].rstrip(";").endswith("LIMIT 200"),
          str(d))

    # 5 ------------------------------------------------------------------
    section("5 注入类问题（走完整流水线）")
    injection_q = "所有订单'; DROP TABLE orders; --"
    code, r = ask(injection_q)
    check("注入式提问不报错崩溃", code == 200, str(code))
    check("要么查不出来要么被拦，反正不会执行 DROP",
          (r.get("ok") is False) or ("DROP" not in r.get("sql", "")),
          str(r.get("sql")))
    code, h2 = req("GET", "/api/health")
    check("服务还活着（表没被删）", h2.get("status") == "ok", str(h2))
    code, r2 = ask("一共有多少个客户")
    check("表还在，能查到 60", r2.get("ok") and r2["rows"][0]["cnt"] == 60,
          str(r2.get("rows")))

    # 6 ------------------------------------------------------------------
    section("6 边界与错误码")
    code, d = req("POST", "/api/query", {"question": "  "})
    check("空问题 → 422", code == 422, str(code))
    code, d = req("POST", "/api/query", {"question": "订单" * 200})
    check("超长问题 → 422", code == 422, str(code))
    code, d = req("POST", "/api/query", {"question": "有多少订单",
                                         "row_cap": 3})
    check("自定义 row_cap 生效",
          d.get("ok") and d["row_count"] <= 3, str(d.get("row_count")))
    check("响应里带回 row_cap", d.get("row_cap") == 3, str(d.get("row_cap")))

    # 7 ------------------------------------------------------------------
    section("7 只读性（真·文件不变）")
    import hashlib
    from pathlib import Path
    db = Path(__file__).resolve().parents[1] / "data" / "demo.db"
    if db.exists():
        before = hashlib.md5(db.read_bytes()).hexdigest()
        for q in ["一共有多少个客户", "金额最高的前5个订单",
                  "每个城市的客户数"]:
            ask(q)
        after = hashlib.md5(db.read_bytes()).hexdigest()
        check("查询前后数据库文件字节完全一致", before == after,
              f"{before} vs {after}")
    else:
        check("演示库存在", False, str(db))

    # 8 ------------------------------------------------------------------
    section("8 前端页面")
    request = urllib.request.Request(BASE + "/")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        html, status = "", exc.code
    check("GET / 返回 200", status == 200, str(status))
    check("页面有标题", "text2sql" in html)
    check("页面引用 /api/health", "/api/health" in html)
    check("页面引用 /api/query", "/api/query" in html)
    check("页面声明 UTF-8", "UTF-8" in html.upper())

    print("\n" + "=" * 70)
    print(f"结果：{PASS} 通过 · {FAIL} 失败")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
