# URMA B7 自动化工具

该目录保存 B7 真实 provider 验证工具。测试对象始终是 Dragonfly 的 `dfdaemon`/`dfget`；demo 只作为
UMDK 行为参考，不提供替代数据路径。

当前提供：

- `discover`：通过 SSH 执行只读环境检查；
- `probe-provider`：用 `urma_perftest` 执行单/双节点 RC/RM provider 前置探测，归档两端命令、输出、
  退出码、completion status 和环境快照；默认只生成计划，显式 `--execute` 才通过 SSH 执行；
- `plan`：生成双机或单机双实例的确定性执行计划，不执行其中的变更操作。
- `render-config`：从现有 YAML 生成隔离配置，不覆盖源文件；
- `prepare`：生成 manifest；只有显式 `--execute` 才在远端创建隔离目录、配置和唯一 origin 链接；
- `run`：只有显式 `--execute` 才启动本轮 dfdaemon，按 case 完成 warmup/measured P2P
  传输并采集证据；
- `cleanup`：只有显式 `--execute` 且 owner/PID/path gate 全部通过才删除本轮资源。

B7 支持三个显式 URMA profile，各自使用独立 Dragonfly checkout：

- `--profile rm`（默认）：`dragonfly-client-urma-rm` / `urma-rm-prototype`，当前诊断清单生成
  `transportMode: rm`、`tpType: ctp` 和 `peerGuaranteedRxCredits`；Dragonfly 自身缺省仍为 RTP，B7 显式
  选择 CTP 是为了复测已观察到的单机 CTP 路径。资源模型是 process-wide shared endpoint + PeerTarget；
- `--profile read`：`dragonfly-client-urma-read` / `urma-rm-read-only-cleanup`，RM READ-only，
  当前测试栈使用 CTP/eid0。新一轮 READ 测试显式选择这个 profile；默认 CLI profile 仍为历史 `rm`。
- `--profile rc`：`dragonfly-client-urma-private` / `urma-main`，生成 `transportMode: rc`，不写入 RM-only
  的 per-peer guarantee，资源模型是 per-peer RC Jetty/JFR lane。

TCP case 不需要机器安装 URMA/UMDK，也不要求 release binary 使用 URMA feature：两端生成配置都会关闭
`storage.server.urma.enable`，`run` 也会跳过 URMA provider preflight。`prepare --profile` 对 TCP 仍用于
选择待测 Dragonfly checkout；与既有 12.6 节 RC 结果对齐时必须显式使用 `--profile rc`。

manifest 固化 profile、transport、TP 类型、所需最大消息和两个 profile 各自的 cross-node probe 状态；
后续 `run/cleanup` 从 manifest 恢复同一个 repo，不能在 prepare 后切换 profile。RM 文档和结果中的
`lane_id` 仅是尚未改名的 session facade 标识，不代表 RM 中存在每 Peer 独占 Jetty/JFR。

157、158 的统一工作区为 `/home/y30083740/dragonfly`。`dragonfly-urma-tools` 单独记录为工具仓库。
`discover --profile ...` 会选择对应 checkout 并核对预期分支，在 repo、分支、源配置或 release binary
不符合时返回 `incomplete`。

当前目录清单中没有 inventory 仍依赖的 `/home/y30083740/dragonfly/config`。因此首次执行前需要恢复或
迁移 `dfdaemon-parent.yaml`、`dfdaemon-child.yaml`（以及 158 上用于留档的 `scheduler.yaml`），再同步修改
inventory；B7 不会凭空生成 scheduler/manager 地址未知的基础配置。

## RC/RM 跨节点前置门禁

2026-09-07 用户初测观察到：RM perftest 单节点可运行、跨节点未跑通；精确命令、错误输出和
`urma_admin` 资源快照尚未归档。因此 inventory 中 RM profile 的 `crossNodeProbe.status` 为
`failed-unarchived`。这项结果只说明跨节点 RM provider/拓扑尚未成立，不能归因到 Dragonfly shared JFR。
RC profile 保留既有 B5/B6/B8 跨节点通过状态，但正式 A/B 前仍应重新归档当前 binary/config hash。

目前收到的局部日志显示：跨节点 RM+CTP `send_bw` 返回 completion status 4（当前 UMDK 枚举中的
`URMA_CR_LOC_ACCESS_ERR`）；Dragonfly 单节点 RM 则在 Parent `import_jetty=-1` 后回退 TCP。RM 分支现已支持
`tpType: rtp|ctp` 并在 wire/import 层 fail-closed 校验一致性，但尚无真机结果；应先完成同 binary 的
RTP/CTP 最小矩阵，不能把离线改造或现有 perftest 结果登记为 RM profile PASS。

先执行只读发现并保存结果：

```bash
python3 b7.py discover --profile rm
python3 b7.py discover --profile rc
```

`discover` 会额外保存两节点 repo HEAD/dirty 状态、perftest/admin binary SHA-256、
`urma_admin show --all`、`urma_admin show topo`、IP/route/neighbour 快照和 perftest help。provider 最小
矩阵可先 dry-run 检查，再执行：

```bash
# RM：默认依次执行 RTP、CTP；双节点默认 node1 server、node2 client。
python3 b7.py probe-provider --profile rm --mode dual \
  --server-address 90.91.177.158 --run-id rm-provider-001
python3 b7.py probe-provider --profile rm --mode dual \
  --server-address 90.91.177.158 --run-id rm-provider-001 --execute

# 单节点 RM；--server-address 仍必须是该节点的 URMA EID 地址。
python3 b7.py probe-provider --profile rm --mode single --host node1 \
  --server-address 90.91.177.158 --run-id rm-loopback-001 --execute

# RC 基线默认只执行 RTP。
python3 b7.py probe-provider --profile rc --mode dual \
  --server-address 90.91.177.158 --run-id rc-provider-001 --execute
```

结果写入 `results/<run-id>/provider-probe.json`。工具始终从 inventory 传入 `--eid_idx`，且不会把 SSH
管理地址推断为 `-S` 的 URMA EID。`urma_perftest -O` 是 priority，不是 opcode；默认不传，让工具按
RTP/CTP 选择 priority。只有需要复现实验时才显式传 `--priority 6`。RM 的 CTP 最大探测消息为 4096
bytes，所以默认矩阵使用 4096；后续 64 KiB 能力应在 RTP 或 Dragonfly 分片路径单独验证。只有跨节点
结果通过并审阅证据后，才人工把对应 inventory profile 状态改成 `passed`。

URMA case 所选 profile 状态不是 `passed` 时，dual-node `run --execute` 会拒绝执行。TCP case 不读取该
门禁。若目的就是诊断未通过门禁的 URMA，可以显式越过；这种结果必须标记为 diagnostic，不能登记为
PASS：

```bash
python3 b7.py run --manifest results/<run-id>/manifest.json \
  --allow-unvalidated-urma --execute
```

single-node run 不受 cross-node 门禁影响，但其成功不能替代跨节点 provider 验证。

## 快速使用

```powershell
cd D:\Deveploment\Workplace\docs\engineering-lab\dragonfly-urma-adaptation\tools\urma-b7
python .\b7.py plan --mode dual --run-id b7-dryrun
python .\b7.py plan --profile rc --mode dual --run-id b7-rc-dryrun
python .\b7.py plan --mode single --host node1 --run-id b7-single-dryrun
python .\b7.py discover
python -m unittest -v .\test_b7.py
```

`discover` 使用 inventory 当前配置的 SSH 管理地址。结果写入被 gitignore 的
`results/`。配置文件只采集相关非敏感键与 SHA-256，不复制完整 YAML。

## Prepare/run/cleanup

下列命令不带 `--execute` 时都只是 dry-run：

```powershell
python .\b7.py prepare --profile rm --mode dual --run-id b7-smoke-001
python .\b7.py run --manifest .\results\b7-smoke-001\manifest.json
python .\b7.py cleanup --manifest .\results\b7-smoke-001\manifest.json
```

确认 manifest、节点、端口和删除目标后，才逐步执行：

```powershell
python .\b7.py prepare --profile rm --mode dual --run-id b7-smoke-001 --execute
python .\b7.py run --manifest .\results\b7-smoke-001\manifest.json --execute
python .\b7.py cleanup --manifest .\results\b7-smoke-001\manifest.json --execute
```

单机只需把 prepare 改为：

```powershell
python .\b7.py prepare --mode single --host node1 --run-id b7-single-001 --execute
```

### CPU/NUMA placement

不要在 controller 外层用 `taskset python3 b7.py ...`：B7 通过 SSH 在目标节点另起 shell，远端
`dfdaemon`/`dfget` 不会继承 controller 上 Python 进程的 CPU affinity。需要在 `prepare` 时把每个角色的
CPU 列表固化进 manifest，例如：

```bash
python3 b7.py prepare --profile rm --mode single --host node1 \
  --run-id rm-numa23-001 --case urma-piece-16mib-cc1-post1-pipe2 \
  --parent-cpus 192-223 --child-cpus 288-319 --execute
python3 b7.py run --manifest results/rm-numa23-001/manifest.json --execute
```

`run` 会在远端用 `taskset -c` 启动 Parent/Child 的 `dfdaemon`，并以相同角色的 CPU 列表运行该角色的
每个 `dfget`。fan-out/fan-in 中 `--child-cpus` 会应用于全部 Child role。daemon 启动后，B7 从
`/proc/<pid>/status` 读取实际 `Cpus_allowed_list`，分别写入
`result.started.<role>.cpuAffinityRequested` 和 `cpuAffinityEffective`；计划值同时保存在
`cpuPlacement` 与 `generated.<role>.cpuAffinity`。可用下面的命令核对：

```bash
jq '{planned: .cpuPlacement, started: (.result.started // {}) |
  with_entries(.value |= {pid, cpuAffinityRequested, cpuAffinityEffective})}' \
  results/rm-numa23-001/manifest.json
```

CPU ID 必须以测试机 `lscpu -e=CPU,NODE,CORE,SOCKET` 的结果为准；每物理 core 有两个逻辑 CPU 时，不能
只凭连续编号推断 sibling 关系。该功能保证 CPU affinity，不等同于强制 NUMA memory binding。tmpfs 页、
registered buffer 和进程 heap 仍受 first-touch/系统 NUMA policy 影响；若要区分 CPU placement 与内存
placement，应先固定 CPU 做可比测试，再单独增加 `numactl --membind` 实验。

`prepare` 使用 mapping-only YAML overlay，支持补齐缺失 mapping，但会拒绝 tab 缩进和非 block-mapping
父节点。生成配置完整保留源 YAML 的其他字段；完整源配置只在内存中处理，不写入本地结果。

## 已冻结的安全规则

- 不覆盖服务器现有 YAML；
- 不清空 `/var/lib/dragonfly` 或测试外的其他 storage；
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
- 后续 cleanup 只能处理 inventory 配置的 B7 run/storage/origin 根目录下、带本轮 owner marker
  或 run-id 的资源；当前单机默认目录为 `/home/y30083740/dragonfly-b7/run/<run-id>`、
  `/home/y30083740/dragonfly-b7/storage/<run-id>`、
  `/home/y30083740/dragonfly-b7/tmpfs-storage/<run-id>` 和
  `/home/y30083740/dragonfly-b7/origin/b7-<run-id>-*`；兼容旧 inventory 的旧目录仍保留在安全根列表中；
