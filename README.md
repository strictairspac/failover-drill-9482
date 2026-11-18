# 衡石 · 值班切主演练沙盘
衡石数据值班组（10人）把节点巡检、故障剧本与切主证明合并为可复核演练记录。

## 业务与模块
原流程用值班表记录节点、聊天传递故障、人工比较日志。痛点：输入版本混乱、隔离证明缺失、决策无法复查。上游是巡检记录；本工具替代人工纸上演练；下游是复核人和运维值班负责人。模块为节点状态、故障剧本、演练队列、决策证据，配套登录及JSON导出。三个实体 Node / Scenario / Drill 全链路实现。

**边界：受控教学/运维演练计算模型，不连接真实复制集群、不执行真实生产选主；任期、日志位置和fencing均由用户输入声明。** 本题真实 MySQL 保存业务输入/快照/结果，Python 实际计算规则，后台 worker 异步处理。

## 启动
```bash
cp .env.example .env
docker compose up -d --build
# http://localhost:18249
# API http://localhost:19249/docs
docker compose exec -T backend python -m scripts.seed_demo
docker compose down --remove-orphans
```
初次启动自动seed；脚本自然键查重，不覆盖用户修改。数据库初次连接和DDL失败会使启动失败，避免静默运行坏结构。演示seed异常的降级处理见测试报告。

|账号|密码|权限|典型动作|
|---|---|---|---|
|admin@hengshi.local|Drill2026!|全操作/审计/节点编辑|修改节点日志位置|
|operator@hengshi.local|Drill2026!|剧本新增/运行/导出|提交晚班演练|
|viewer@hengshi.local|Drill2026!|只读/导出|核对规则及结果|

## 在线唯一索引沙盘
订单表允许历史重复单号；新建唯一索引不能锁表，也不能在切主时让副本把重复单号当新单收下。沙盘按真实在线建索引的顺序建模：

1. **写单**：主库服务层先查重再写。已有单号（含尚未纠正的旧重单）返回 409；其他单号照常写入。
2. **建索引**：按 id 顺序每批 3 条扫描进影子唯一索引（`index_entries`）。扫描遇到旧重单就停在已扫描位置，`blocking_order_id` 指向那条旧单。
3. **纠正旧重单**：只有同组里 id 较大的重单可纠正，新单号不能被占用；纠正后构建从原游标继续。其他订单不能改单号。
4. **构建期间写入**：照常落库，同时写 `order_write_logs`；构建中断期间也一样。
5. **追赶与校验**：存量扫完后逐条核对日志，再校验「无旧重单 + 全部订单被覆盖 + 索引单号与订单一致」。任一不满足，索引不生效。
6. **切主闸门**：请求切主时看副本能否按索引拒重。索引未生效就暂缓并记录原因；索引一旦生效，此前暂缓的切主自动放行。
7. **中断恢复**：用同一构建标识重试，从 `scan_cursor` 继续，已写索引条目原样保留，不新建构建、不重复填充。

页面的 05 索引构建沙盘提供写单、纠正、开始/推进/中断构建、请求切主和副本视角检查，并展示四段进度、等待原因、索引生效时间与事件流。观察员只读。

边界：不执行真实 `ALTER TABLE`、不连接复制集群；「副本能否拒重」由影子索引覆盖范围和构建状态声明，不是真实副本行为。

## 一天剧本
09:00操作员登录；09:05核对hs-primary日志8100、hs-east日志8100、hs-west日志8098，任期均6；09:10创建shift-20261117剧本，允许损失0、新任期门槛7、主故障且已隔离；09:20提交晚班演练；09:25查看候选hs-east、新任期7、多数派2/2；09:30导出JSON；09:35运行“缺少隔离证明”案例，应阻断；09:40观察员复核但新增返回403；10:00管理员查看 /api/audits。14:00值班开始在线建唯一索引，扫到 HS-2611 旧重单后停住；14:10纠正两条旧重单、追平期间写入后索引生效；14:15此前暂缓的切主自动放行。

## 算法口径
输入为入队时冻结的节点与剧本快照。法定票数 floor(配置投票节点数/2)+1；可达票扣除不可达/已隔离/本次故障主节点；候选必须可达、有投票权、非主、未隔离且落后量不超过允许日志损失。新任期=max(节点任期)+1。所有安全条件成立时按日志位置降序、节点名升序确定候选；缺少旧主fencing、票数不足、未触发故障、任期不足或无合格候选均阻断。空节点、零投票节点、多个主节点计算失败并保存原因。结果含输入SHA256与模型版本，可重复计算。不会伪造真实集群共识证据。

## 工程
|组件|技术|
|---|---|
|前端|原生HTML/CSS/JS模块，Nginx1.27|
|后端|Python3.11/FastAPI/SQLAlchemy/PyJWT|
|任务|MySQL持久队列 + Redis worker互斥锁 + 心跳|
|存储|MySQL8.0、Redis7|

订单防重：主库写入串行化在 sandbox_locks 行锁内完成，构建与写单共用同一把锁；影子索引 index_entries 只在一次构建内唯一。
所有计算和权限检查在后端。JWT有效8小时，密码PBKDF2-SHA256 180000轮随机盐。20条待处理任务配额。审计保存业务写入动作；错误返回request-id。五服务均真实健康检查。

目录：frontend/src/api、stores、types、components/common、hooks、pages、router、utils、constants；backend/src/routes、controllers、services、models、schemas、middlewares、utils、constants、config；backend/scripts；backend/migrations。本地开发：安装backend/requirements.txt并设DATABASE_URL、REDIS_URL、JWT_SECRET，再运行uvicorn；前端以Nginx代理/api/去除前缀。

环境：COMPOSE_PROJECT_NAME控制隔离命名；FRONTEND_PORT/BACKEND_PORT/DB_PORT为端口；DB_PASSWORD为演示数据库密码；JWT_SECRET为签名密钥；SEED_DEMO=false关闭演示数据。命名卷db-data、redis-data、artifacts；reset时显式down -v删除数据。前端字体依赖Google Fonts，离线回退serif/sans-serif。模型是简化安全门禁，非完整Raft实现。MIT许可。

枚举：角色在backend/src/constants/enums.py和frontend/src/constants/enums.js；任务状态同处定义，使用于models/entities.py、services/drill_service.py、worker.py、components/common/status.js；节点角色在schemas/node.py、pages/nodes.js。构建状态/阶段（BUILD_STATUSES、BUILD_STAGES）与切主状态（FAILOVER_STATUSES）同在 enums.py，服务实现见 services/sandbox_service.py。测试、截图与已知差异见批次test-artifacts/gb-4110/TESTING.md。

## 测试
```bash
cd backend
DATABASE_URL=sqlite:////tmp/sandbox-test.db JWT_SECRET=test PYTHONPATH=. python3 -m pytest tests -q
```
测试用 SQLite 覆盖：旧重单阻断与纠正、新重单拒绝、构建期/中断期写入追赶、校验不通过不生效、切主暂缓与放行、同标识恢复不重复建索引。运行时仍以 MySQL 为准，写单串行化在 MySQL 上使用 sandbox_locks 行锁 + 唯一索引。
