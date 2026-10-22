# 自动同步（watchdog 四机制）— 判定与实施

> **当前状态（0.3.6）**：全部落地并在跑——存储格式（原样文本＋分片）、单实例防撞锁（v3.2：锁记录持有者 pid，持有进程消失即立即接管）、两枚自动同步脚本随技能分发（安装见文末）、哨兵脚本（`HERMES_HOME/scripts/hb-sentinel.py`，官方 cron 每 2 分钟、`--no-agent`）与周期任务（`hb-autosync.py` 每 2h）。轮次性能（sync v3.3/v3.4）＝单进程批量导出（≈8x，与 CLI 输出逐字节一致）＋未变文件不重写（mtime 不抖、git 不重哈希整树）。哨兵触发受「距上次完成同步 ≥20 分钟」新鲜度闸约束；触发失败会复位允许重试，但重试仍受该闸与静默窗约束——**静默窗长期不满足时由 ③ 周期轮兜底**，数据链不会断。

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

①②④ 合一个「每 2–5 分钟跑一次」的哨兵脚本；③ 独立原生任务。（均已安装：哨兵 2 分钟 / 周期 2h——2026-10-17 装。）

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
4. **测试纪律**：判读触发条件用 `--debug`（只读、不写状态）；验证触发路径时若向状态文件注入了合成值（如伪 `gw_start`），**事后必须还原**——残留的合成值会污染后续轮的真实判定（实例：残留伪值让下一轮误判「网关重启」多发一次同步）。另：**勿在前台、低超时下触发整轮同步**——`cron run`/`sync.py` 会跟随脚本跑完（一轮 4–6 分钟），中途被掐会留孤儿锁＋未推提交；v3.2 起孤儿锁由下一轮立即接管、未推提交随下轮自动补推（零丢失），但仍请一律用 background 或放宽超时。

## 安装（参考实现，两步）

1. 复制本技能 `scripts/hb-sentinel.py`、`scripts/hb-autosync.py` 到 `HERMES_HOME\scripts\`。
2. 建两个官方任务并点火：

```
hermes cron create "2m"       --name "hermes-backup 哨兵"     --script hb-sentinel.py --no-agent --deliver local
hermes cron create "every 2h" --name "hermes-backup 周期同步" --script hb-autosync.py --no-agent --deliver local
hermes cron run <任务id>      # 各触发一轮核验（同步单轮 4–6 分钟：会等脚本结束，前台请放宽超时或放后台）
```

参考参数：静默阈值 5 分钟（按全库最后一条消息计时）、长静默 1 小时、任意触发最小间隔 20 分钟、哨兵轮询 2 分钟。哨兵状态文件 `HERMES_HOME\state\hb-sentinel-state.json` 含 `last_busy_reasons` / `last_msg_age_s` / `last_trigger_*`——「为何（未）触发」可直接从状态文件读出。

## 判读与运维（未触发≠故障）

- 核查顺序：① 哨兵状态文件——`last_busy_reasons` / `last_msg_age_s` 给出上一轮「为何判忙」，`last_trigger_*` 是最近一次真实触发；② 镜像提交链——每 2h 一条＝③ 轮正常在兜底（自动同步在跑的正面证据）；③ 两个任务在 `hermes cron list` 的 Last run 新鲜度。
- **口径**：`processes.json` 非空（有后台进程在跑）或 5 分钟内有消息 ⇒ 判忙、②按定义不触发＝照章办事。先验证「静默窗是否真的出现过」再谈动手——别把「条件从未满足」误诊为「触发坏了」。
- **推送失败会自愈**：失败轮不推 ≠ 数据丢失——**下一轮自动补推积压提交**（实战验证过：一轮 push 被网络掐断、本地留 1–2 笔，下一轮整链追上）。连续多轮失败才查网络层：fake-ip 拦截的典型症状是 `Connection closed by 198.18.x port 22`（代理软件开启的窗口期），镜像 remote 可改走 `ssh://git@ssh.github.com:443/<owner>/<repo>.git`（GitHub 官方支持的 SSH 备用端口，直连与代理环境均可用）。