- 显式 `cleanup` 同样逐资源持久化结果；一个资源清理失败时仍会继续尝试其他已记录资源；
- 对旧版工具留下且 manifest resource record 已丢失的 partial prepare，显式 `cleanup --execute` 会尝试
  安全恢复：role 必须带匹配当前 run 的 owner marker，origin 必须与配置的 seed 是同一 inode 的硬链接；
  任一校验不通过都会保留现场而不是猜测删除；
- parent 预热和 announce 完成后才能启动 child，避免 scheduler 反向选择 node2。

## 单机模式

单机模式在同一节点规划 parent/child 两套 socket、storage 和完整端口组。它首先用于验证配置隔离；
后续真实执行器会先做 provider loopback smoke。如果同一 device/EID 不支持双进程 RC Jetty，结果应标记
为 `UNSUPPORTED`，不能冒充真实 URMA E2E PASS。

单机 B7 默认将本轮文件集中在：

```text
/home/y30083740/dragonfly-b7/origin   # seed 与每轮 hard link；必须由 origin HTTP 服务提供
/home/y30083740/dragonfly-b7/storage        # 普通 filesystem case 的 storage 与 output
/home/y30083740/dragonfly-b7/tmpfs-storage  # tmpfs case 的 storage 与 output
/home/y30083740/dragonfly-b7/run      # 配置、socket、pid、daemon/dfget 日志
```

如果 origin HTTP 服务仍以 `/var/www/dragonfly` 为 document root，需要先把服务切换到新的
`origin` 目录，或将 inventory 的 `origin.directory` 改回服务实际提供的目录；B7 不会自动修改
nginx/HTTP 服务配置。

### 当前 READ-only 验证（2026-10-15）

两端 checkout 使用 `urma-rm-read-only-cleanup`，构建应包含 `d79c69c` 的 WR 背压修复。
inventory 的 `readRepo` 目录名接受 `dragonfly-client-urma-read`、
`dragonfly-client-urma-rm-read-only-cleanup` 或 `urma-cached-source`；路径应指向实际构建的 checkout，分支校验保持不变。
先核对 inventory 的 SSH 地址、设备、EID、配置和工作目录，执行 `discover --profile read`；
新的部署栈需要重新确认 provider 和撤权证据，inventory 的历史 passed 记录不能替代本次验证。
READ profile 继续显式写 `providerRevocationValidated: true`，这只是测试配置，不是工具执行了撤权验证。

READ overlay 固定当前默认：total/source/destination 32/16/16 GiB、per-peer source/destination 各8 GiB、
quarantine 1 GiB、共享 WR64、每 Piece 窗口8、sourceWorkers=0、pwrite并发1024。
`maxReadSize: 4294967295B` 等于当前代码默认的软件上限，覆盖源配置遗留的1MiB；
实际 READ 大小取 Piece 剩余长度与两端 provider 上限的最小值，不会预分配4GiB内存。
显式 case 的 `urmaRead` 覆盖这些默认值；缩小池预算时应同时将 per-peer 预算调至不超过对应池。READ profile 不再注入旧 SEND/RECV 调优字段；
源 YAML 中已有弃用字段仍可能触发迁移警告，它们不会控制 READ。

按以下顺序执行，所有新增 case 均为1GiB文件、预热1次、测量3次：

| 顺序 | case | 用途 |
|---|---|---|
| 1 | `read-auto-piece32-cc1` | owner 同步注册基线、正确性 |
| 2 | `read-auto-piece64-cc1` | 大Piece按provider上限自动拆分 |
| 3 | `read-auto-piece32-cc16` | CC16基线，也是workers=0对照 |
| 4 | `read-backpressure-piece32-cc16-credit1` / `credit8` | READ软件上限4MiB，强制共享额度竞争 |
| 5 | `read-workers1-piece32-cc16` / `read-workers2-piece32-cc16` / `read-workers4-piece32-cc16` | 仅改变注册/清理worker数 |
| 6 | `read-auto-piece64-cc16` | 大Piece并发容量和资源回收 |

低额度用例必须同时满足校验通过、实际URMA传输、无TCP fallback、无卡死或异常资源积累。
线程池用例先跑 `read_register_probe --file <file> --remap --threads 1,2,4,8`，
再验证产品中与READ并行时的行为；取消、断连和shutdown仍需额外验证，case未自动注入这些故障。
旧 `read-*post1-pipe2` case 保留其显式READ大小/预算用于历史复测，post/pipeline在READ-only中不参与调优。
1GiB用例用于快速验证，不能替代长时间模型文件吞吐和多节点稳定性测试。

```bash
python3 b7.py discover --profile read
# 先生成计划核对，再添加 --execute；替换 case/run-id 可执行其它组。
python3 b7.py prepare --profile read --mode dual --run-id read-current-001 \
  --case read-auto-piece32-cc1
python3 b7.py prepare --profile read --mode dual --run-id read-current-001 \
  --case read-auto-piece32-cc1 --execute
python3 b7.py run --manifest results/read-current-001/manifest.json --execute
python3 read_attribution_summary.py results/read-current-001
python3 b7.py cleanup --manifest results/read-current-001/manifest.json --execute
```

新结果的 `urmaReadStageSummary.storage` 增加 `digestBlockingQueueNs` / `pwriteBlockingQueueNs`，
归因表的 `crcQ` / `pwrQ` 为blocking线程池排队，`pwrWait` 为pwrite准入等待。
storage 组另含 `fileOpenNs`、`pwriteCalls`（每 Piece 的 pwritev 次数）与 `writebackNs`（后台
`sync_file_range` 时延），归因表的 `fileOpen` / `calls` / `writeback` 列对应它们；
`urmaReadStageSummary.pwriteActivity` 从 pwrite worker 的独立日志行取 `pwriteActiveAtStart`
/ `pwriteActiveAfter`，归因表的 `pwrAct` / `pwrEnd` 列为该 Piece 的 pwrite 并发度（不是时延）。
归因表读取时若 manifest 缺少这些字段，会从 `evidence/child.*.tasks.log` 重新解析补齐。
旧日志缺少的字段不计入样本；保留原始日志用于进一步检查，分位数不能直接相加归因。

child 落盘阶段 `pwrite` 占 Piece E2E 约 65-75%；它不是唯一限速项，但剩余余量比 E2E 吞吐
看起来的小得多（见下）。用 `read_membw_probe.c` 定位（对照 memcpy 上限 / buffered pwrite / tmpfs / O_DIRECT，形状为
`--len` × N 并发，不触碰产品代码），结论：

- `pwrWait`/`pwrQ` ≈ 0、`pwrAct` ≈ 11 → 限流器、blocking 池、并发度都不是原因；
- 一次 32 MiB pwritev 无竞争时只要 ~2 ms（M3 ≈ M1 memcpy），memcpy 聚合到 254 GiB/s →
  不是拷贝，也不是内存带宽；
- iostat 设备 `%util` 仅 ~14%，每轮只落 1 GiB → 不是设备；
- `--same-file` 复刻多 lane 写同一 task 文件：聚合恒为 **16.2 GiB/s**，p50 随并发线性涨
  （T=16 → 28.8 ms），`--fallocate` 前后一致 → 与块分配无关。根因是 **ext4 对同一 inode 的
  buffered 写走 `inode_lock` 排他锁、完全串行**；产品的 24 ms ≈ 11 路排队 × 2 ms。
- 按同一口径比，**余量只有 ~1.4×，不是 2.5×**：dfget E2E 吞吐 6.65 GiB/s 是**含前导**的
  总账，前导（`toREAD`：进程/连接/任务准备/scheduler/父片元数据同步/准入，URMA lane 与
  source 注册都在该埋点之后）占 E2E 的 **37–46%**（workers0/1/2/4）。扣掉前导后，一轮 1 GiB
  的写入在数据期稳定跑到 **~11.8 GiB/s ≈ 16.2 GiB/s 上限的 72%**（1 GiB ÷ pwrite 区间，
  四轮均落在 71–73%），数据期整体速率 9.3→10.5 GiB/s（占上限 57%→65%）。
- 所以结论要改口：**不是"存储无关紧要"，而是写路径在整个数据期持续占用同 inode 硬顶的
  七成**；该硬顶与并发数无关（probe 已证聚合恒为 16.2 GiB/s），且**它只属于"并发写同一文件"
  这一形状**——同一台盘上 perfile 同轮能到 194.7 GiB/s（12.2×）。因此同 inode 不拆时继续抬
  READ 并发只会把队列堆到 pwrite 上，换不到吞吐；拆同 inode 有效，但 **O_DIRECT 不行**
  （perfile O_DIRECT 恒 4.7 GiB/s，比 buffered 慢 3.3–53×，是反向优化）。
- 口径提醒：workers0→4 的 E2E +24%（5496→6814 MiB/s）里，首片→末片几乎不变
  （82.92→81.23 ms）、数据期速率只 +13%，增量主要来自前导 −37%（86.94→54.98 ms），不能记作
  READ 管道收益，与 `04-performance/rm-read.md` 的结论一致。报告 rate 的窗口口径仍待核对：
  workers4 的 `1 GiB ÷ dfget`（6820 MiB/s）与报告值吻合，workers0/1 则偏低 5–6%。

复现：

```bash
cc -O2 -Wall -Wextra -pthread read_membw_probe.c -o read_membw_probe
# 在 child 节点、指向产品实际写入的同一文件系统
# 对照基准：每线程写自己的文件（不加 --same-file）
./read_membw_probe --dir /path/to/storage --fallocate \
  --len 33554432 --iters 8 --threads 1,2,4,8,16
# 复刻产品形状：所有线程写同一个文件的不同 offset；--fallocate 对齐 create_task 的
# set_len + fallocate(KEEP_SIZE)，两行必须同线程数对比
./read_membw_probe --dir /path/to/storage --same-file --fallocate \
  --len 33554432 --iters 8 --threads 1,2,4,8,16
# 分离 page cache 与设备：再加 tmpfs 与 O_DIRECT 变体
./read_membw_probe --dir /path/to/storage --tmpfs /dev/shm --odirect \
  --len 33554432 --iters 8 --threads 1,2,4,8,16
```

读法见 probe 头部注释：M3 buffered p50 接近 2×M1 memcpy p50 即拷贝受限；并发 pwrite 聚合
不再随线程数线性增长而 memcpy 仍在增长，说明共享内存上限；tmpfs ≈ disk 排除设备与脏页节流
（**本轮 tmpfs 实测反常，见下节**）；O_DIRECT ≪ buffered 即跳过 page-cache 拷贝的收益上限；
`samefile` 行远慢于 `perfile` 行即同 inode 并发写受限，与硬件无关。`--fallocate` 与产品一致
（预分配，避免计入块分配开销），做同 inode 对比时应打开。

