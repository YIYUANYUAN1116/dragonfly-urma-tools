# URMA B7 自动化工具

该目录保存 B7 真实 provider 验证工具。测试对象始终是 Dragonfly 的 `dfdaemon`/`dfget`；demo 只作为
UMDK 行为参考，不提供替代数据路径。

当前提供：

- `discover`：通过 SSH 执行只读环境检查；
- `plan`：生成双机或单机双实例的确定性执行计划，不执行其中的变更操作。
- `render-config`：从现有 YAML 生成隔离配置，不覆盖源文件；
- `prepare`：生成 manifest；只有显式 `--execute` 才在远端创建隔离目录、配置和唯一 origin 链接；
- `run`：只有显式 `--execute` 才启动本轮 dfdaemon，按 case 完成 warmup/measured P2P
  传输并采集证据；
- `cleanup`：只有显式 `--execute` 且 owner/PID/path gate 全部通过才删除本轮资源。

## 快速使用

```powershell
cd D:\Deveploment\Workplace\docs\engineering-lab\dragonfly-urma-adaptation\tools\urma-b7
python .\b7.py plan --mode dual --run-id b7-dryrun
python .\b7.py plan --mode single --host node1 --run-id b7-single-dryrun
python .\b7.py discover
python -m unittest -v .\test_b7.py
```

`discover` 默认连接 `root@90.91.177.158` 和 `root@90.91.177.157`。结果写入被 gitignore 的
`results/`。配置文件只采集相关非敏感键与 SHA-256，不复制完整 YAML。

## Prepare/run/cleanup

下列命令不带 `--execute` 时都只是 dry-run：

```powershell
python .\b7.py prepare --mode dual --run-id b7-smoke-001
python .\b7.py run --manifest .\results\b7-smoke-001\manifest.json
python .\b7.py cleanup --manifest .\results\b7-smoke-001\manifest.json
```

确认 manifest、节点、端口和删除目标后，才逐步执行：

```powershell
python .\b7.py prepare --mode dual --run-id b7-smoke-001 --execute
python .\b7.py run --manifest .\results\b7-smoke-001\manifest.json --execute
python .\b7.py cleanup --manifest .\results\b7-smoke-001\manifest.json --execute
```

单机只需把 prepare 改为：

```powershell
python .\b7.py prepare --mode single --host node1 --run-id b7-single-001 --execute
```

`prepare` 使用 mapping-only YAML overlay，支持补齐缺失 mapping，但会拒绝 tab 缩进和非 block-mapping
父节点。生成配置完整保留源 YAML 的其他字段；完整源配置只在内存中处理，不写入本地结果。

## 已冻结的安全规则

- 不覆盖服务器现有 YAML；
- 不清空 `/var/lib/dragonfly`；
- 不停止非本轮启动的进程；
- `plan` 永不执行 `mutates=true` 的步骤，其他变更命令必须显式指定 `--execute`；
- `prepare` 在任何远端变更前先写 `state=preparing` manifest，便于部分失败后按已记录资源恢复；
- `prepare` 对 parent、child、origin 使用 write-ahead resource record，并在每一步开始和完成后原子替换
  manifest；任一步失败会按 origin、child、parent 的逆序自动 rollback，每个 rollback 结果也立即持久化；
- role 目录先在带 owner marker 的 staging directory 中建立，再原子发布；origin 使用独立 sidecar owner
  marker。自动 rollback 只删除能证明属于当前 run 的资源，无法证明所有权时保留现场并写入
  `prepare-rollback-failed`；
- 未完成事务的 manifest 不允许被后续 `prepare` 覆盖。`planned`、`cleaned` 或
  `prepare-rolled-back` 状态才允许使用同一 run ID 重新 prepare；
- start/stop 只接受 `.b7-owner.json` 与 manifest 一致的目录；stop 还会校验 `/proc/<pid>/cmdline`
  中的精确 dfdaemon binary/config，超时只报告错误，不自动 SIGKILL；
- 后续 cleanup 只能处理 `/tmp/dragonfly-urma-b7/<run-id>`、
  `/var/lib/dragonfly-b7/<run-id>` 和 `/var/www/dragonfly/b7-<run-id>-*`；
- 显式 `cleanup` 同样逐资源持久化结果；一个资源清理失败时仍会继续尝试其他已记录资源；
- 对旧版工具留下且 manifest resource record 已丢失的 partial prepare，显式 `cleanup --execute` 会尝试
  安全恢复：role 必须带匹配当前 run 的 owner marker，origin 必须与配置的 seed 是同一 inode 的硬链接；
  任一校验不通过都会保留现场而不是猜测删除；
- parent 预热和 announce 完成后才能启动 child，避免 scheduler 反向选择 node2。

## 单机模式

单机模式在同一节点规划 parent/child 两套 socket、storage 和完整端口组。它首先用于验证配置隔离；
后续真实执行器会先做 provider loopback smoke。如果同一 device/EID 不支持双进程 RC Jetty，结果应标记
为 `UNSUPPORTED`，不能冒充真实 URMA E2E PASS。

## Performance case