## 轮次生命周期（与网关重启、触发器的交互）

- 触发链是两层：cron 起父脚本 `hb-autosync.py`，父脚本再以子进程跑 `sync.py`。**网关重启/drain 常常只杀掉父脚本，`sync.py` 作为脱离进程继续跑完**——此时 `hermes cron runs` 停在 `running` 或落 `unknown`，而同步其实正常在跑；父脚本死后外层超时一并失效（该轮不受 `script_timeout` 约束）。判死活只认锁文件 pid 存活，判落地只认 `git ls-remote` / 远端历史。
- ①启动触发与在跑轮次：若触发瞬间**已有同步在跑**，哨兵跳过发送、却仍把新 `gw_start` 写进状态 ⇒ 该事件不重试（在跑的那轮自带覆盖）。复核法＝状态文件 `last_trigger_tag` / `last_trigger_ts` 是否更新为 `gateway-restart`。
- 手动补跑遇到在跑轮次会立即让路退出（stdout：`已有另一轮同步在进行（锁年龄 Ns）——本轮退出，让路`）＝设计行为，等那轮落地即可，不是失败。
- 慢轮排查顺序：`logs/last-run.log`（轮末整份落盘）→ 阶段行（批量导出 / `git 提交完成` / `git 推送完成` 三段计时）→ `git count-objects -v` 的 loose 数（偏大＝近期发生过 repack）→ 包文件 mtime。**轮次进行中别跑 `du` / `git status` 类全树探针**（持锁会挂住）。

## 观测与自愈（0.3.7，2026-10-21）

- **每轮一行结构化记录**：`$HERMES_HOME/state/hb-sync-runs.jsonl`（`start_ts/end_ts/duration_s/exit_code/timings{collect,export,git}/sessions/files/size_total_mb/committed/pushed/aborted/via`）——验收与排障只认这一行。
- **子进程输出流式落盘**：`$HERMES_HOME/state/hb-sync-child.log`（哨兵与周期脚本都流式写盘；超时被掐也留有阶段证据，替代原先 `capture_output` 把输出吞掉的做法）。
- **分阶段预算**：导出 ≤2000s、push ≤600s（逐档 CLI 回退每档 ≤900s）；外层 3300s 仍作兜底看门狗。
- **降优先级**：两个包装脚本以 `BELOW_NORMAL_PRIORITY_CLASS` 拉起 `sync.py`，不与前台抢 CPU/IO。
- **连续失败记账（0.4.3 起单一来源）**：`state/hb-sync-health.json` 由 **sync.py 自己在每个出口写**——cron 包装、哨兵 ①②④、手工运行都走同一条链路，字段含 `consecutive_failures / last_exit_code / last_start_ts / last_end_ts / last_duration_s`；连续 ≥3 轮失败写 `state/hb-sync-needs-attention`（**不主动推送**），一旦出现成功轮即清零并**自动删标记**（标记描述的是"当前是否仍连续失败"，条件消失就该消失）。子进程**没跑完**（超时/被杀/起不来）时由包装补记一次，`last_exit_code="unfinished"`，并按"健康文件 `updated_at` 是否晚于子进程启动"判重，绝不重复计数。

### 分支回归测试（0.4.3，可复跑）

```bash
python scripts/test-hb-sentinel.py     # ①②④/新鲜度闸/锁跳过/失败上报 → 8 个场景
python scripts/test-hb-health.py       # 健康记账单一来源 + 未跑完补偿去重
```
两个脚本都用假 `HERMES_HOME` + 桩 `sync.py`，纯标准库、只写临时目录、不跑真同步；末行 `== 总判定：PASS ==` 且退出码 0 即通过（`HB_TEST_BASE=<目录>` 可指定工作目录）。
实测记录（2026-10-23）：sentinel 8/8 通过、health 9/9 通过。**④"长静默≥1h 结束恢复"从此有确定性回归**，不再依赖"等自然发生"。