### read_membw_probe 实测数据

2026-10-19 在 child 节点实测，目标为产品实际写入的 storage（本地 ext4 on LVM），三轮均为
`--len 33554432 --iters 8 --threads 1,2,4,8,16`，每档 op 数 = threads × 8：

| 轮 | 命令要点 | 跑出的行 |
|---|---|---|
| A | `--same-file` | memcpy、disk buffered samefile |
| B | `--tmpfs /dev/shm --odirect`（**无** `--same-file`，即 perfile） | memcpy、disk buffered perfile、disk odirect perfile、tmpfs buffered perfile |
| C | `--same-file --fallocate` | memcpy、disk buffered samefile+pre |

原始输出未归档到本仓库，下面为三轮输出转录。

**M1/M2 memcpy —— 纯内存拷贝，只用 src/dst 缓冲区、不碰文件**，与 layout / 预分配无关
（探针只跑 threads=1 与 threads=16 两档，下表为三轮范围）：

| target | threads | p50 (ms) | agg (GiB/s) |
|---|---:|---:|---:|
| M1/M2 memcpy | 1 | 1.601–1.829 | 17.4–19.0 |
| M1/M2 memcpy | 16 | 1.602–1.855 | 246.1–254.5 |

**M3 disk buffered samefile —— 所有线程写同一个文件**（A = 不带 `--fallocate`，C = 带）：

| threads | p50 (ms) A | agg A | p50 (ms) C | agg C |
|---:|---:|---:|---:|---:|
| 1 | 2.082 | 15.0 | 2.015 | 15.5 |
| 2 | 1.874 | 15.9 | 2.022 | 16.1 |
| 4 | 7.214 | 16.1 | 7.397 | 16.1 |
| 8 | 12.598 | 16.1 | 12.545 | 16.2 |
| 16 | 29.707 | 16.0 | 28.771 | 16.2 |

**M3–M6 perfile —— 每线程写自己的文件**（轮 B，无 `--fallocate`）：

| target | 模式 | threads | p50 (ms) | p95 (ms) | agg (GiB/s) |
|---|---|---|---:|---:|---:|
| M3 disk | buffered | 1 | 2.026 | 2.471 | 15.4 |
| M3 disk | buffered | 2 | 2.021 | 2.565 | 30.4 |
| M3 disk | buffered | 4 | 1.812 | 2.593 | 64.1 |
| M3 disk | buffered | 8 | 1.908 | 2.591 | 122.4 |
| M3 disk | buffered | 16 | 2.001 | 2.690 | **194.7** |
| M4 disk | odirect | 1 | 6.680 | 6.815 | 4.7 |
| M4 disk | odirect | 2 | 13.275 | 13.399 | 4.7 |
| M4 disk | odirect | 4 | 26.541 | 26.599 | 4.7 |
| M4 disk | odirect | 8 | 53.064 | 74.179 | 4.7 |
| M4 disk | odirect | 16 | 106.978 | 135.121 | 4.7 |
| M5 tmpfs | buffered | 1 | 8.821 | 9.175 | 3.5 |
| M5 tmpfs | buffered | 2 | 9.369 | 9.751 | 6.6 |
| M5 tmpfs | buffered | 4 | 9.292 | 9.703 | 13.3 |
| M5 tmpfs | buffered | 8 | 9.316 | 9.768 | 26.2 |
| M5 tmpfs | buffered | 16 | 9.562 | 10.000 | 49.4 |

读到的东西：

- memcpy 三轮一致（单线程 1.60–1.83 ms / 17.4–19.0 GiB/s，16 线程 246–254 GiB/s）→ 内存与
  拷贝路径余量极大，三轮之间可比。
- **samefile vs perfile 是只差"是否同一个文件"的决定性对照**（A vs B，同 `--len`/`--iters`）：

  | threads | samefile agg | perfile agg | 倍数 |
  |---:|---:|---:|---:|
  | 1 | 15.0 | 15.4 | 1.0× |
  | 2 | 15.9 | 30.4 | 1.9× |
  | 4 | 16.1 | 64.1 | 4.0× |
  | 8 | 16.1 | 122.4 | 7.6× |
  | 16 | 16.0 | **194.7** | **12.2×** |

- perfile 聚合随线程数几乎线性涨到 194.7 GiB/s（逼近 memcpy 的 253.7），且 p50 恒定在
  ~1.8–2.0 ms（无排队）；samefile 聚合恒定 16.0 GiB/s，p50 线性涨到 29.707 ms
  （每多一路约 +2 ms，是排队不是变慢）。
- 关键签名：**samefile 的聚合上限 16.0 GiB/s ≈ 单线程 memcpy 的 19.0 GiB/s** —— 16 路并发写
  同一文件，实际退化成"一个核在拷"。这坐实 probe 头部那条判据：代价是同 inode 并发，
  不是拷贝、不是设备。
- `--fallocate` 前后 samefile 两列几乎完全一致（p50 1.87–29.71 vs 2.02–28.77，agg 均 ~16）→
  塌陷与块分配无关。
- `--odirect`（perfile）是**反向优化**：agg 恒定 4.7 GiB/s，p50 6.680→106.978 ms；T=1 比
  buffered 慢 3.3×，T=16 慢 53×（107 vs 2.001）。当轮曾据此误判"设备上限 4.7 GiB/s"，后被
  iostat（dm-0/nvme1n1 `%util` 仅 14–16%、`w_await` 0.25 ms）推翻——4.7 GiB/s 是 O_DIRECT
  串行同步写的速率，不是设备带宽上限。
- `--tmpfs`（perfile）单次 9.3 ms，比磁盘 buffered 的 2.0 ms **慢 4.6×**，16 线程也只有
  49.4 GiB/s（磁盘 perfile 是 194.7）。本轮 tmpfs 不是有效的"纯 page cache 上界"，文档里
  "tmpfs ≈ disk" 的判据在这台机器上不成立（反常，当轮未定位）；脏页节流改用 vmstat 排除
  （约 1.7 TiB RAM，脏页阈值 ~170 GB，每轮只落 1 GiB，不可能触发）。
- 与产品对齐：samefile T=16 的 28.771 ms 与产品 pwrite p50（workers1/2 的 28.07/28.73 ms）
  吻合 → 产品的落盘代价来自同 inode 串行，不是拷贝、内存或设备。

### RM READ direct source 验证

`read-src-010` 与 `read-wr4-001` 已证明 32 MiB Piece 使用 1 个 32 MiB WR 优于 8 个 4 MiB WR。
以下为当时的 page-aligned `MappedPiece` direct registration 复测步骤；当前验证矩阵见上一节。
调度层不在本轮范围内，性能归因只使用 RM READ 的 source、READ WR/CQE、CRC、pwrite、recycle 与 metadata 阶段。

```bash
python3 b7.py prepare --profile read --mode dual --run-id read-src-011 \
  --case read-piece32-cc16-post1-pipe2 --execute
python3 b7.py run --manifest results/read-src-011/manifest.json --execute
python3 read_attribution_summary.py results/read-src-010 results/read-src-011
python3 b7.py cleanup --manifest results/read-src-011/manifest.json --execute
```

新 run 的 Parent sample 行应显示 `direct = 96/0`，warmup 应为 `32/0`，且 `alloc`、`copy#2`
均为 0。若 direct 注册失败，保留结果目录和 Parent provider 错误，不要自动改用复制注册重跑。

新构建会把 measured samples 的细分结果写入
`result.transfer.urmaReadStageSummary`：传输侧含 lane/Offer/destination admission/READ CQE/ReadDone send/Done wait，
Storage 侧含 file open/pwrite/CRC/recycle/metadata commit。`taskTimingSummary` 另含
`dfgetToFirstReadStartNs`、`firstReadStartToFirstPieceNs` 和 `firstReadStartToLastPieceNs`。
`urmaReadTimelineSummary` 使用正常路径事件汇总每个 measured batch 的首末 READ 数据区间、首末
pwrite 区间、两者 envelope overlap、最后 CQE 到最后 pwrite 的距离，以及实际 pwrite 峰值并发；
不需要启用 transport-only profile。`read_attribution_summary.py` 会直接打印这些字段的 p50。

### READ 与 SEND/RECV 同口径时延

新构建的 SEND/RECV 路径会把 measured samples 写入
`result.transfer.urmaSendRecvStageSummary` 和 `urmaSendRecvTimelineSummary`。同时，READ 与
SEND/RECV 的 `taskTimingSummary` 都提供中性字段 `dfgetToFirstTransportStartNs`、
`firstTransportStartToFirstPieceNs`、`firstTransportStartToLastPieceNs`，原 READ 专用字段继续保留。

```bash
python3 transport_timing_summary.py \
  results/read-run/manifest.json results/send-recv-run/manifest.json
```

输出包含完整 dfget、to-transport、transport→首 Piece、首末 Piece span、tail、transport envelope
和 pwrite envelope。阶段耗时是归因数据，不可相加为 batch 墙钟；receive/READ 与 pwrite、CRC 与
pwrite 均可能重叠。所有 envelope 只使用 child 进程时间戳，不比较 Parent/Child 的绝对时间。

## Performance case

`cases.json` 中的 performance case 会真实执行 `warmups` 和 `repetitions`，不是只记录矩阵参数。工具先在
child 启动前完成全部唯一 task 的 parent preheat，再启动 child 并按相同顺序使用
`--disable-back-to-source` 下载，避免 scheduler 在后续 preheat 中反向选择已经活跃的 child。父子同一轮
使用相同 `--tag`，不同轮次使用不同 tag，并分别写入 storage 目录下的 `output.bin.warmup-NNN` 或
`output.bin.sample-NNN`。output 与 Dragonfly content storage 位于同一文件系统且每轮目标唯一，使正常
路径可以 hard link，避免 task cache 命中、目标冲突和跨文件系统的 1 GiB output copy。warmup 结果保存
但不参与汇总，measured sample 输出单轮耗时/吞吐及 min、median、mean、p95、max、aggregate 吞吐。一次
2 warmup + 3 repetitions 的 1 GiB case 会在 parent/child 各创建 5 个独立 task，执行前需要为两侧
隔离 storage 和 run directory 预留足够空间，结束后使用 `cleanup` 回收。

### 单任务 Piece concurrency TCP/URMA 对照

`tcp-piece-cc{1,2,4,8,16,32}-post1-pipe2` 固定一个 `dfget`、1 GiB 文件、16 MiB Piece、1 次 warmup、
3 次 measured task，只改变 `download.concurrentPieceCount`，用于对齐设计台账 12.6 的 `pwritev` 后
URMA 曲线。manifest 中的 task `concurrency` 始终为 1；case 名称里的 `cc` 表示单 task 内的 Piece
concurrency，不是并发 `dfget` 数量。`post1-pipe2-in16` 是保留的 URMA 对照标签，对 TCP 数据路径没有
调参意义。

无 URMA 环境运行 TCP CC 曲线时，使用保存 12.6 对照代码的 RC checkout；无需执行 `discover` 或添加
`--allow-unvalidated-urma`：

