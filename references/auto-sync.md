# 自动同步（watchdog 四机制）— 判定与实施

> **当前状态**：全部落地并在跑——存储格式（原样文本＋分片）、单实例防撞锁（v3.2：锁记录持有者 pid，持有进程消失即立即接管）、两枚自动同步脚本随技能分发（安装见文末）、哨兵脚本（`HERMES_HOME/scripts/hb-sentinel.py`，官方 cron 每 2 分钟、`--no-agent`）与周期任务（`hb-autosync.py` 每 2h）。轮次性能（sync v3.3/v3.4）＝单进程批量导出（≈8x，与 CLI 输出逐字节一致）＋未变文件不重写（mtime 不抖、git 不重哈希整树）。哨兵触发受「距上次完成同步 ≥20 分钟」新鲜度闸约束；触发失败会复位允许重试，但重试仍受该闸与静默窗约束——**静默窗长期不满足时由 ③ 周期轮兜底**，数据链不会断。

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

## 失败告警接到人

默认 `--deliver local`（只落盘）。要真正收到通知（bot 会话 / 微信 / QQ）见 `references/operations.md` 的同名小节——
只改 cron 的 `--deliver`，不要改脚本（脚本契约：成功=空输出，失败=一段摘要）。

## 安装（参考实现，两步）

