# 自动同步（watchdog 四机制）— 判定与实施

> **当前状态（0.3.0）**：存储格式（原样文本＋分片）与单实例防撞锁均已落地——剩哨兵脚本与 cron 任务的安装。

## 官方件：Hermes cron 纯脚本任务

- 建法：`hermes cron create "<schedule>" --name <名> --script <HERMES_HOME/scripts/ 下脚本> --no-agent --deliver local`
- `--no-agent`＝不过模型、零 token；stdout 按 `--deliver` 投递，**空 stdout＝完全静默**（watchdog 形态）。
- 约束（源码实读）：脚本**必须**放 `HERMES_HOME/scripts/` 内；`.sh/.bash` 走 bash、其余走 Python（`--interpreter` 可指）；单轮超时默认 **3600s**（`cron.script_timeout_seconds` / `HERMES_CRON_SCRIPT_TIMEOUT` 可调）；调度随网关进程（网关没跑＝不触发，恢复后按 catch-up 补）。
- 错过补跑：`cron.catch_up_missed` 默认开，宽限窗外多次错过**合并为一次**。
- 事实：Hermes **没有**「启动/会话/回合」原生钩子（shell hooks 只有 pre/post_tool_call、pre_llm_call、subagent_stop）→ 条件类触发只能做**哨兵轮询**。

## 四机制与判定（目标形态）

| 机制 | 实现 | 可靠性 | 备注 |
|---|---|---|---|
| ① Hermes/网关启动后 | 哨兵读 `gateway.pid`（JSON 含 `start_time`）发现新启动 | 高 | 延迟 ≤ 轮询间隔 |
| ② 全部静默 >N 分钟 | 哨兵复合信号（见下） | 中高 | N 建议 10 分钟（长回合中 5 分钟易误判）；加「两轮自动同步最小间隔」帽 |
| ③ 每 2 小时 | 原生 interval 任务 | 高 | 最稳；配合 catch-up 已≈「每次开机后一轮＋周期刷新」 |
| ④ 长静默（≥1h）结束恢复活动 | 哨兵记活动时间戳，「≥1h 间隔后的活动跳变」→触发 | 高 | 与 ② 共用一个哨兵 |

①②④ 合一个「每 2–5 分钟跑一次」的哨兵脚本；③ 独立原生任务。

## 静默/活动信号源（实测可用）

- `%LOCALAPPDATA%/hermes/gateway_state.json`：`active_agents`、`active_work`（网关自报在跑作业）。
- `%LOCALAPPDATA%/hermes/processes.json`：**活的后台进程登记表**（实时更新、完成即清退）＝「无后台任务」判据。
- `state.db`（只读）：
  - `SELECT max(timestamp) FROM messages`（≈0.09s）＝全库最近一条消息时间；
  - `session_turn_leases`：未过期租约＝有回合在执行（旧行不清理，必须按 `expires_at > now` 判）；
  - `async_delegations`：非完成态＝在跑的后台委派。
- 判据组合：`active_agents==0 && active_work==null && 无未过期租约 && 无活跃委派 && processes.json 无活条目 && max(timestamp) 早于 N 分钟` ⇒ 静默。

## 实施前提（顺序不可反）

1. **先落 v0.3 存储格式**（**已完成，0.3.0**；见 `references/session-storage.md`）：高频触发 × 每轮 +226MB ＝ 仓库按 GB/天膨胀 → 原样文本格式后每轮增量降到 ~0.01–2.25MB/文件级，频率已解禁。
2. 给 `sync.py` 补**单实例防撞锁**（**已完成，0.3.0**：`HERMES_HOME/state/hermes-backup-sync.lock`，崩溃遗留 3h 后自动接管）。
3. 装完当场 `hermes cron run <任务id>` 真触发一轮验证（核对镜像/远端真实前进）。