```bash
python3 b7.py prepare --profile rc --mode dual \
  --run-id tcp-piece16-cc1-pwritev-001 \
  --case tcp-piece-cc1-post1-pipe2 --execute
python3 b7.py run \
  --manifest results/tcp-piece16-cc1-pwritev-001/manifest.json --execute
python3 b7.py cleanup \
  --manifest results/tcp-piece16-cc1-pwritev-001/manifest.json --execute
```

CC2/4/8/16/32 分别替换 case 和 run ID，每轮完成后立即 cleanup，避免遗留文件影响后续样本。

旧的 `urma-piece-cc{1,2,4,8,16,32}-post1-pipe2` case 使用默认 Piece 大小和 2 次 warmup；12.6 的
16 MiB URMA 数据来自 `urma-piece-16mib-cc{1,2,4,8,16,32}-post1-pipe2`，不要混用两组名称。

URMA 对照示例：

```powershell
python .\b7.py prepare --mode dual --run-id urma-piece-cc8-001 --case urma-piece-cc8-post1-pipe2 --execute
python .\b7.py run --manifest .\results\urma-piece-cc8-001\manifest.json --execute
```

该组默认保留 `maxConcurrentTransfers=16`，用于测量当前默认 URMA transport 的端到端曲线；CC32 代表
Dragonfly 调度侧允许 32 个并发 Piece，不宣称 lane 内同时存在 32 个 native transfer。若默认曲线在 CC16
附近受 transport cap 限制，应另建只修改 `maxConcurrentTransfers` 的对照 case，不能覆盖本组基线。

定位 TX8 admission/pipeline 边界时使用三个只改变 Piece CC 和 TX pool 的正交 case；三者均保持 RX pool
为 32 MiB、`post1-pipe2-in16` 和 `maxConcurrentTransfers=16`：

- `urma-piece-cc8-post1-pipe2-tx16`：TX16 可容纳 8 个 transfer 的双 ring；
- `urma-piece-cc16-post1-pipe2-tx16`：TX16 可容纳 16 个 required ring；
- `urma-piece-cc16-post1-pipe2-tx32`：TX32 可容纳 16 个 transfer 的双 ring。

TX pool 对照完成后，使用以下三个 case 区分 native post batching 和第二 pipeline ring 的影响；除 case 名
指出的轴外，继续保持 `in16`、`maxConcurrentTransfers=16` 和 RX32 MiB：

- `urma-piece-cc8-post8-pipe2-tx16` 对照 CC8/TX16/post1；
- `urma-piece-cc16-post8-pipe2-tx32` 对照 CC16/TX32/post1；
- `urma-piece-cc16-post1-pipe1-tx16` 对照 CC16/TX16/pipe2。

验证 `maxConcurrentTransfers=16` 是否限制 CC32 时，使用以下严格 A/B；两者固定 CC32、post8、pipe1、
in16、TX32、RX32，只改变 MCT：

- `urma-piece-cc32-post8-pipe1-mct16-tx32`；
- `urma-piece-cc32-post8-pipe1-mct32-tx32`。

MCT16 胜出后，固定 CC32、pipe1、in16、MCT16、TX32、RX32，使用以下矩阵定位 native post-list
批处理的饱和点：

- `urma-piece-cc32-post1-pipe1-mct16-tx32`；
- `urma-piece-cc32-post4-pipe1-mct16-tx32`；
- `urma-piece-cc32-post8-pipe1-mct16-tx32`；
- `urma-piece-cc32-post16-pipe1-mct16-tx32`。

定位单任务 Piece 固定生命周期开销时，固定 1 GiB、URMA、CC16、post1、pipe2 和 in16，
只改变 Piece 大小：

- `urma-piece-4mib-cc16-post1-pipe2`；
- `urma-piece-16mib-cc16-post1-pipe2`；
- `urma-piece-32mib-cc16-post1-pipe2`；
- `urma-piece-64mib-cc16-post1-pipe2`。

该快速 sweep 使用 1 次 warmup 和 3 次 measured task。64 MiB 时 1 GiB 文件恰好包含
16 个 Piece，仍能实际填满 CC16；若继续测试 128 MiB 以上，必须同时扩大源文件，避免
Piece 总数不足导致实际并发下降。

若 64 MiB 基线出现较多 optional single-window fallback，使用
`urma-piece-64mib-cc16-post1-pipe2-tx64-rx64` 复测。它保持 Piece、CC、post-list、
pipeline 和 inflight 不变，仅将注册内存扩大为 TX64 MiB + RX64 MiB，用于区分 Piece
大小效应和 registered-window budget 效应。

确认 16 MiB 为 Piece 大小甜点后，使用
`urma-piece-16mib-cc4-post1-in64-pipe2-tx32-rx32` 对齐 demo 的 64 chunk（4 MiB）
application window。该 case 使用 CC4/MCT4、pipeline2，并提供 TX32 MiB + RX32 MiB；
1 GiB 文件仍有 64 个 Piece，不会因 Piece 总数不足降低实际并发。

### 16 MiB Piece 多 lane fan-out 曲线

旧 `fanout-post*` case 没有显式固定 `concurrentPieceCount`，会继承 Dragonfly 默认 CC8，因此不能单独
解释 lane 数的贡献。新的第一组 case 固定每个 Child CC1，并将进程级 MCT 固定为 8；其他参数为
16 MiB Piece、post1、pipe2、in16、TX16 MiB + RX32 MiB，仅改变 lane/Child 数：

- `fanout-piece16-cc1-post1-in16-l1`；
- `fanout-piece16-cc1-post1-in16-l2`；
- `fanout-piece16-cc1-post1-in16-l4`；
- `fanout-piece16-cc1-post1-in16-l8`。

L1 使用普通 queue topology；L2/L4/L8 使用 fanout topology。`maxConcurrentTransfers` 同时限制
进程级持久连接数和活跃 Piece 数，并非 per-lane 配置，因此必须至少为 8。TX16 MiB 可以同时容纳 L8
下每 lane 一个 transfer 的两级 1 MiB window，因此这组用于隔离 lane 扩展，不应出现 TX budget 导致的
ring1 退化。每个 case 使用 1 次 warmup + 3 次 measured batch。

第二组固定每 lane CC8，使用 16 MiB Piece、post1、pipe2、in16，测试多 lane 加 Piece 并发后的
系统饱和能力：

- `fanout-piece16-cc8-post1-in16-l1-tx64`；
- `fanout-piece16-cc8-post1-in16-l2-tx64`；
- `fanout-piece16-cc8-post1-in16-l4-tx64`；
- `fanout-piece16-cc8-post1-in16-l8-tx128`。

L1/L2 使用 MCT32，L4 使用 MCT40，以容纳 32 个 steady transfers 和 persistent-lane Piece 完成时的
短暂 process-admission 交接；三点均使用 TX64 MiB + RX32 MiB。L8 将 steady transfers 扩至 64，
使用 MCT80、TX128 MiB + RX32 MiB。L8 因此是按 lane 数同步扩展 transport/resource ceiling 的饱和点，
不是只改变 lane 的严格单变量实验。建议先按 CC1 的 L2、L4、L8 顺序验证纯 lane 曲线，再跑 CC8 的
L1、L2、L4、L8。当前 dual inventory 会
在 node2 上启动多个隔离 Child daemon，所以结果代表同一物理 Child host 的多 lane/多进程 fan-out，
不能表述为多节点 fan-out。

### transport-only 与 CRC32+pwrite tmpfs 分层对照

以下两个 case 固定 `L8 × 每 lane CC8`、16 MiB Piece、post1、pipe2、in16、MCT80、TX128 MiB +
RX32 MiB，只改变 Child 是否执行 CRC32+pwrite：

- `fanout-piece16-cc8-post1-in16-l8-tx128-transport-only-tmpfs`；
- `fanout-piece16-cc8-post1-in16-l8-tx128-crc32-pwrite-tmpfs`。

两者都把 Parent/Child storage 和 dfget output 放到 inventory 的
`singleHost.tmpfsStorageRoot/<run-id>`（当前为 `/home/y30083740/dragonfly-b7/tmpfs-storage/<run-id>`），prepare
会用 `stat -f` 拒绝并非 tmpfs 的挂载。需要跑这两个 case 时，应先把该 storage root 挂载为 tmpfs，
并确认它至少还能容纳本轮 storage、output hard link 及系统余量。每个 case 为 3 个 measured batch ×
8 lane × 1 GiB，即 24 GiB，不执行 warmup；每个 run 结束立即执行 manifest-owned cleanup。

transport-only 是 validation-only profile：仍执行 TCP control、persistent lane、SEND_IMM/CQE、长度、
Done 和 RX lease recycle，但跳过 Child CRC32 和 pwrite。该 profile 会信任 Parent 提供的 Piece digest，
所以 Child 产物内容是无效的预分配文件；runner 只校验 Parent 内容 SHA、Parent/Child 长度、正常 URMA
lifecycle，以及每个 task 恰好 64 条 transport-only Piece completion，明确不把 Child SHA 当作完整性
证明。普通 `--features urma` binary 不支持这个 profile，测试前必须在两端构建：

manifest 同时保留两种口径：`transfer.summary` / `concurrentSummary` 是 dfget 端到端耗时，会包含
启动、调度、任务文件创建/预分配和收尾；`transfer.urmaServerTransportSpanSummary` 则使用 Parent 同一时钟，
从首个 Piece 进入 URMA server 到最后一个 Piece 完成 Done，只覆盖 Piece 服务区间。与 RDMA
transport-only 对照时应读取后者；RDMA 台账采用三次取最佳值时，使用 `bestThroughputGbps`，若改为
按总字节/总耗时汇总则使用 `aggregateThroughputGbps`。同时保留前者用于说明 Dragonfly 端到端开销。
该口径仍包含 Parent source open/fill、TX Window 等待以及 URMA SEND/CQE，不应表述为纯网卡线速。

```bash
jq '{endToEnd: .result.transfer.concurrentSummary.aggregateThroughputMiBps,
     transportMiBps: .result.transfer.urmaServerTransportSpanSummary.aggregateThroughputMiBps,
     transportGbps: .result.transfer.urmaServerTransportSpanSummary.aggregateThroughputGbps,
     transportBestGbps: .result.transfer.urmaServerTransportSpanSummary.bestThroughputGbps}' \
  results/<run-id>/manifest.json
```

```bash
cargo build --release -p dragonfly-client \
  --features urma-test-failpoints --bin dfdaemon --bin dfget
```

运行示例：

```bash
python3 b7.py prepare --mode dual --run-id urma-l8-transport-tmpfs-001 \
  --case fanout-piece16-cc8-post1-in16-l8-tx128-transport-only-tmpfs --execute
python3 b7.py run --manifest results/urma-l8-transport-tmpfs-001/manifest.json --execute
python3 b7.py cleanup --manifest results/urma-l8-transport-tmpfs-001/manifest.json --execute

python3 b7.py prepare --mode dual --run-id urma-l8-storage-tmpfs-001 \
  --case fanout-piece16-cc8-post1-in16-l8-tx128-crc32-pwrite-tmpfs --execute
python3 b7.py run --manifest results/urma-l8-storage-tmpfs-001/manifest.json --execute
python3 b7.py cleanup --manifest results/urma-l8-storage-tmpfs-001/manifest.json --execute
```

