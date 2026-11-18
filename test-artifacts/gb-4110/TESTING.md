# 批次 gb-4110 · 在线唯一索引沙盘 测试记录

## 环境
- 容器内无 Docker/MySQL 守护进程；本地以 SQLite 跑后端 pytest 与 HTTP 冒烟，接口与页面用 jsdom 渲染校验。
- 运行时目标仍是 MySQL 8.0（docker compose）。SQLite 仅用于自动测试，MySQL 专属路径（`sandbox_locks` 行锁 + `SELECT ... FOR UPDATE`）已按方言分支实现但未在本机实跑。
- 依赖：`backend/requirements.txt` + `pytest` + `httpx`；前端无构建步骤。

## 自动化
```bash
cd backend
DATABASE_URL=sqlite:////tmp/sandbox-test.db JWT_SECRET=test PYTHONPATH=. python3 -m pytest tests -q
# 21 passed
```

覆盖点：
- 存量 18 条、2 组历史重单（HS-2611、HS-2614）被识别；初始副本不能拒重。
- 扫描按批 3 条推进，遇到旧重单停在已扫描 9 条处，`blocking_order_id` 正确；已扫描部分保留在影子索引。
- 构建期间写新单成功并记待核对日志；写已有单号（含旧重单）返回 409。
- 纠正旧重单后从原游标继续；新单号被占用/与原号相同/非重单均拒绝。
- 校验阶段发现扫描后直接写入的重复单 → 暂停等纠正，不生效。
- 待核对日志未清零时不生效；清零且一致后 `status=active`、记录 `index_effective_at`，新单直接入索引。
- 生效前请求切主一律 `deferred` 并写明卡在哪个阶段；生效时此前暂缓的切主自动 `completed`。
- 中断后 `scan_cursor`/`scanned`/索引条目不变；同标识重试仅恢复（`resumed_count+1`，构建总数仍为 1），换标识被拒；中断期间写入在恢复后追平进索引。
- 权限：观察员写操作 403；重置需管理员；未登录 401。

## HTTP 冒烟（SQLite + uvicorn）
真实进程跑通：start → tick(steps=20) 停在 #10 → 写 HS-8888 成功 / HS-2611 409 → 纠正 #10、#16 → tick 至 active（19 条索引，0 待核对）→ 切主 completed；`/audits` 记录 `index_build:*`、`order:*`、`failover:request`。

## 前端渲染（jsdom）
- 导航新增「05 索引构建沙盘」，页面渲染四段流程、进度条、等待原因、副本视角、切主列表、写单/纠正表单、事件流。
- 停住阶段高亮为阻断；无构建时给引导文案。
- 纠正、写单、推进、持续推进、中断、切主、副本检查均发出预期请求；观察员看不到写操作表单。
- 该 jsdom 校验为临时脚本，未纳入仓库（无前端构建链）。

## 已知差异 / 边界
- 不执行真实 `ALTER TABLE ... ADD UNIQUE`；「副本能否拒重」由影子索引覆盖与构建状态声明，不是真实复制链路行为。
- 构建由页面「推进一批/持续推进」或 API tick 驱动，无独立 worker 定时推进，便于逐步观察停点。
- 写单串行化在 SQLite 上退化为单写者；MySQL 上使用 `sandbox_locks` 行锁 + `FOR UPDATE` 查重。并发写单的 MySQL 实测未在本机执行。
- 历史重单纠正只支持改单号，不删除原记录，保留 `original_order_no` 便于复核。
