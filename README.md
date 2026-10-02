# text2sql — 安全闸门优先的中文自然语言查库

自然语言 → SQL → 只读沙箱执行。**主角是安全层，不是生成器**：SQL 不管从哪来
（规则生成器 / LLM / 人手写），都要过同一道白名单闸门才能落库。

```
问题 ──► 生成器（规则版 / OpenAI 兼容 LLM 版，可插拔）
            │  生成的 SQL 不可信
            ▼
        安全闸门 guard.py
        ├─ 只允许 SELECT / WITH
        ├─ 标识符白名单：表名必须真实存在（模糊接近也要拦），
        │   列名必须在用到的表里唯一定位，函数只能在白名单里
        ├─ 强制 LIMIT（缺失补 200，超出收紧到 200）
        └─ 拒绝多语句 / 注释粘连 / PRAGMA / ATTACH
            │  GuardReport（用到的表/列/函数、limit 是否收紧）
            ▼
        只读沙箱 sandbox.py
        ├─ SQLite authorizer 二次兜底（就算 guard 有洞也写不进去）
        ├─ 每次执行独立连接 + busy 超时 + interrupt 强制中断
        └─ 行数沙箱层兜底截断
            ▼
        结果 + 过程（为什么是这个 SQL：识别的模式、置信度、命中词）
```

## 为什么这么设计

- **生成器不可信是前提**。规则版会拼错、LLM 会幻觉出不存在的表名列名 ——
  所以校验对象是 SQL 本身，而不是信任某个生成器。
- **双层防线**：词法级白名单（guard）+ SQLite authorizer（沙箱）。
  测试里专门有一条"绕过 guard 直接对沙箱下发 DELETE"的用例，
  验证防线冗余真的冗余。
- **可解释**：每次查询返回 trace（识别到的模式、置信度、命中的表/列），
  以及"为什么是这个 SQL"。
- **核心零第三方依赖**：`app/` 的 guard / sandbox / schema / 规则生成器是
  纯 Python 标准库，`scripts/check_core_isolation.py` 是 CI 门禁，
  有人手滑在核心里 `import sqlglot` 会被静态+动态双检查揪出来。

## 快速开始

```bash
pip install -r requirements.txt          # fastapi / uvicorn / pydantic
python -c "from app.seed import build; build('data/demo.db')"   # 生成演示库
python -m uvicorn app.main:app --port 8133
# 打开 http://127.0.0.1:8133
```

接入 LLM（可选，OpenAI 兼容接口）：

```bash
export TEXT2SQL_LLM_BASE_URL=https://api.openai.com/v1
export TEXT2SQL_LLM_API_KEY=sk-...
export TEXT2SQL_LLM_MODEL=gpt-4o-mini
```

未配置时自动用规则版，页面上会显示「未配置（用规则版）」。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务状态、表列表、LLM 是否配置 |
| GET | `/api/options` | 示例问题、行数上限 |
| GET | `/api/schema/prompt` | 给 LLM 用的 schema 提示词 |
| POST | `/api/query` | `{question, prefer_llm}` → 走完整流水线 |
| POST | `/api/sql/check` | `{sql}` → 手动过一遍安全闸门（页面上有面板） |

## 测试与验收

```bash
python -m pytest -q                      # 113 个单元测试
python scripts/check_core_isolation.py   # 核心零依赖门禁
python scripts/e2e_check.py http://127.0.0.1:8133   # 66 项端到端黑盒验收
node scripts/shot_frontend.js http://127.0.0.1:8133 docs/screenshots
                                         # 54 项真实浏览器渲染验证 + 截图
```

截图见 `docs/screenshots/`，包括幻觉表名被拦截、模糊提示「最像：orders」。

## 目录

```
app/
  schema.py     表/列/外键定义，中文别名，模糊解析（拒绝「差一点」的名字）
  seed.py       演示库：客户/商品/订单，260 行订单带时间与状态
  guard.py      ★ 安全闸门：词法白名单校验，含 CTE 与子查询递归校验
  sandbox.py    ★ 只读沙箱：authorizer + 超时 + 行数兜底
  generator.py  规则生成器（纯标准库）+ LLM 生成器（可选）
  pipeline.py   生成 → 校验 → 执行 的编排与 trace
  main.py       FastAPI
web/index.html  单文件前端：查询 / 结果 / 过程 / schema / SQL 校验面板
scripts/        e2e、零依赖门禁、浏览器渲染验证
tests/          113 个单元测试（guard 56 / pipeline 22 / schema 23 / sandbox 12）
```

MIT License