#### single-host 分层复测矩阵（warmup 与 registered budget）

首轮 single-host CC16 分层结果中，transport-only 的 measured sample 从 2307 MiB/s 逐步升至
2868 MiB/s，且出现 1145 次 RX optional single-window fallback；正常 CRC32+pwrite 路径为约
2046 MiB/s。为避免把冷启动和 optional Window 压力混入 CRC/Storage 结论，保留上述历史 case，新增两对
`1 warmup + 5 measured` case：

```text
# 保持首轮 TX48 MiB + RX48 MiB，只修正 warmup/repetition。
urma-piece16-cc16-post1-in16-pipe2-tx48-rx48-warm1-transport-only-tmpfs
urma-piece16-cc16-post1-in16-pipe2-tx48-rx48-warm1-crc32-pwrite-tmpfs

# 扩为 TX64 MiB + RX96 MiB，检查 optional pressure 是否影响分层差值。
urma-piece16-cc16-post1-in16-pipe2-tx64-rx96-warm1-transport-only-tmpfs
urma-piece16-cc16-post1-in16-pipe2-tx64-rx96-warm1-crc32-pwrite-tmpfs
```

四组都固定 1 GiB、16 MiB Piece、CC16、post1、in16、pipe2、MCT16 和 tmpfs。第一对总注册预算
96 MiB、TX48 MiB，隐含 RX48 MiB；第二对总预算 160 MiB、TX64 MiB，隐含 RX96 MiB。建议用不同
run-id 按 `transport/base → storage/base → storage/roomy → transport/roomy` 执行，全部 cleanup 后再反向
重复一轮。只有同一预算内的 transport/storage 可以直接计算 CRC32+pwrite 差值；base/roomy 之间用于判断
registered budget 和 optional Window pressure。

single-host 示例：

```bash
python3 b7.py prepare --profile rm --mode single --host node1 \
  --run-id rm-layer-base-transport-001 \
  --case urma-piece16-cc16-post1-in16-pipe2-tx48-rx48-warm1-transport-only-tmpfs \
  --execute
python3 b7.py run \
  --manifest results/rm-layer-base-transport-001/manifest.json --execute
python3 b7.py cleanup \
  --manifest results/rm-layer-base-transport-001/manifest.json --execute
```

其余三组只替换 run-id 和 case。每轮至少记录 measured sample、aggregate/mean/p50/p95、
`startToFirstPiece`、`firstToLastPiece`、TX/RX optional pressure、single-ring/window fallback、TCP fallback、
session retirement 和 unexpected shutdown error。

PR #1945 的 RDMA concurrency 曲线不是多 lane/QP 曲线：一个 daemon 只创建一个共享
`FI_EP_RDM` endpoint、共享 CQ/progress thread；每个 Piece 各自建立 TCP rendezvous，再以独立 tag 在同一
RDM endpoint 上并发。URMA 上最接近的严格结构对照是单 Parent + 单 Child persistent lane 内的并发
Piece；本节 L8×CC8 用于测量 URMA 的多 lane 总饱和能力，结果必须同时报告 lane=8、每 lane CC8，
不能直接标成“对齐 RDMA concurrency=8”。

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

普通 `topology: queue` 并发 case 只建立“上层并发、单 lane”吞吐基线，不把同 lane 的 Piece overlap
作为 PASS 条件。多 Child fan-out 和多 Parent fan-in 使用独立多 role manifest，不能用 queue 模式冒充
多 lane transport concurrency。

### 单 lane 并发 Piece（B8.4）

`topology: piece-concurrency` 仍只启动一个 Parent 和一个 Child，但把并发 Piece 证据升级为 correctness
gate。每个 batch 的多个独立 task 经同一 barrier 同时启动，工具从 Parent 日志按顺序配对：

- `start upload piece content over urma` 的 `task_id`、`lane_id`、`transfer_id`；
- `urma piece finished on peer lane role="server"` 的 `lane_id`、`transfer_id`。

PASS 要求每个 task 均有 Piece start、所有 `(lane_id, transfer_id)` 都完成且无重复、全 batch 只使用一个非零 lane，并且
至少两个不同 task 的 Piece 生命周期在该 lane 上重叠。这样不会把“先跑完 task A 再复用 lane 跑 task B”
误判为并发。manifest 在每个 batch 保存 `pieceConcurrencyEvidence`，并汇总
`pieceConcurrencyDiagnostics` 与 `pieceConcurrencyValidation`。

基础 `piece-concurrency-*` case 只要求并发 rendezvous/storage/Piece 生命周期和独立
`transfer_id`。支持 lane-global SEND_IMM dispatcher 的客户端还会输出每个 RX window 和 Piece 的
`reordered_chunk_count`、`cross_transfer_chunk_count` 汇总；runner 将其写入
`sendImmRouting`，但基础 case 不把 native RX window 并行作为 PASS 条件。

先运行 c2，再运行 c4：

```powershell
python .\b7.py prepare --mode dual --run-id b84-piece-c2 --case piece-concurrency-post1-in32-c2 --execute
python .\b7.py run --manifest .\results\b84-piece-c2\manifest.json --execute
python .\b7.py prepare --mode dual --run-id b84-piece-c4 --case piece-concurrency-post1-in32-c4 --execute
python .\b7.py run --manifest .\results\b84-piece-c4\manifest.json --execute
```

### 单 lane native RX window 并发（B8.6）

`piece-native-rx-*` 在上述 Piece overlap gate 之上要求 SEND_IMM window/Piece 汇总完整一致，并解析
`URMA native RX window admitted/released` 生命周期。PASS 要求 admission/release 完整配对、无重复或
遗留 window，并且同一 lane 的 `maxActiveTransfers >= 2`。`crossTransferChunkCount` 继续记录某个
transfer 的 SEND 落入另一 transfer 发布的 RX slot，但 provider 可以保持同 transfer 匹配，因此它只是
路由观测指标，不再作为 native RX window 并发的必要条件。

先运行 c2，再运行 c4：

```powershell
python .\b7.py prepare --mode dual --run-id b86-native-rx-c2 --case piece-native-rx-post1-in32-c2 --execute
python .\b7.py run --manifest .\results\b86-native-rx-c2\manifest.json --execute
python .\b7.py prepare --mode dual --run-id b86-native-rx-c4 --case piece-native-rx-post1-in32-c4 --execute
python .\b7.py run --manifest .\results\b86-native-rx-c4\manifest.json --execute
```

### TX fan-out（B7.2）

`topology: fanout` 将 `concurrency` 解释为 Child daemon/lane 数，而不是同一 Child 内的 task 数。工具在
node1 启动一个 Parent，在 node2 启动 `child-001..NNN` 多个隔离 daemon；每个 Child 都有独立的
hostname、socket、端口组、storage 和日志。每个 batch 为每个 Child 分配一个唯一 task，再由 node2 上
一个 host-local barrier 同时释放所有 `dfget`。这样 Parent 侧的 lane 才会真实共享同一个 TX registered
pool、owner thread 和 JFC。

第一轮内置 case：

- `fanout-post1-in32-l2`：低压力双 lane correctness/throughput；
- `fanout-post1-in32-l4`：四条 required 32-slot window 恰好占满默认 128-slot TX pool；
- `fanout-post8-in64-l2`：两条 required 64-slot window 恰好占满默认 TX pool；
- `fanout-post1-in32-l4-pipe1-tx8`：关闭 optional 第二窗口，四条 required window 使用 8 MiB TX；
- `fanout-post1-in32-l4-pipe2-tx16`：保留双窗口，将总注册预算/TX 调为 48/16 MiB，并保持 RX 32 MiB。

先只运行 l2：

```powershell
python .\b7.py prepare --mode dual --run-id b72-fanout-post1-l2 --case fanout-post1-in32-l2 --execute
python .\b7.py run --manifest .\results\b72-fanout-post1-l2\manifest.json --execute
```

除 B7.1 的 batch 汇总外，每个 `transfer.batches.*[]` 还包含 `laneEvidence`。工具从 Parent 的
`start upload piece content over urma` 行提取最内层 `task_id`/`lane_id`，记录 `laneIdsByTask`、
`pieceAttemptsByTaskAndLane`、`churnTaskIds` 和 `stableLaneByTask`。缺 lane、同 task lane churn、未绑定
lane 0、同 batch task 复用 lane 或同一 role 跨 batch 换 lane都会记入 `fanoutValidation.failures`。

lane 校验失败不会再中断第一个 batch。runner 会完成剩余 batch、全量日志/metrics 采集和有序 shutdown，
最后统一返回失败。`fanoutDiagnostics` 分开保存 TX required/optional budget pressure、
BufferUnavailable、BUSY/reject、session retirement 和 TCP fallback；因此失败 manifest 也可用于归因。
其中 `sendCompletion` 汇总 Parent 每个 PeerTarget 的 `urma SEND completion summary`，
用 `sendsPerCqe` 验证 TX CQ moderation 是否按 `sendCompletionInterval` 收敛；
`storageConsumer` 汇总 Child 每个 Piece 的 `digest_ns`/`pwrite_ns`/`rx_window_wait_ns`/`recycle_ns`
相对 `storage_total_ns` 的占比，并保留 transport-only 的 `transport_only_ns` 作为去存储基线。
`taskScopedEvidence` 分别记录 Parent 和每个 Child 的证据文件。

当前 fan-out runner 要求所有 Child 位于同一节点，以便使用单一远端 barrier；这正好覆盖当前
node1 Parent / node2 Children 的实验环境。`fanout-post1-in32-l4` 和 `fanout-post8-in64-l2` 都位于默认
TX required-window 预算边界，应在 l2 基础 case 通过后再执行，并重点检查 required/optional budget
pressure、fallback 和跨 lane fairness。RX fan-in 使用一个 Parent client 同时从多个隔离 Child server
拉取唯一 task，验证共享 RX pool、并发 lane、公平性和 fallback。

若默认 l4 出现 lane churn，按顺序运行 `fanout-post1-in32-l4-pipe1-tx8` 和
`fanout-post1-in32-l4-pipe2-tx16`。两者都通过说明默认 l4 是 optional window 抢占 required admission；
pipe1 仍失败说明 8 MiB required 边界本身缺少等待/公平性；只有 pipe2 失败则需继续检查 depth2 的多 lane
生命周期。

