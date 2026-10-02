"""演示数据库：电商（客户 / 商品 / 订单 / 订单明细）。

数据是**合成**的，但分布是有意的：
* 客户集中在少数几个城市（让 GROUP BY 有意义）；
* 存在一个金额显著高的类目（让 ORDER BY 聚合有区分度）；
* 有一批 cancelled 订单（让 WHERE 过滤有区分度）；
* 用固定随机种子 —— 每次生成的库完全一样，测试才可复现。
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京"]
CITY_WEIGHTS = [22, 20, 12, 12, 10, 10, 7, 7]

CATEGORIES = {
    "数码": (800.0, 5000.0),
    "家电": (300.0, 4000.0),
    "图书": (20.0, 120.0),
    "服饰": (60.0, 900.0),
    "食品": (10.0, 200.0),
}
PRODUCT_NAMES = {
    "数码": ["无线耳机", "机械键盘", "移动电源", "智能手环", "蓝牙音箱",
             "USB-C 扩展坞", "电竞鼠标", "便携显示器"],
    "家电": ["空气炸锅", "电饭煲", "吸尘器", "加湿器", "破壁机",
             "电暖器", "挂烫机", "净水壶"],
    "图书": ["算法导论", "深入理解计算机系统", "流畅的 Python",
             "SQL 必知必会", "设计模式", "计算机网络", "统计学入门"],
    "服饰": ["连帽卫衣", "运动T恤", "牛仔裤", "轻便外套", "跑步鞋",
             "休闲衬衫", "针织帽"],
    "食品": ["坚果礼盒", "挂面", "咖啡豆", "燕麦片", "蜂蜜",
             "牛肉干", "绿茶"],
}

STATUSES = ["paid", "shipped", "done", "cancelled"]
STATUS_WEIGHTS = [40, 25, 25, 10]

DDL = """
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    signup_date TEXT NOT NULL
);
CREATE TABLE products (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    category TEXT NOT NULL,
    price    REAL NOT NULL,
    stock    INTEGER NOT NULL
);
CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    total_amount REAL NOT NULL
);
CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_product ON order_items(product_id);
"""


def build(path: str | Path, *, customers: int = 60, products: int = 34,
          orders: int = 260, seed: int = 42) -> Path:
    """生成演示库。固定 seed，产物逐字节可复现。"""
    rng = random.Random(seed)
    target = Path(path)
    if target.exists():
        target.unlink()
    conn = sqlite3.connect(str(target))
    try:
        conn.executescript(DDL)

        # 客户：名字不重样就行，重点在城市的加权分布
        names = [f"用户{i:03d}" for i in range(1, customers + 1)]
        cities = rng.choices(CITIES, weights=CITY_WEIGHTS, k=customers)
        base = datetime(2025, 1, 1)
        customer_rows = [
            (i + 1, names[i], cities[i],
             (base + timedelta(days=rng.randrange(0, 300))).isoformat())
            for i in range(customers)
        ]
        conn.executemany(
            "INSERT INTO customers(id,name,city,signup_date) VALUES(?,?,?,?)",
            customer_rows)

        # 商品：每个类目按区间取价
        product_rows = []
        pid = 0
        for cat, (lo, hi) in CATEGORIES.items():
            pool = PRODUCT_NAMES[cat]
            for j, pname in enumerate(pool):
                pid += 1
                product_rows.append((
                    pid, f"{pname}", cat,
                    round(rng.uniform(lo, hi), 2),
                    rng.randrange(0, 500)))
        conn.executemany(
            "INSERT INTO products(id,name,category,price,stock) "
            "VALUES(?,?,?,?,?)", product_rows)

        # 订单：金额来自明细求和（保证一致性，而不是随机编一个数 ——
        # 否则「按金额排序」的结果和明细对不上，演示就是假的）
        order_rows, item_rows = [], []
        day0 = datetime(2025, 3, 1)
        for oid in range(1, orders + 1):
            cust = rng.randrange(1, customers + 1)
            status = rng.choices(STATUSES, weights=STATUS_WEIGHTS, k=1)[0]
            created = day0 + timedelta(
                days=rng.randrange(0, 180), minutes=rng.randrange(0, 1440))
            n_items = rng.choices([1, 2, 3, 4], weights=[45, 30, 15, 10])[0]
            total = 0.0
            for _ in range(n_items):
                pr = product_rows[rng.randrange(len(product_rows))]
                qty = rng.choices([1, 2, 3], weights=[70, 20, 10])[0]
                # 成交价在标价附近浮动 ±10%
                unit = round(pr[3] * rng.uniform(0.9, 1.1), 2)
                total += qty * unit
                item_rows.append((oid, pr[0], qty, unit))
            order_rows.append((
                oid, cust, status, created.strftime("%Y-%m-%d %H:%M"),
                round(total, 2)))
        conn.executemany(
            "INSERT INTO orders(id,customer_id,status,created_at,total_amount) "
            "VALUES(?,?,?,?,?)", order_rows)
        conn.executemany(
            "INSERT INTO order_items(order_id,product_id,quantity,unit_price) "
            "VALUES(?,?,?,?)", item_rows)
        conn.commit()
    finally:
        conn.close()
    return target
