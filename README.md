# 展厅换牌网页

馆员录入最多 12 件展品的讲解词、现号与目标号（两组号码各自不重复，另留一个
空闲且不作目标号的临时号），系统算出**最少换牌次数**，逐条派发指令；馆员在
现场换牌后回填看到的新旧牌号完成核对，核对通过才发下一条。游客在另一个页面
按墙上号码查讲解。

## 快速启动（Docker Compose）

```bash
docker compose up --build -d
# 馆员工作台  http://localhost:8080/clerk
# 游客查号    http://localhost:8080/gallery
```

数据存于命名卷 `gallery-data`（容器内 `/data/gallery.db`），重启容器不丢。
停止：`docker compose down`；连同数据清空：`docker compose down -v`。

## 本地运行（无 Docker）

```bash
python3 -m pip install -r requirements.txt
DATABASE_PATH=./instance/gallery.db python3 -m app          # 开发服务器
# 或：DATABASE_PATH=./instance/gallery.db gunicorn -w 2 -k gthread wsgi:app
```

## 使用流程

1. **录入新方案** `/clerk/plans/new`
   - 每行：讲解词、现号、目标号；整行空白视为未用，最多 12 行。
   - 现号集合与目标号集合必须相同且各自不重复；临时号必须空闲、且不出现在任何目标号中。
   - 提交后显示最少步数（例：两个 2-循环共 6 步；12 件整体轮换为 13 步）。
2. **发出下一条指令**：方案页点按钮。已有待核对指令时不会重复发出。
3. **现场核对** `/clerk/instructions/<id>`
   - 看到**新号** → 确认换牌，可发下一条。
   - 看到**旧号** → 撤回本次指令，该步可重新发出。
   - 其他号码 → 报错，指令保持待核对。
4. **中断续办**：待核对状态全部落库。重启后打开工作台，仍能进入同一条待核对指令，按现场牌号继续。
5. **游客查号** `/gallery`
   - 输入墙上号码，或点号码按钮。
   - 有指令待核对时，**新旧两号都显示「换牌中」**，不给讲解；其余号码按现牌查。
   - 已确认/已撤回的指令被重复上报，不改变任何状态，后续指令与查号结果保持原样。

## 最少步数原理

每件展品必须到目标号，置换可拆成若干循环。长度 k≥2 的循环至少 k+1 步
（借临时号起手、k 次归位、临时号重新空出），不同循环互不干扰，
故最少步数 = Σ(k+1)。生成器只走「展品 → 当前空号」的合法移动，
对 n≤5 的全排列已用 BFS 穷举验证与最短路径一致。

## 测试与验收

```bash
python3 -m pytest tests/ -q                      # 单元 + 集成（Flask 测试客户端）
BASE_URL=http://127.0.0.1:8080 python3 -I scripts/e2e_check.py   # 对运行中服务的 HTTP 端到端走查
```

脚本第 4 段会验证「中断续办」：可让它启一个新进程（同一数据库）继续处理待核对指令。
`RESTART_CMD` 负责起新进程，`E2E_BASE_FILE` 指向写有新地址的文件；脚本用 `/healthz`
的 `boot_id` 断言确实是新进程，而不是旧进程还在服务。

```bash
# 例：A 进程在 8210，B 进程用同一个 /data/gallery.db 起在 8211
RESTART_CMD=/path/to/start_process_b.sh \
E2E_BASE_FILE=/tmp/new_base.txt \
BASE_URL=http://127.0.0.1:8210 python3 -I scripts/e2e_check.py
```

## 结构

```
app/__init__.py   应用工厂、模板过滤器
app/db.py         SQLite 连接、BEGIN IMMEDIATE 写事务
app/schema.sql    表结构（可重复执行）
app/planner.py    校验与最少步数换牌规划
app/service.py    方案 / 指令 / 上报 / 查号业务逻辑
app/routes.py     HTTP 路由（HTML + 可选 JSON）
app/templates/    页面
scripts/e2e_check.py  端到端验收脚本
tests/test_app.py     自动化测试
```

## 接口（JSON，便于演示与集成）

- `POST /clerk/plans`（表单）→ 302 方案页
- `POST /clerk/plans/<id>/instructions?format=json` → `{ok, created, instruction:{id,step_index,from_number,to_number,status}}`，新发出为 201，已有待核对为 200
- `POST /clerk/instructions/<id>/report?format=json`，body `{"number": 3}` → `{ok, result, instruction_status, changed, message}`，`result ∈ {confirmed, withdrawn, mismatch, invalid, already_finalized}`
- `GET /gallery/lookup?number=3&plan=<id>`（HTML）
- `GET /healthz` → `{"ok": true, "boot_id": "..."}`（`boot_id` 每次进程启动变化，便于确认重启）