fan-in 先运行 `fanin-post1-in32-l1-pipe1` 和 `fanin-post1-in32-l1-pipe2`
建立单 lane 的单/双 window 方向性基线，再运行
`fanin-post1-in32-l2-pipe1`、`fanin-post1-in32-l2` 和 `fanin-post1-in32-l4`
建立多 lane 基线，最后依次运行
`fanin-post1-in32-l4-pipe1-rx8`、`fanin-post1-in32-l4-pipe2-rx16` 和
`fanin-post1-in32-l4-pipe2-rx8`。前两组预算 case 分别验证 4 个 required RX window 和 4 条双 window
pipeline 的充足预算；最后一组验证 RX8 下 optional window 能否受控退化而不造成 session retirement 或
TCP fallback。`faninDiagnostics` 从 Parent client 记录 RX required/optional pressure、BufferUnavailable、
single-window fallback 和 session 健康，并按 Child server 分开保留 TX pressure。

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
`child.shutdown.log`。受控停机引发的 peer `early eof` 或对端退出后关闭 incoming transfer queue，统一计入
`peerCloseEvents`，并分别保留 `earlyEofEvents` 和 `controlQueueClosedEvents`；其他 CQE、completion、
protocol、digest、Jetty 或 panic 错误会使本轮失败。该放行只应用于发送 SIGTERM 前记录 offset 之后的
shutdown 日志，不会放宽传输阶段的 correctness gate。

连续运行会复用 B7 的固定端口组。启动失败时工具会回收已拉起但尚未记入 manifest
`started` 列表的 daemon；正常停止后会等待 TCP/UDP 端口退出监听和 TCP teardown，
`prepare` 也会检查 UDP/QUIC 占用并等待上一轮端口可复用，避免紧接着启动时出现
`address already in use`。

证据分析还会固定方向：parent preheat 日志中出现任何从 peer 下载的 Piece，或 child 的 URMA Piece
来自非预期 parent，都会以 `topology contamination` 失败。`urma download failed, fall back to tcp
downloader` 及 parent penalty 文本同样作为真实 fallback 处理。

## 当前限制与后续层

当前 `run` 支持 standard-task correctness、顺序 performance repetitions、同一 parent/child 上的并发
task batch、单 lane 并发 Piece correctness gate、一个 Parent/多个隔离 Child 的 TX fan-out，以及多个
Child server/一个 Parent client 的 RX fan-in：唯一 origin、逐 task 预热、`--disable-back-to-source`、逐 task
三方 SHA-256、固定拓扑与 lane-ID 校验、task-ID scoped 日志、URMA 日志/metrics 证据以及有序 shutdown。后续仍需增加
persistent/persistent-cache、failpoint、双 lane 定向中断和带 outstanding WR 的专项 shutdown case。

## 完整普通文件 Cached 注册对照（2026-10-19）

当前 READ profile 的目标分支为 `urma-cached-source`。Parent/Child 都需要同步并重新构建本轮
`dfdaemon`/`dfget`；不要沿用此前 cleanup 分支的二进制。READ checkout 的两个目录别名继续允许，
实际路径由 `inventory.json` 的 `nodes.*.readRepo` 指定；若远端分支不同，请显式修改
`urma.profiles.read.expectedBranch`，不要跳过预检。

新增三个 case，均为 1 GiB 文件、32 MiB Piece、Piece 并发 16、sourceWorkers=0、
1 次 warmup + 3 次 sample；保持 SHA256、零 TCP fallback 和现有 READ 生命周期校验：

| case | registration.mode | maxRegionBytes | 每个 Task 使用的 Source 数 |
|---|---|---|---:|
| `read-registration-piece32-cc16` | piece | 1GiB | 32 个独立 Piece Source |
| `read-cached-file-piece32-cc16` | cached | 1GiB | 1 |
| `read-cached-region256-piece32-cc16` | cached | 256MiB | 4 |

Parent 先完整下载所有 Task，随后 Child 才加入并下载。这组测试覆盖完整普通文件的
首次注册、并发 miss 合并及文件内跨 Piece 复用；**不覆盖未完成文件的 Piece 回退或 GC 竞态**。
每个 batch 仍使用独立 TaskID/文件实例，warmup 不会为 sample 预热同一个 Source。
因此它比较的是每个新文件的完整传输成本，不能当作“跨批次纯热缓存吞吐”。

```bash
for case in \
  read-registration-piece32-cc16 \
  read-cached-file-piece32-cc16 \
  read-cached-region256-piece32-cc16
do
  run_id="${case}-001"
  python3 b7.py prepare \
    --profile read --mode dual \
    --run-id "$run_id" --case "$case" \
    --parent-cpus 192-223 \
    --child-cpus 192-223 \
    --execute || break

  python3 b7.py run \
    --manifest "results/$run_id/manifest.json" \
    --execute || break
done

python3 read_attribution_summary.py \
  results/read-registration-piece32-cc16-001 \
  results/read-cached-file-piece32-cc16-001 \
  results/read-cached-region256-piece32-cc16-001
```

新 case 会生成 `storage.server.urma.read.registration.mode/maxRegionBytes`。
原 READ case 会显式生成 `mode: piece`，避免基础 YAML 中遗留的 Cached 设置污染基线。
Parent 的 `mmapContent` 由现有角色 overlay 打开。

Cached case 额外校验每个 Task 的全部 32 个 Piece 均从 Cached Source 完成，并从
Parent 完成日志的 `source_id` 提取 registry/sequence，核对成功使用的 Source 数为 1 或 4。
缺失身份、走了旧 Piece 路径、每 Piece 都注册或计数不完整都会失败。
该证据写入 `result.transfer.{warmups,samples}[*].child.sourceCacheEvidence`。
Source 身份只用于诊断，不记录 token 或 descriptor 凭证。

汇总新增 `parent Source cache` 表：

- `cached`：Cached 服务的 Piece 数。
- `sources`：成功服务过 Piece 的不同 Source 身份数；**不是 native 注册 API 调用总数**。
- `reuse`：`cached - sources`，表示同 Source 服务的额外 Piece；不是严格的 lookup-hit 次数，
  等待同一次并发注册的 Piece 也计入其中。
- `noID`：缺失 Source 身份的 Piece 数，非零时不能证明复用。
- `leases` / `lease ms`：缓存传输租约的完成次数/耗时，不能算作 Segment 注销。
- `adm ms`：缓存查找、完整性检查及注册等待阶段的耗时；缓存租约的零值 shim pin 字段
  不再混入 native 注册耗时分布。

每组 3 个 measured Task，整文件模式通常显示 `cached=96, sources=3, reuse=93`；
256 MiB Region 模式显示 `cached=96, sources=12, reuse=84`。warmup 单列。
当前工具尚未证明跨批次热缓存、Segment 淘汰/注销或多人环境下的性能收益。


### SEND/RECV 控制通道对照

两端需更新到支持 **DFUR READ v7** 的二进制；v6 及更早版本不兼容。
默认 `controlTransport: tcp`，新 case 显式设为 `sendrecv`，`controlBytes: 16MiB`。
TCP 只负责握手交换 descriptor 和连接断开检测；握手后控制消息走 SEND/RECV，
大块数据仍走 READ。活动会话不会悄悄切回 TCP 控制。

控制 Jetty/CQ、收发 Buffer 独立于 READ，32 个 4 KiB RX Slot + 1 个 TX Slot，
每条 lane 注册 132 KiB，预算耗尽拒绝新控制端点。TCP 基线 case 显式覆盖为 tcp，
无需修改基础 config 模板。两端仍须同时升级，即使本次运行选 TCP。

新增 case 保留 SHA256、零 fallback 和 READ 生命周期校验；Cached case 另保留复用校验。
运行后还会核对 Parent/Child 原始日志中的 `control_transport=Sendrecv`，
缺失任一端的实际协商证据都会失败。结果保存在 `result.controlEvidence`。

```bash
for case in \
  read-registration-piece32-cc16 \
  read-sendrecv-piece32-cc16 \
  read-cached-region256-piece32-cc16 \
  read-cached-region256-sendrecv-piece32-cc16
do
  run_id="${case}-001"
  python3 b7.py prepare \
    --profile read --mode dual \
    --run-id "$run_id" --case "$case" \
    --parent-cpus 192-223 \
    --child-cpus 192-223 \
    --execute || break
  python3 b7.py run \
    --manifest "results/$run_id/manifest.json" \
    --execute || break
done
```

先单独跑一个 SEND/RECV case 确认能握手、完成和安全关停，再进行上述同配置 A/B。
本轮仅离线验证；真实 provider 的收发、低 credit/接收拥塞、取消/flush、
同 Task 多 Child 与 1→7/7→1 仍待真机验收。不要把离线测试通过当作硬件路径通过。

### READ 性能汇总口径与 native 时序（2026-10-20）

更新工具后可直接重算已有 run，不用更新二进制或重新跑测试：

```bash
python3 read_attribution_summary.py \
  results/read-sendrecv-piece32-cc16-002 \
  results/read-cached-region256-sendrecv-piece32-cc16
```

只指定需要的 run，避免无意遍历整个 results。脚本从已有 measured task-scoped
日志补齐 native 字段；旧 manifest 没有 batch 索引时，只使用明确的
`child*.sample-*.log`，不把 warmup 或合并后的全 run 日志当作 measured。
部分日志缺失时保留 manifest 的完整 transport/storage 聚合，native 表的 `n`
仅表示本地可解析 data-completion 事件数，可能小于完整样本数。

显示名修正，原始 JSON 数值及历史字段名保持兼容：

| 旧显示 | 新显示 | 实际含义 |
|---|---|---|
| toREAD / READ→1 / READspan | toTry / try→1 / trySpan | Piece attempt 起点；发生在握手、准备和实际 READ post 之前；trySpan 含 Storage |
| piece ms / rate | pieceGap / gapRate | 首片完成到末片完成；gapRate 分母遗漏首片前工作，不是实际链路吞吐 |
| READ / READenv | rdDrv / drvEnv | 完整软件 drive_reads 阶段及批次包络 |
| CQEspan | doneSpan | 软件 data-completion 日志时间跨度，不是硬件 CQE 时间 |
| avgRd / peakRd / busy% | avgDrv / peakDrv / active% | 软件 driver 区间重叠与覆盖率，不是硬件在途或网卡利用率 |
| idle | drvIdle | 批次包络内无软件 driver 活跃的区间，不是网卡空闲 |
| 1CQE→pwr / lastCQE→end | 1done→pwr / lastdone→end | 从软件完成日志重建的 Storage 关系 |
| doneTx | doneEnq | ReadDone 进入 writer channel，尚未等待实际 SEND CQE |
| pkRet | dstRet | Child destination pool 保留峰值，不是 Parent Source cache |
| adm ms / lease ms | cacheAdm / leaseEnq | 缓存查询/注册等待；租约释放及 Done 入队，不是 native 注销或控制 RTT |

`cacheAdm` 不包含前面的 Storage metadata/当前文件 FD 打开；Cached 的
`source_open_ns=0` 也不能说明没有打开文件。

新增 `child READ driver/native attribution` 表，仅从
`urma READ child completed data transfer` 解析，避免与随后重复字段的
`child finished transfer` 日志重复计数：

