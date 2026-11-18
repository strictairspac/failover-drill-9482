# 数据结构
本小型项目首次启动使用 SQLAlchemy metadata 建立 users/nodes/scenarios/drills/audits 五表。自然键 users.email、nodes.name、scenarios.name 唯一。drills.scenario_id 为外键，快照在入队时固定。上线结构演进须补充版本化迁移，本项目不支持对既有不同结构数据库自动升级。

## 索引构建沙盘（orders 等）
批次 gb-4110 新增在线唯一索引沙盘，首次启动由 metadata 一并建立：

|表|用途|关键约束|
|---|---|---|
|orders|业务写单。存量允许历史重复单号|order_no 普通索引（非唯一，构建前不能约束）|
|index_builds|一次在线建索引的进度：快照上界、扫描游标、阶段、等待原因|attempt_key 唯一（同次重试靠它恢复）|
|index_entries|影子唯一索引，构建期间逐批填充|(build_id, order_no) 与 (build_id, order_id) 唯一|
|order_write_logs|构建期间写入/纠正的变更日志，追赶阶段逐条核对|processed 未核对的日志会阻止索引生效|
|failovers|切主请求与闸门结果|attempt_key 唯一，避免重复请求产生多条记录|
|index_events|停在何处、何时生效的可读事件流|按 build_id 索引|
|sandbox_locks|主库写单/构建/纠正共用的串行化行锁|name 唯一|

`index_entries(no)` 的唯一约束是**按构建隔离**的：同一单号可以在不同构建里各有一条，但在一次构建内重复填充会直接触发唯一冲突。真正的业务唯一索引只在构建校验通过后由 `index_builds.status=active` 表示（沙盘不真实执行 `ALTER TABLE ... ADD UNIQUE`，避免锁定生产表；副本能否拒重由该状态决定）。

存量数据迁移：已有 deployments 升级时需先 `CREATE TABLE` 上述表（或临时开启 SEED_DEMO 之外的建表脚本），并对 orders 回填历史数据；`order_no` 上不要建唯一索引，否则历史重单会阻塞部署。