1. 复制本技能 `scripts/hb-sentinel.py`、`scripts/hb-autosync.py` 到 `HERMES_HOME\scripts\`。
2. 建两个官方任务并点火：

```
hermes cron create "2m"       --name "hermes-backup 哨兵"     --script hb-sentinel.py --no-agent --deliver local
hermes cron create "every 2h" --name "hermes-backup 周期同步" --script hb-autosync.py --no-agent --deliver local
hermes cron run <任务id>      # 各触发一轮核验（同步单轮 4–6 分钟：会等脚本结束，前台请放宽超时或放后台）
```

参考参数：静默阈值 5 分钟（按全库最后一条消息计时）、长静默 1 小时、任意触发最小间隔 20 分钟、哨兵轮询 2 分钟。哨兵状态文件 `HERMES_HOME\state\hb-sentinel-state.json` 含 `last_busy_reasons` / `last_msg_age_s` / `last_trigger_*`——「为何（未）触发」可直接从状态文件读出。

### 换机/恢复后：任务已在盘但是被禁用（不要重建，重启用）

从备份恢复出来的 `cron/jobs.json` 里两个任务通常 `enabled: false`。此时**不要 `cron create`**——会多出重复任务、两套一起抢锁。启用的官方命令是 `hermes cron resume <id>`；`hermes cron edit` **没有** `--enable` 参数（报 unrecognized arguments）。

**有一个显示陷阱**：`jobs.json` 里明明是 `enabled: false`，`hermes cron list` 仍可能显示 `[active]`——那一列读的是历史状态，不是启用位。所以「到底启没启用」要三处分开取、缺一不可：

1. `cron/jobs.json` 的 `enabled` 字段（地面真值）；
2. `hermes cron list` 里该任务的 Next run 是否在未来；
3. `cron/executions.db` 里该 job_id 的**新行**，并且要看 `source` 列——`direct` 是手动 `cron run` 打出来的，`builtin` 才是 ticker 自己跳的。**只有 builtin 能证明调度活着**；只有 direct 说明任务从没自然触发过。

两个任务都 resume 之后，等哨兵自己跳一次（2 分钟级别），executions.db 里出现 `builtin` 行才算真跑通。

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
- **分阶段预算**（以代码为准：`sync.py` 的 `PUSH_TIMEOUT` / `EXPORT_TIMEOUT` / `EXPORT_HARD_CAP`、`hb-autosync.py` 的 `SYNC_TIMEOUT`）：导出**按进展判**（每 30s 看进程 IO，涨就续期；连续 900s 不涨才杀整树，硬上限 1500s）、push ≤480s（×3 次）、外层 **1800s** 兜底；逐档 CLI 回退每档 ≤900s。**约束：所有内部预算必须小于外层 1800s**——超了就会被 `taskkill` 腰斩，连 runs 行都写不下。实测：正常轮 250–650s；一次 `git gc --auto` 全量 repack 曾把单轮拖到 **1,465s**（git 段 1,090s）⇒ 镜像已设 `gc.auto=0`（0.4.37）。（**按进展判**：每 30s 看导出目录体积，涨了就续期；连续 1200s 不涨才杀，硬上限 3600s）
- **降优先级**：两个包装脚本以 `BELOW_NORMAL_PRIORITY_CLASS` 拉起 `sync.py`，不与前台抢 CPU/IO。
- **连续失败记账（0.4.3 起单一来源）**：`state/hb-sync-health.json` 由 **sync.py 自己在每个出口写**——cron 包装、哨兵 ①②④、手工运行都走同一条链路，字段含 `consecutive_failures / last_exit_code / last_start_ts / last_end_ts / last_duration_s`；连续 ≥3 轮失败写 `state/hb-sync-needs-attention`（**不主动推送**），一旦出现成功轮即清零并**自动删标记**（标记描述的是"当前是否仍连续失败"，条件消失就该消失）。子进程**没跑完**（超时/被杀/起不来）时由包装补记一次，`last_exit_code="unfinished"`，并按"健康文件 `updated_at` 是否晚于子进程启动"判重，绝不重复计数。

### 分支回归测试（0.4.3，可复跑）
```bash
python scripts/test-hb-sentinel.py     # ①②④⑤/新鲜度闸/锁活性/失败上报 → 全部场景块（看末行「总判定：PASS」）
python scripts/test-hb-health.py       # 健康记账单一来源 + 未跑完补偿去重
```
两个脚本都用假 `HERMES_HOME` + 桩 `sync.py`，纯标准库、只写临时目录、不跑真同步；末行 `== 总判定：PASS ==` 且退出码 0 即通过（`HB_TEST_BASE=<目录>` 可指定工作目录）。
实测记录（2026-10-23）：sentinel 8/8 通过、health 9/9 通过。**④"长静默≥1h 结束恢复"从此有确定性回归**，不再依赖"等自然发生"。

## 推送硬化（0.4.9，2026-10-23 实案）

**现象**：11:18 那轮提交完成后（11:23:58），推送卡了 ~10 分钟**零字节**，GitHub 报
`Connection to ssh.github.com closed by remote host`；sync 重试第 2 次又是零字节、4 分钟不动。

**判据**（三条同时成立就是管道死了，不是网速慢）：
1. `git push` 子进程在、`ssh` 在，但 **ssh 读写计数 0 字节/10s**；
2. `git pack-objects` CPU 不再增长（压缩早就跑完，卡在写管道）；
3. 独立 `git ls-remote` 4 秒通（网络与认证没问题）。

**根因**：`subprocess` 的超时只杀**直接子进程**。上一轮的 `pack-objects` / `ssh` 孙子活下来继续
占着管道，于是**每一次重试都在同一个死管道前秒卡**——看起来像"网络又断了"，其实是本地残留。

**修法（sync.py 现已内置）**：
- 所有 git 网络操作注入 `GIT_SSH_COMMAND="ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30 -o TCPKeepAlive=yes"`（长传输不再静默挂死）；
- 超时用 `_kill_tree()` 杀**整棵进程树**（psutil children recursive）；
- 失败后用 `_reap_orphan_git()` 回收本仓**孤儿** git/ssh：只认命令行带本镜像路径、或带 `ssh.github.com … git-receive-pack`，且**父进程已消失**的进程——不碰别的 agent 的正常 git。

**实测**：手动带保活重推同一提交 → 压缩后 112MiB、**3.32 MiB/s**、2 分钟推完（`20c8fd92..c99f15c0`）。
超时路径单测：`sync.git(... timeout=0.05)` → 抛"超时（已杀进程树 2 个，回收孤儿 0 个）"，事后零残留。

## 导出超时留下的孤儿导出（0.4.15，2026-10-24 实案）

**现象**：连续两轮 `sync` 都撞 3300s 硬超时（exit=1，`runs.jsonl` 无新行，health `consecutive_failures` 递增）；
`hb-sync-child.log` 停在 `tools 镜像: 10 件`，之后再无 `export …` 行。

**取证**：
- `psutil` 找到一个 `python -I -c "import os, sys, runpy…"` 进程，**父进程已死**、RSS 1.6GB、持有 `profiles/ceo/state.db`；
- 镜像里 `sessions/ceo/*` 的 mtime 停在一天前，其它档有零散新写入 ⇒ 两个导出进程在写同一批文件；
- 该档 DB 本身没问题（`SELECT COUNT(*)` 0.09s、最大消息 0.4MB）。

**根因**：`subprocess.run(..., timeout=2000)` 超时只杀直接子进程（启动器），真正的导出进程留成孤儿；
下一轮接管锁后又起一个导出，两个同时写镜像 → 互相拖死，直到外层 3300s 再掐一次，再留一个孤儿。

**修法（0.4.15）**：
1. 导出改 `Popen + communicate(timeout=2000)`，超时 `_kill_tree()`（psutil 递归）杀整树；
2. 每轮 `acquire_lock()` 后调用 `_reap_orphan_exporters()`，只回收父进程已消失的导出进程；
   识别条件四件套（python ＋ `hermes-backup` ＋ `export_all_profiles.py` ＋ `--home`/`--out`），
   避免误杀"命令行里只是提到文件名"的进程；
3. `hb-autosync.py` 改 Popen + `wait(timeout)`，超时同样杀整树，并在 child log 落一行"已杀进程树 N 个"。

**回归**：`scripts/test-reap-exporters.py` 三场景（孤儿被杀 / 父进程健在不动 / 只提文件名不杀）。

### 修复后实测（2026-10-24 18:44–18:57）
- 手动验证轮：**745.9s**（collect 11.7 / export 458.7 / git 270），`ceo` 32.8s、`main` 159s，`committed+pushed`；
- 健康 2 → **0**；残留导出进程 **0**；本地=远端 `3982cddb`；
- 两个"活库已不存在"的会话在该轮记为 `D`（prune 生效），历史仍可取回；
- 随后审计 `--with-files`：**AUDIT-PASS，硬伤 0 项**（活库 2,204/397,407 ⇒ 镜像 2,203/396,907）。

## ⑤ 超期兜底（0.4.27）

**问题**：周期 cron 是 120 分钟一班，但 `next_run_at` 会因网关重启被重排（2026-10-25 实案：
03:57 跑完 → 03:41 的重启把下一班从 05:57 挪到 06:29，实际空窗 2.5 小时）。哨兵原来的三种触发
（网关重启 / 静默 5 分钟 / 长静默恢复）都盖不住"单纯漂移"。

**做法**：哨兵增加 `⑤ overdue`——镜像 `meta/sync-state.json` 的 `finished_at` 超过 **2.5 小时**、
当下没有同步在跑、且不满足"20 分钟新鲜"闸，就补一轮。它与既有触发互斥（先命中先触发）。

**回归**：`scripts/test-hb-sentinel.py` 增加 S8（超期触发 overdue）/ S9（刚同步过不触发）；后续又加 S11（shrink guard 只报一次）。判据看末行「总判定：PASS」。

**「下一班几点」要读不要算**：interval 任务的 `next_run_at` 锚在**上一次完成时刻 + 间隔**，不是整点/固定时钟槽——实测 06:51:35 跑完后下一班是 08:51:35，按「每 2 小时」心算成 08:29 会差 20 多分钟；**手动补跑一轮不会重排 cron 的下一班**（它只影响镜像内容，不影响调度）。报时间前一律读 `hermes cron list` 的 `Next run`（或 `--json` 里的 `next_run_at`）。

## shrink guard 与包装器（1.0.20，2026-10-31）

新机/重装后如果先跑同步（而不是先恢复），备份会被"镜像成本机现状"。`sync.py` 采集前因此有一道闸：
活库会话数 < 备份的 50%（且备份 ≥20 个会话）⇒ `exit 6`、镜像不动、`report.aborted=shrink_guard`。

包装器的处理约定（两件一致，别再各写一套）：

| 情形 | 输出 | 退出码 | 副作用 |
|---|---|---|---|
| 首次被拦 | 一行说明（会经 cron 投递给人） | 0 | 写 `state/hb-sync-guard-blocked.json` |
| 24 小时内再跑 | **空**（静默） | 0 | 不调 `sync.py`，不写状态 |
| 成功一轮 | 空 | 0 | 删掉该标记 |

判据：**主动拒绝 ≠ 故障**——不要让它进连续失败计数（`sync._record_health` 里 exit 6 不增不清）、
不要每次触发都弹同一条提示（`synced_this_quiet` 复位后每 20 分钟撞一次同一堵墙，等于把真故障信号淹掉）。
闸门在**建目录之前**跑：拒绝时连 `logs/`、`meta/sync-state.json` 都不写。