| 列 | 日志字段 / 边界 |
|---|---|
| n / timedWRs | data-completion Piece 事件数 / 这些事件的 native_timed_wr_count 总数 |
| driver | read_completion_ns，含订阅、提交、DMA、poll 和异步通知 |
| sub | progress_subscribe_ns，首次订阅命令往返 |
| admQ / ownerQ | post_admission_wait_ns / post_queue_wait_ns |
| postCmd | post_command_ns，含 admission、排队、执行及回复调度 |
| postAPI | native_post_call_ns，native post 调用累计时间 |
| nativeEnv | native_read_envelope_ns，首 native post 到末匹配 CQE 的主机观察区间 |
| cqeObs | cqe_to_observed_ns，末 CQE poll 返回到 driver 观察完成 |
| wake | completion_notification_delay_ns，通知发布到 driver 观察完成 |

全部时长列按各自 measured 样本给 p50，**重叠且不能相加，也不能拿不同字段的
中位数相减当作精确剩余时间**。nativeEnv 包含 provider/DMA/网络和 owner poll
延迟，不能称纯 wire 时间；postAPI 已包含在它之内。cqeObs 包含 wake。

旧日志缺字段显示 `-`，真实测得的零仍显示 `0.00`；显式
`native_timed_wr_count=0` 时，不把零 native 时长显示为成功测得的传输。
`--json` 的 `read_stages.readDriver` 保留每字段的 count，另有
nativePostToCqeSumNs/nativePostToCqeMaxNs/progressWaitNs；多 WR 的 sum 可以重叠，
progressWaitNs 也含正常 DMA 等待，不能解释成纯软件 CPU 开销。

新 manifest 和汇总 JSON 增加 `readIntervalScope: piece-driver`、
`readConcurrencyIsHardwareWrCount: false`（timeline）及
`stageDurationsAreAdditive: false`。旧 `readCqe*` / `readActive*` 等 JSON key
保留历史名称，其含义仍是软件区间。已有结果文件不会被重写。

### B/C：热 Cached READ 带宽验证（独立于正式下载）

新增 `read_bandwidth_probe.py`，在两端**分别本地运行**，不使用旧 SEND_IMM
`transport-only` case。它构建当前 checkout 的 release Storage lib-test 可执行文件，
再直接调用一个明确的 ignored 真机入口；没有该入口的旧 binary 不会被当作成功。

| 模式 | 计时区间 | 保留/排除 |
|---|---|---|
| B | 持续 native post/poll，停止补投后排空所有在途 WR | 生产 shim、file-backed mmap、共享 TABLE token ID、32 MiB READ、16个独立目的Segment；注册/import只在准备阶段，无Fabric和逐片控制 |
| C | 持续 Cached Piece 生命周期，含末批排空、Done和Buffer回收 | 生产Fabric、SEND/RECV、当前FD重开/身份检查、lookup-only cache hit、lease acquire/expose/release、Child import/unimport、READ、Done；不做Child Storage/CRC/元数据提交 |

两组默认都使用已填充的1 GiB文件、256 MiB Region、32 MiB Piece、深度/并发16，
1s预热+10s测量。Parent先计算每片CRC32并注册全部Region；Child在计时前后各遍历
完整文件并核对CRC32。计时期间不计算数据CRC，但仍保留长度、offset、单WR完成及
身份/状态校验；查不到已预热Source立即失败，不走冷注册或TCP bulk fallback。
目的Buffer均在测量前热好。建议使用原B7输入文件，保持inode/内容不变，不用只truncate
出来的稀疏文件代替已填充文件。

**测量范围：** B刻意busy-poll，与perftest接近；C保留现有Fabric owner轮询和
Tokio调度，默认32个Tokio worker。C复用生产传输核心，但文件完整性/Piece digest
元数据来自固定fixture，不运行scheduler、RocksDB Task/Piece查询、完整dfdaemon启动
或应用日志配置。因此C是传输核心基线，不是“从正式下载只删除pwrite”的精确A/B。
B/C都接近perftest时，只能把剩余差距定位到更高层集成（包括Storage），不能单凭此
把全部差距归因于Storage。

#### 两端先同步代码并完成离线构建

在 Parent 和 Child 的 `urma-b7` 目录各执行一次；`repo` 改成这台节点实际的新代码目录。
此步骤不打开provider、不绑定控制端口：

```bash
repo=/home/y30083740/dragonfly/urma-cached-source
python3 read_bandwidth_probe.py \
  --repo "$repo" --mode B --role child --addr 10.10.10.2:31918 \
  --log results/read-bw-build-001/build.log \
  --build-only --execute
```

两端都构建完再启动Parent，避免首次编译让对端等超时。脚本使用现有UMDK编译/链接
环境（UMDK_INCLUDE_DIR/UMDK_LIB_DIR/LD_LIBRARY_PATH按节点原有设置），没有新增crate依赖。
后续run仍检查增量构建，**编译时间不计入带宽**。

#### B：Parent先启动，Child随后启动

Parent节点：

```bash
repo=/home/y30083740/dragonfly/urma-cached-source
python3 read_bandwidth_probe.py \
  --repo "$repo" --mode B --role parent --addr 0.0.0.0:31918 \
  --file /home/y30083740/dragonfly-b7/origin/input-1g.bin \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-001/parent.log --execute
```

Child节点：

```bash
repo=/home/y30083740/dragonfly/urma-cached-source
python3 read_bandwidth_probe.py \
  --repo "$repo" --mode B --role child --addr 10.10.10.2:31918 \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-001/child.log --execute
```

**C：**两端B都结束后，将上述命令中的`--mode B`改为`--mode C`，日志路径改为
`results/read-bw-C-001/{parent,child}.log`，其余保持一致，再先Parent后Child。
需使用两台真实节点，不能用同EID单机loopback替代。

显式参数还包括 `--file-bytes`、`--piece-bytes`、`--region-bytes`、`--depth`、
`--seconds`、`--warmup-seconds`、`--tokio-threads`；单位为整数bytes/seconds。
两端的模式、几何、深度和时长必须相同，初始化先核对签名。Child不需要输入文件。
不带`--execute`只打印构建/运行参数。旧日志不会被覆盖，重跑使用新路径。

#### 看结果

```bash
grep -E 'URMA_BW_(CONFIG|SOURCE_READY|RESULT|PASS)' \
  results/read-bw-B-001/child.log
```

Child只有在前后完整文件CRC32校验、全部WR/lease回收、Child runtime关闭和Parent
关闭确认均成功后才输出`URMA_BW_RESULT`。`GiBps`为完成字节/实际测量耗时，
`Gbps`为十进制bit/s；两者来自同一计时，不含setup/校验/warmup。耗时包含截止后
已提交工作排空，因此可能略长于10s，不只统计post数量。Parent不输出本地伪造带宽。

两端均须有`URMA_BW_PASS ... integrity=all-pieces-before-and-after shutdown=both-sides`。
launcher另要求进程退出0；即使Cargo返回0但实际没有运行匹配的测试，也判失败。
每个日志旁的`.log.json`记录实际test binary路径/SHA256、git HEAD/dirty、参数和结果，
复制代码目录没有 Git、或 `.git` 指向另一台机器的 worktree 时仍可运行：
记录 `gitAvailable=false`、`gitHead=null`、`gitDirty=null`，binary SHA256 仍必填。
没有向正式Storage发布成功Task或伪造文件产物。失败/不确定post不生成成功带宽；
不要把超时、EOF或强制杀进程当作drain证明。

与A比较时保持READ、RM/CTP、同设备/EID/NUMA、32 MiB、单Jetty、depth16、post-list1、
CQ每WR通知一致；taskset只证明CPU范围，不证明内存NUMA落点。各组单独运行，
比较平均/稳态带宽。B显著低于A还可能来自file-backed/页布局、shim或poll/post；
C显著低于B重点检查控制、owner队列和逐片生命周期。不要将B busy-poll结果当作正式
dfdaemon无需占CPU就能达到的保证，也不要将这个热缓存结果与每batch新Task的B7
冷Source注册成本混为一谈。


#### B 固定源工作集对照

新增 `--source-pattern fixed`（仅 B 支持，默认 `sequential`）。两端必须一致；
初始化签名核对模式，launcher还要求实际binary的CONFIG明确输出`source_pattern=fixed`，
防止旧Rust入口忽略新环境变量却被当作固定源结果。旧顺序模式命令仍然可用。

只改变预热和计时阶段：所有READ反复读Piece0的32 MiB源范围；保留整个1 GiB文件、
四个256 MiB注册Region、全部import及16个独立目的Buffer（512 MiB），不共用目的地址。
前后校验阶段仍遍历全部Piece，校验/注册/shutdown门禁保持。此对照只缩小源访问工作集，
不能单独区分CPU缓存、地址翻译和内存带宽，也不复刻perftest的共享目的Buffer。

两端同步`read_bandwidth_probe.py`及Rust `src/urma/read_bandwidth_probe.rs`，先按上文
build-only完成构建，再启动Parent和Child。使用原来的1 GiB已填充文件，不需要新建32 MiB文件。

Parent：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --role parent --addr 0.0.0.0:31918 \
  --file /home/y30083740/dragonfly-b7/origin/input-1g.bin \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-fixed-001/parent.log --execute
```

Child：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --role child --addr 10.10.10.2:31918 \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-fixed-001/child.log --execute
```

与默认`--source-pattern sequential`交替跑三轮、各用新日志路径。如果fixed明显提高，
支持源工作集是差距因素；仍不能把剩余差距直接归因Rust/shim。若差异不大，再检查目的
工作集、NUMA和native post/poll，先不修改生产控制/Storage实现。


#### B native 汇总观测

B在成功校验和双方关闭后，额外输出一行`URMA_BW_NATIVE`；只汇总measured阶段，
不混入预热或前后完整性校验，不输出逐WR日志。原launcher JSON带宽字段保持不变。
同步新版Rust探针即可；旧命令均可用，重跑换新日志路径。

```bash
grep -E 'URMA_BW_(CONFIG|RESULT|NATIVE|PASS)' \
  results/read-bw-B-fixed-002/child.log
```

| 字段 | 含义 |
|---|---|
| avg_unretired / peak_unretired | 已接受post起点至匹配CQE被poll观察的WR时间加权平均/峰值 |
| full_window_pct / zero_unretired_pct | 上述未退休计数等于depth/为0的时间比例，含启动和最后排空 |
| polls / empty_polls / empty_poll_pct | poll调用数、返回0次数及其调用比例；不是CPU空闲或链路利用率 |
| post_call_ms / post_call_mean_us / post_call_max_us | 既有ReadPostTiming包围的生产shim post调用累计/均值/最大值，含检查和WR设置，不含外围HashMap/空闲Slot管理 |
| post_to_cqe_mean_us / min_us / max_us | post调用起点到匹配CQE的主机poll观察时间，含post调用和poll延迟，不是纯DMA/wire时间 |
| refill_count / refill_mean_us / refill_max_us | 已观察CQE的目的Slot到下一次用于post的间隔；不含初始空Slot及最终未再复用Slot |

`avg_unretired`按时长加权，不是循环采样均值；设备可能已完成但CQE尚未poll，
所以这些计数不能称硬件队列占用/NIC busy%。`post_to_cqe`跨WR可以重叠，
与post_call不能相加；empty_poll_pct高在busy-poll下可以完全正常。所有观测使用
既有post/poll时间戳，空poll仅递增计数，不额外读钟；仍有少量汇总计算开销。