`cases.json` 中的 performance case 会真实执行 `warmups` 和 `repetitions`，不是只记录矩阵参数。工具先在
child 启动前完成全部唯一 task 的 parent preheat，再启动 child 并按相同顺序使用
`--disable-back-to-source` 下载，避免 scheduler 在后续 preheat 中反向选择已经活跃的 child。父子同一轮
使用相同 `--tag`，不同轮次使用不同 tag，并分别写入 storage 目录下的 `output.bin.warmup-NNN` 或
`output.bin.sample-NNN`。output 与 Dragonfly content storage 位于同一文件系统且每轮目标唯一，使正常
路径可以 hard link，避免 task cache 命中、目标冲突和跨文件系统的 1 GiB output copy。warmup 结果保存
但不参与汇总，measured sample 输出单轮耗时/吞吐及 min、median、mean、p95、max、aggregate 吞吐。一次
2 warmup + 5 repetitions 的 1 GiB case 会在 parent/child 各创建 7 个独立 task，执行前需要为两侧
隔离 storage 和 run directory 预留足够空间，结束后使用 `cleanup` 回收。

### 并发 batch（B7.1）

case 可增加 `concurrency: 2..16`。此时 `warmups` 和 `repetitions` 表示 batch 数，每个 batch 包含
`concurrency` 个唯一 tag/task/output。Parent 仍在 Child 启动前顺序预热全部 task；Child 侧由一个远端
SSH 脚本启动所有 `dfget` 子进程，待它们全部到达 run-scoped barrier 后统一释放，避免把控制机线程调度
误当作并发起点。

当前内置第一轮矩阵为 `post1-in32` 与 `post8-in64` 的 `c2/c4/c8`。例如：

```powershell
python .\b7.py prepare --mode dual --run-id b7-queue-c4 --case concurrent-post8-in64-c4 --execute
python .\b7.py run --manifest .\results\b7-queue-c4\manifest.json --execute
```

manifest 同时保留：

- `transfer.samples`：所有 measured task 的平铺明细，供既有逐任务汇总继续使用；
- `transfer.batches.samples`：每个 measured batch 的 makespan、aggregate MiB/s、completion skew、
  per-task throughput 和 Jain fairness；
- `transfer.concurrentSummary`：按 batch makespan 汇总的并发吞吐与平均公平性；
- `transfer.measuredTaskIds`：本轮 measured task ID；
- `evidence/{parent,child}.sample-NNN.tasks.log`：只保留对应 batch task ID 的结构化 daemon 行，warmup
  不会混入 measured 内部 TX/RX/Storage 分解。

task ID 按本工具实际调用的 Dragonfly standard URL-based 规则计算：规范化 URL + tag + `STANDARD`；
B7 不设置 application、revision、piece length 或 filtered query parameters。若后续 case 增加这些参数，
必须同步扩展 task-ID helper，不能继续套用当前公式。

这一模式使用同一个 Child daemon 和同一个 Parent，因此当前 URMA `SessionSlot` 会把 Piece 排到一条
persistent lane 上；它建立的是“上层并发、单 lane 排队”基线。多 Child fan-out 和多 Parent fan-in
需要扩展为多 role manifest，不能用本模式冒充多 lane transport concurrency。

每轮 child dfget 还会在远端同一时钟上记录 start/end，并按运行前后的 dfdaemon 日志行号保存
`evidence/child.warmup-NNN.log` 或 `evidence/child.sample-NNN.log`。工具从该范围内真实的 URMA Piece
completion 提取 first/last Piece，把任务墙钟时间拆成 `startToFirstPieceNs`（调度、建连及首 Piece）、
`firstToLastPieceNs`（稳态 Piece 区间）和 `lastPieceToDfgetEndNs`（收尾、成品落盘/链接及 dfget 退出）。
三段必须精确覆盖 `dfgetElapsedNs`，时间戳越界、没有 URMA Piece completion 或混入多个 task id 都会
使运行失败。每轮明细保存在 child 的 `taskTiming`；`taskTimingSummary` 只汇总 measured samples，warmup
只保留明细，不参与 mean/median/p95/max 和 aggregate 占比。

output 布局是在 `prepare` 时固化进 manifest 的。旧 manifest 若仍把 output 指向 `/tmp`，新版 `run` 会
拒绝执行并要求重新 prepare，避免性能结果继续混入跨文件系统 copy；不要直接手工修改已准备的 manifest。

主证据文件只包含一次 selected-events 扫描和运行中 metrics，不再拼接可能重复的 log tail。工具在发送
SIGTERM 前记录日志行偏移，停止两端后将新增内容分别保存为 `parent.shutdown.log` 和
`child.shutdown.log`。受控停机引发的 peer `early eof` 单独计为 `peerCloseEvents`；其他 CQE、completion、
protocol、digest、Jetty 或 panic 错误会使本轮失败。

证据分析还会固定方向：parent preheat 日志中出现任何从 peer 下载的 Piece，或 child 的 URMA Piece
来自非预期 parent，都会以 `topology contamination` 失败。`urma download failed, fall back to tcp
downloader` 及 parent penalty 文本同样作为真实 fallback 处理。

## 当前限制与后续层

当前 `run` 支持 standard-task correctness、顺序 performance repetitions，以及同一 parent/child 上的
并发 task batch：唯一 origin、全量 parent preheat、child `--disable-back-to-source`、逐 task 三方
SHA-256、固定拓扑校验、task-ID scoped 日志、URMA 日志/metrics 证据以及有序 shutdown。后续仍需增加
多 role fan-out/fan-in、persistent/persistent-cache、failpoint、双 lane 定向中断和带 outstanding WR 的
专项 shutdown case。