先比较avg_unretired/满窗口时间和refill，再看post开销：长期接近depth且refill很短，
会降低“补投不及时”的优先级，但不能仅凭软件未退休深度排除所有软件影响。


### perftest 独立目的范围对照（只用于实验）

新增`build_read_perftest_slots.py`和`read_perftest_slots.h`。构建器复制当前UMDK的
perftest源码至新输出目录，严格匹配并修改两个文件，再生成独立的
`urma_perftest_read_slots`；不修改UMDK原源码或安装的`urma_perftest`。源码不匹配直接失败，
输出目录不覆盖。保存原始/修改后源码SHA256、构建命令及binary SHA256到`build.json`。

同一个新binary用`URMA_BW_DST_LAYOUT=shared|slots`选择：

| 项目 | shared | slots |
|---|---|---|
| 源访问 | 固定32 MiB | 固定32 MiB |
| 目的访问 | 全部在途READ反复写同一段32 MiB | 16个独立32 MiB范围，共512 MiB |
| 实际分配/注册 | 两端均为一块544 MiB匿名Segment，提前写入触页 | 相同 |
| WR管理 | 相同的16 Slot分配/CQE回收逻辑 | 相同 |

两种模式均保留perftest原post/poll主循环、RM/CTP、单Jetty、depth16、post-list1、
CQmod1、poll batch16及原持续时间统计。user_ctx改为Slot+1，按匹配CQE释放确切Slot，
不依赖完成顺序；只有成功CQE才能推进正常测试。拒绝非read_bw、非32 MiB、不同深度、
双向、事件poll、credit/notify、随机大小、hugepage/固定VA等超出本次实验范围的模式。

**边界：**新shared也扩展了注册范围并增加首次触页/Slot管理，因此必须先确认它仍接近
原版perftest，才能把shared→slots差距归因目的访问范围。新slots是一个Segment里的
16个独立范围，B是16个独立Segment；仍有注册映射/NUMA/源内存类型等差异。
此工具保持perftest原内容校验能力，不新增全文件CRC或双方安全关闭门禁，不能称为
B/C完整性验证通过，也不能替代正式下载正确性测试。输出明确标`integrity=not-checked`。
原perftest duration包含预热/统计/结束阶段，`-D10`的统计窗口约5s；不改这个口径。

#### 两端先构建

在两端urma-b7目录运行；`UMDK_ROOT`填这台节点UMDK源码根目录（应包含
`src/urma/tools/urma_perftest`）。链接库应与原版perftest相同：先用`ldd "$(command -v urma_perftest)"`
核对liburma/liburma_common。默认沿用UMDK_LIB_DIR，否则查UMDK的build-perftest；
可显式传`--lib-dir`和`--common-lib-dir`，例如两者均在`/usr/lib64`。

```bash
UMDK_ROOT=/实际的/umdk源码目录
python3 build_read_perftest_slots.py \
  --umdk "$UMDK_ROOT" \
  --output results/read-perftest-dst-build-001 \
  --execute
ldd results/read-perftest-dst-build-001/urma_perftest_read_slots
```

如果匹配库在系统目录，构建时增加：

```text
--lib-dir /usr/lib64 --common-lib-dir /usr/lib64
```

默认dry-run只校验/打印构建计划，不打开provider；build也不打开provider。保存源码目录
供审查；不需要sudo安装，也不用重新构建整个UMDK。

#### 先shared，再slots；每组先Parent后Child

Parent：

```bash
URMA_BW_DST_LAYOUT=shared taskset -c 192-223 \
  results/read-perftest-dst-build-001/urma_perftest_read_slots read_bw \
  -d udmac0d1e2 --eid_idx 0 --ctp -p 0 \
  -s 33554432 -J 1 -T 16 -l 1 -Q 1 -D 10 -w -N
```

Child：

```bash
URMA_BW_DST_LAYOUT=shared taskset -c 192-223 \
  results/read-perftest-dst-build-001/urma_perftest_read_slots read_bw \
  -d udmac0d1e2 --eid_idx 0 --ctp -p 0 \
  -s 33554432 -J 1 -T 16 -l 1 -Q 1 -D 10 -w -N \
  -S 10.10.10.2
```

两端都退出后，仅将shared改slots重跑，其余不变。贴两端`URMA_PERFTEST_DST`及Child
BW average、进程退出状态。相邻跑原版/shared/slots，建议三轮，以确认环境波动。
若shared仍约400 Gbps而slots明显下降，支持目的工作集是差距因素；若两者均降低，
不能单独归因目的范围，应先排查扩大注册/首次触页、库版本及NUMA差异。


### B 单 Segment 目的窗口对照（无需 perftest 源码）

参考urma-transport-lab的单Segment独立RX Slot布局，新增B-only参数：

```text
--destination-layout segments       # 默认：16×32 MiB分配，注册16个Segment
--destination-layout contiguous     # 1×512 MiB分配，注册1个Segment，划分16个独立范围
```

两种布局的总字节、独立Slot数量/长度、LOCAL_ONLY注册flag/对齐/清零及post/poll循环相同。
源file/Region/import和source-pattern不变。每个WR在对应Slot完成前独占其目的范围，
不共写目的地址。contiguous的READ local_offset及CRC slice均来自同一份Slot范围表；
全部buffer在前后校验完成和WR排空后只关闭一次。小文件的校验至少发depth次，以覆盖
每个目的Slot；源索引循环，仍覆盖全部文件。

仅测试用Rust模块改变，不修改生产DestinationPool、Source Cache、Storage或GC。
两端签名包含layout，C拒绝contiguous。launcher除了CONFIG还校验Child实际
URMA_BW_DESTINATION记录的单Segment、Slot数与注册字节一致，防止旧binary忽略新参数。
CONFIG/NATIVE和.log.json也记录layout。

**范围：**这同时改变分配和注册形态，物理页布局、首次触页/NUMA分布可能随之变化。
若性能改变，说明该布局组合值得继续细分，不能直接断言是Segment数量本身导致。
建议同轮交替segments/contiguous，固定source-pattern=fixed、CPU、provider和所有其它参数。

#### 两端更新并预构建

同步`dragonfly-client-storage/src/urma/read_bandwidth_probe.rs`和工具
`urma-b7/read_bandwidth_probe.py`。不需要UMDK perftest源码；使用之前可构建Rust探针的
已安装头文件/库（例如UMDK_INCLUDE_DIR=/usr/include/ub/umdk/urma、UMDK_LIB_DIR=/usr/lib64）。
两端先执行一次，避免Parent等待Child首次编译：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --destination-layout contiguous \
  --role child --addr 10.10.10.2:31918 \
  --log results/read-bw-B-contiguous-build-001/build.log \
  --build-only --execute
```

Parent：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --destination-layout contiguous \
  --role parent --addr 0.0.0.0:31918 \
  --file /home/y30083740/dragonfly-b7/origin/input-1g.bin \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-contiguous-001/parent.log --execute
```

Child：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --destination-layout contiguous \
  --role child --addr 10.10.10.2:31918 \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-contiguous-001/child.log --execute
```

先Parent后Child；两端都退出后用segments替换contiguous、换新日志目录跑对照。
两组均保留完整性和双方shutdown门禁，不以单轮变化宣布性能收益。

```bash
grep -E 'URMA_BW_(CONFIG|DESTINATION|RESULT|NATIVE|PASS)' \
  results/read-bw-B-contiguous-001/child.log
```

默认几何应出现`layout=contiguous segments=1 segment_bytes=536870912
registered_bytes=536870912 slots=16 slot_bytes=33554432`。segments组应为16个32 MiB
Segment，registered_bytes相同。取值/计时定义沿用前文B汇总观测，软件未退休深度不等于硬件占用。

### B 源内存对照：file / anonymous

新增 `--source-backing file|anonymous`，默认 `file`，仅 B 支持；两端必须相同。
先同步 Rust 探针、`ffi/read/source.rs` 和 Python launcher。匿名 mmap 类型只在
`cfg(test)` 下编译，不改变生产 Source、Cache、Storage 或 DestinationPool 行为。

- file：原文件 mmap 直接注册。
- anonymous：准备阶段逐 Region 分配匿名 mmap，从同一文件范围复制全部内容，转只读后
  通过相同 Source/native API 直接注册。每页在复制时触页；分配、复制、注册均在计时外。
- 两种模式仍使用原1 GiB输入、4×256 MiB源 Segment、fixed 时的32 MiB源访问范围、
  16个独立目的Slot和相同post/poll/校验/关闭流程。不是把源注册改成32 MiB。
- anonymous 会改变物理页和首次触页/NUMA分布；即使有收益，也不能只归因文件页类型。
- `Bytes` 的 shim 分配/复制路径未用于此对照。两种模式均检查 native `direct` 标记，
  Parent 输出 `URMA_BW_SOURCE backing=... segments=4 registered_bytes=1073741824
  region_bytes=268435456 direct=true`。双方配置和握手签名包含 backing，launcher 拒绝
  未证明匿名配置的旧binary；Parent还必须提供真实Source几何证据。
- Parent 仍要求 `--file`；前后完整性校验覆盖所有Piece，不局限fixed读取的Piece0。

两端先预构建（使用此前相同UMDK_INCLUDE_DIR/UMDK_LIB_DIR/LIBCLANG_PATH）：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --source-backing anonymous \
  --destination-layout contiguous \
  --role child --addr 10.10.10.2:31918 \
  --log results/read-bw-B-source-build-001/build.log \
  --build-only --execute
```

Parent先启动：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --source-backing anonymous \
  --destination-layout contiguous \
  --role parent --addr 0.0.0.0:31918 \
  --file /home/y30083740/dragonfly-b7/origin/input-1g.bin \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-anonymous-001/parent.log --execute
```

Child后启动：

```bash
python3 read_bandwidth_probe.py \
  --repo /home/y30083740/dragonfly/urma-cached-source \
  --mode B --source-pattern fixed --source-backing anonymous \
  --destination-layout contiguous \
  --role child --addr 10.10.10.2:31918 \
  --cpus 192-223 --device udmac0d1e2 --eid-index 0 \
  --log results/read-bw-B-anonymous-001/child.log --execute
```

两端退出后，仅将双方 `--source-backing anonymous` 改为 `--source-backing file`，
日志目录改为 `results/read-bw-B-file-001/`。交替跑file/anonymous，重复时换002/003目录。
每轮保留两端CONFIG/SOURCE/PASS及Child RESULT/NATIVE：

```bash
grep -E 'URMA_BW_(CONFIG|SOURCE|DESTINATION|RESULT|NATIVE|PASS)' \
  results/read-bw-B-anonymous-001/child.log \
  results/read-bw-B-file-001/child.log
```

在Parent用相同grep表达式读取两组`parent.log`。此实验只比较源backing，不与C模式混比，
也不保证匿名内存一定提高带宽；优先看同轮结果，再决定是否调整源注册几何/NUMA。
