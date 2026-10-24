---
name: hermes-cloud-backup
description: Back up/restore Hermes state via a private GitHub repo.
version: 0.4.28
author: yechang1450, Hermes Agent
license: UNLICENSED
platforms: [windows]
metadata:
  hermes:
    tags: [backup, restore, github, cloud, sync]
    related_skills: []
---

# hermes-cloud-backup — Hermes 数据云备份 / 恢复

> **维护笔记（发布前必读）**：①官方策展器会持续回写本技能文件（回写带 CRLF）；发布时把技能目录拷入暂存后先 `sed -i 's/\r$//'` 归一（git 的 `text=auto` 在提交侧也会归一 blob），再用「全新克隆 ↔ 暂存」逐字节比对核实——**判 CRLF 用 python 字节计数，不要用 MSYS grep（管道层会造假 CR）**。②公开仓默认分支为 **main**（`git push origin HEAD`）。③一致性四处：技能目录 ↔ 暂存 `temp/hcb-publish` ↔ 公开仓 ↔ 数据镜像 tools/。④改会话存储格式或恢复工具时，同步更新**镜像根 README** 的格式描述（该文件不被 sync 自动生成；手改后随下轮同步提交）。⑤**owner 会直接改公开仓**（如政策提交：删 LICENSE、`license: MIT → UNLICENSED`、README 改 Copyright 段）——推送前必 `git fetch` 并 `git log HEAD..origin/main` 看有没有远端新提交；有就 `git reset --hard origin/main` 再把本地的文件改动重放上去重新提交，**绝不 force / 绝不覆盖他的提交**。本仓的常态口径＝**无 LICENSE 文件**（保留所有权利）、frontmatter `license: UNLICENSED`；别再写回 MIT。⑥**可能有并行 agent 也在维护本技能**（2026-10-24/25 实见两笔既非 owner 也非本对话的提交：`restore_sessions --verify`、推送预算 900→1500s／外层 3300→3900s）——每次动手前 `git log --oneline -3` 看一眼，推前必 fetch，**只做增量、绝不回滚别人的提交**；改同一文件前先重读当前内容（技能目录可能已被对方改过）。⑦**版本号当最后一步改**：`SKILL.md` 的 `version:` 要在**提交前最后一次**写，并让提交信息里的版本号与它一致；提交后回读一次 `git show origin/main:SKILL.md | grep '^version'` 核对。多写者并行时很容易出现「0.4.22 的提交信息里装着 0.4.23 的内容」这种考古灾难——本次就因对方已把版本提到 0.4.23、我的 `sed 0.4.21→0.4.22` 静默不匹配而撞上。

把本机 Hermes 的全部状态（配置/凭据/记忆/技能/会话/桌面端数据）**线性同步**到一个
**私有 GitHub 仓**（默认 `hermes-backup`，唯一权威版本在 GitHub），并支持在任何新机上
从该仓**一键恢复到重装前状态**。本技能含两套流程：**日常同步**（源机）与**新机恢复**（目标机）。

架构一句话：镜像工作区 `D:\hermes-backup` 采集本机数据 → commit → push；恢复方向相反。

## When to Use

- 用户说「备份一下 / 同步一下 / 推到 GitHub / 把 Hermes 存一下」→ 执行 **流程 A（日常同步）**
- 用户在新机或重装后说「恢复我的 Hermes 备份 / 从 GitHub 恢复之前的状态」→ 严格执行 **流程 B（新机恢复，见 templates/RESTORE.md）**
- Don't use for：非 Hermes 数据；公开仓库（**本仓必须永远保持私有**，内含凭据与私人对话）。

## Prerequisites

- GitHub 账号。首次使用：`python <SKILL>/scripts/sync.py --owner <你的GitHub用户名> --create-remote`（自动建私有仓、配 remote；已有仓直接识别）。owner/remote 也会从镜像仓 git remote 或 `gh` 登录名自动解析。
- 本机 Hermes 安装完整：`HERMES_HOME`（默认 `%LOCALAPPDATA%\hermes`）内有 `hermes-agent/` 源码与 `bin/`、`tools/python-*/`。
- 恢复端需要：git、可访问 GitHub（SSH 或 PAT）、gh（可选但强烈建议——用于私有性检查）。
- 脚本纯标准库；除会话导入外都可用任意 python 直跑。

## Quick Reference

| 操作 | 命令（`<SKILL>` = 本技能目录，`<REPO>` = 备份仓 clone 目录） |
|---|---|
| 首次初始化（建私有仓） | `python <SKILL>/scripts/sync.py --owner <gh-user> --create-remote` |
| 日常同步 | `python <SKILL>/scripts/sync.py` |
| 文件层恢复 | `python <REPO>/tools/restore_files.py --repo <REPO> --home <HERMES_HOME>` |
| 会话层恢复 | `python <REPO>/tools/run_in_runtime.py <REPO>/tools/restore_sessions.py --all`（加 `--fast-import`，大型历史约 5 倍速，见 references/session-storage.md） |
| 拼包（给桌面端导入用） | `python <REPO>/tools/concat_sessions.py main` |
| 覆盖率审计（镜像 vs 活库） | `python <SKILL>/scripts/audit-coverage.py --repo <REPO> --home <HERMES_HOME>`（PASS 打印 `AUDIT-PASS`，硬伤退出码 1；加 `--with-files` 连文件层一起审） |
| 数据仓换代（体积到阈值） | `bash <SKILL>/scripts/rotate-generation.sh --from <旧代> --to <新代> --dry-run` 先看计划，再去掉 `--dry-run` 真跑（见 references/generations.md） |
| 路由回写（微信/QQ 续接） | `python <REPO>/tools/apply_routing.py` |
| 桌面端收尾器 | `pwsh -NoProfile -File <REPO>/tools/finish_restore.ps1 -Repo <REPO>` |

> 机器本地配置（镜像目录、同步默认仓名）放技能目录 `local.json` 的 `mirror` / `repo` 两项（不进任何仓库）；镜像目录默认自动探测，`--mirror` 可指定；镜像已有 origin 时仓名以 origin 为准（换代脚本会同时改写 origin 与 `local.json`，代码里不写死代名）。

## 文件构成

- `scripts/sync.py` — 日常同步（采集 → commit → push；本机镜像工作区）
- `scripts/restore_files.py` — 文件层恢复（home/profiles 覆盖，旧件自动备份）
- `scripts/restore_sessions.py` — 会话层恢复（走官方 SessionDB 导入管线）
- `scripts/run_in_runtime.py` — 在 Hermes 官方运行时里执行脚本（会话导入需要）
- `scripts/apply_routing.py` — 平台路由回写（微信/QQ 旧线程续接）
- `scripts/concat_sessions.py` — 会话拼包（桌面端「导入」按钮用）
- `scripts/finish_restore.ps1` — 桌面端收尾器（等待退出 → 替换数据 → 重启网关/桌面端）
- `scripts/hb-sentinel.py` — 自动同步哨兵（①启动 / ②静默≥5分钟 / ④长静默≥1h 恢复触发；安装=复制到 `HERMES_HOME\scripts\` 后由官方 cron 每 2 分钟调用）
- `scripts/hb-autosync.py` — 周期同步（③每 2 小时；安装方式同上）；与哨兵共用 `state/hb-sync-child.log`（子进程输出流式落盘）与 `state/hb-sync-health.json`（连续失败计数 + needs-attention 标记）
- `scripts/export_all_profiles.py` — 单进程批量会话导出（sync 提速件；直调官方导出函数，与 CLI 输出逐字节一致，失败自动回退逐档 CLI）
- `scripts/verify_drill.py` — 演练对账器（镜像会话文件 vs 沙箱 DB 逐会话比对；`restore-drill.md` 第 6 步的现成实现，参数＝沙箱根目录）
- `scripts/test-hb-sentinel.py` — 哨兵分支回归（8 场景：①网关重启 / ②静默 5 分钟 / ④长静默恢复 / 新鲜度闸 / 锁跳过 / 失败上报 / 冷启动两态；假 HERMES_HOME＋桩 sync.py，不碰真数据）
- `scripts/test-hb-health.py` — 健康记账回归（sync.py 单一来源 + 包装"未跑完"补偿去重）
- `scripts/test-audit-coverage.py` — 覆盖率审计的**负向对照**（8 场景合成夹具：全等 / 缺会话 / 多会话 / 镜像超前 / 镜像落后 / 轮内新建豁免 / 孤立组 / 坏 JSON；证明审计"该红就红"，不碰真库）
- `scripts/test-split.py` — 分片回归（合成 2MB/100MB/45MB 三条会话、阈值取 `sync.PART_MAX`）：断言每片≤阈值、片数＝ceil(字节/阈值)、按片名拼接后**小会话逐字节相同**、**>5MB 的会话语义等价**（`json.loads` 相同——它们会被尾部稳定化重排成员顺序，不能再用字节相等断言）
- `scripts/test-layout.py` — 尾部稳定化的回归（6 项：语义等价 / 幂等 / 字段落尾 / 追加后 messages 前缀不变 / 40MB 性能 / 前半段＝KEEP_KEYS 且 messages 收尾）
- `scripts/churn-report.py` — **单轮重写量**量测器（按目录汇总新增 blob 字节、列 >5MB 改动）：判断「体积增长有没有真被压住」只认它，不看 `--stat` 的文件数/行数；`git --raw` 解析用 `-z`，带空格路径不会错位
- `scripts/session_layout.py` — 会话 JSON 的尾部稳定化（零解析扫描；**只留身份字段＋`messages` 在前，其余成员全部移到对象末尾**）
- `scripts/test-reap-exporters.py` — 孤儿导出回收回归（3 场景：孤儿被杀 / 父进程健在者不动 / 命令行只提到文件名者不杀）
- `scripts/audit-coverage.py` — 覆盖率审计：①会话层（镜像会话文件 vs 活库 state.db 逐会话条数）②文件层（`--with-files`：HERMES_HOME 顶层条目 vs 采集白名单，列出未分类/漏备项）；判据＝硬伤退出码 1
- `scripts/rotate-generation.sh` — 数据仓换代（孤儿快照单提交种子 → 新代私有仓 → 旧仓归档；`--dry-run` 只打印计划；幂等可续跑）
- `references/generations.md` — 数据仓「换代」归档预案（体积到阈值时换新代仓、旧仓冻结为只读档案；不重写历史）
- `templates/RESTORE.md` — 新机恢复手册（编号步骤 + 验收清单）
- `references/session-storage.md` — 会话存储格式与历史体积机制（v0.3 原样文本格式、实测方法、GitHub 链路限制）
- `references/auto-sync.md` — 自动同步（watchdog 四机制）判定、静默信号源与实施顺序
- `references/restore-drill.md` — 恢复侧验收演练配方（两遍独立演练标准、逐会话对账口径、判读坑）

## 流程 A：日常同步（源机）

1. 在 `terminal` 里跑 `python <SKILL>/scripts/sync.py`（后台跑也可以；其日志在 `<镜像>/logs/`）。
   完成标准：末尾出现 `sync 完成：… committed=… pushed=True`，且 `git ls-remote` 与本地 HEAD 一致。
2. 铁规：**永不 force-push**；推送前脚本自检仓库私有性（gh 可用时；非私有会尝试改回、失败即中止推送）。
3. 节流建议：每日 ≤1–2 次或按需。**v0.3 存储格式已落地（0.3.0）**：会话原样存文本、**>12MB 自动分片**（0.4.26），每轮入历史的是「真实新增 ＋ 变动分片」而不是整份文件。**但别把它读成「体积无顾虑」**：容量仍随内容增长与末片重写上升，必须按 `references/generations.md` 的「容量巡检」定期量 `churn-report.py` 与远端 `.size`，到 4.5GB 前换代——2026-10-24 就是因为把这句当成「已解决」而漏看了 150MB/小时的暴涨。

## 自动同步（可选；走官方 cron 的纯脚本任务）

把同步挂成 Hermes 官方 cron 的 watchdog 型任务（`--no-agent`：不过模型、不花 token，只投递脚本输出）：

1. 启动器：本技能随附 `scripts/hb-sentinel.py`（哨兵）与 `scripts/hb-autosync.py`（周期）两枚参考实现——复制到 `HERMES_HOME\scripts\`（cron 脚本**只允许**放在该目录内；`.py` 由官方托管 Python 执行）。脚本自带 git PATH 摆正、防撞锁与 20 分钟新鲜度闸；**成功＝空输出（静默）**，失败输出错误摘要（被自动投递）。
2. 建两个任务（哨兵 2 分钟＋周期 2 小时；完整命令见 `references/auto-sync.md`「安装」节）：`hermes cron create "2m" --name "hermes-backup 哨兵" --script hb-sentinel.py --no-agent --deliver local` 与 `hermes cron create "every 2h" --name "hermes-backup 周期同步" --script hb-autosync.py --no-agent --deliver local`。建好后用 `hermes cron run <任务id>` 当场触发核对——注意 **`cron run` 会跟随脚本跑完（同步一轮约 4–6 分钟）**：请放宽超时或用 background，勿中途掐掉（掐掉会留孤儿锁＋未推提交；v3.2 起由下一轮自动接管并补推，零丢失）。
3. 官方语义：**错过自动补跑**（`cron.catch_up_missed` 默认开；多次错过合并补跑一次）⇒ 等效「每次开机/网关重启后补一轮 ＋ 运行中周期刷新」；调度器随网关运行（`hermes cron status` 看宿主与心跳）；脚本单轮超时可调（`HERMES_CRON_SCRIPT_TIMEOUT` 或 `cron.script_timeout_seconds`），按同步 4–6 分钟一轮留足余量。
4. **并发前提**：多触发源（手动＋自动）并存前，先给 `sync.py` 补**单实例锁**——两轮同步相撞会互踩镜像仓。（**已落地**：v0.3 起 `sync.py` 自带防撞锁，后来者自动让路。）
5. **目标形态（四机制）**：①网关/Hermes 启动后 ②全部静默 ≥N 分钟 ③每 2 小时 ④长静默（≥1h）结束即触发——①②④ 由一个哨兵轮询脚本统一实现（Hermes 无启动/会话原生钩子，条件触发只能轮询；静默判据＝`gateway_state.json`＋`processes.json`＋`session_turn_leases`＋`async_delegations`＋`messages.max(timestamp)` 复合）。
6. **实施顺序**：v0.3 存储格式＋防撞锁（**均已落地并实测**）→ 安装哨兵与两任务（命令见 `references/auto-sync.md`「安装」节）。判定表、信号源与坑 → 同文档。

## 流程 B：新机恢复（目标机）

**严格按 `templates/RESTORE.md` 的编号步骤执行**。要点顺序（此顺序有依赖，勿打乱）：

1. clone 备份仓到本机（`git clone git@github.com:<owner>/hermes-backup.git D:\hermes-backup`）。
2. 文件层覆盖（含覆盖前自动备份旧件到 `restore-out/pre-restore-backup/`）。
3. 会话层导入（走官方 `SessionDB.import_sessions` 管线；本工具进程内放宽官方导入尺寸上限，以恢复含超大档在内的全部历史；已存在的会话自动跳过，可重复跑）。
4. 路由回写（微信/QQ 线程 → 会话映射）。
5. 桌面端收尾器（**唯一需要用户配合的一步：完全退出桌面端一次**；收尾器自动完成剩余拷贝、重启网关、重新拉起桌面端）。
6. 重启后按 RESTORE.md 的验收清单逐项核对。

## Pitfalls

- **恢复要带 `--verify`**（0.4.22）：`restore_sessions.py --fast-import --verify` 会在导入后**逐会话**把「镜像里的消息条数」与「恢复库里的行数」对齐，不一致就打印明细并让退出码=3。以前演练靠人肉比对（我手动抽 7 个会话），现在一条命令出结论；实测 mentoragent 11/638 「不一致 0 个」。
- **跑会 import `hermes_state` 的脚本必须经 `tools/run_in_runtime.py`**：在本机终端里 `python <脚本>.py` 会被启动器以**另一个解释器**（缺 ruamel 等运行期依赖）经 runpy 重跑，报 `ModuleNotFoundError: No module named 'ruamel'`；就是 `python -c "import <模块>"` 也不能豁免（`SessionDB(...)` 构造会把进程切到工具树 python）。正确形态一律是 `python <REPO>/tools/run_in_runtime.py <脚本> [参数]`——恢复、对账、A/B 测试脚本皆同。同理：**恢复/对账不要与 `hermes update` 并发**，更新收尾持有 checkout 锁，卡住的子进程会 0% CPU 空转（判据：RSS 几 MB、CPU 0、日志停在启动横幅）。
- **`sync.py` 的 home 只按 `LOCALAPPDATA` 解析（不读 `HERMES_HOME` 环境变量）**：`HERMES_HOME` 只影响两个包装脚本（哨兵/周期）与 hook 的路径。写单测时必须**注入 `sync.HERMES_HOME / HEALTH_FILE / ATTENTION_MARKER / RUNS_LOG`** 模块属性，否则会写进真实 `state/`（2026-10-23 实测踩过：把假失败记进了真健康文件）。
- **推送预算 900s→1500s、外层 3300s→3900s**（0.4.23）：一次性大重排那轮（900MB 级）推送超 900s 被掐，属合法大包不是故障；外层同步放宽到 3900s（collect+export+commit 最坏 ~1900s）。日常轮次仍只有几十 MB，读数不会变。
- **推送卡在"零字节"时先看进程树，别只看网络**（0.4.9）：`subprocess` 超时只杀直接子进程，`git pack-objects` / `ssh` 这些孙子会留下来占住管道 → **之后每次重试都秒卡、零字节**（2026-10-23 实案：一轮推 10 分钟零字节、三次重试全卡，GitHub 侧只报 `Connection … closed by remote host`）。现在的 `sync.py`：①所有 git 网络操作注入 `GIT_SSH_COMMAND` 保活（ServerAliveInterval=20 / CountMax=30 / TCPKeepAlive）；②超时杀**整棵进程树**；③失败后回收本仓的**孤儿** git/ssh（只认命令行带本镜像路径或 `ssh.github.com … git-receive-pack`、且父进程已消失的）。判据三件套：`git push` 子进程存在 + `pack-objects` CPU 不涨 + ssh 读写 0 字节 ⇒ 管道死了，不是网速慢。
- **成功轮要清 `last_error`**（0.4.17）：健康文件是连续性记账的单一来源，失败时写 `last_error`、成功时必须置 `None`——否则「上一轮 timeout 3300s」会一直挂着，被后读的人（包括我）误判成当前仍在失败。`repaired_note` 属历史注记，保留不动。
- **SQLite 必须用在线备份 API 快照，不能直接拷**（0.4.28）：看板库是 `journal_mode=wal`，而 `-wal`/`-shm` 被排除在备份外 ⇒ 只拷 `.db` 会丢掉尚未 checkpoint 的已提交事务；实测极端情况下**连建表语句都可能不在 `.db` 里**（普通拷贝打开即 `no such table`）。现在 `copy_file()` 对所有 `.db` 走 `snapshot_sqlite()`（`sqlite3.Connection.backup()`）：读穿 WAL、产出自包含紧凑快照，失败自动回退普通拷贝。覆盖 `kanban/*.db`、`shared-state.db`、`projects.db` 以及各档 `projects.db`。回归＝`test-kanban-snapshot.py`（3 项）。
- **哨兵有超期兜底**（0.4.27）：周期 cron 的 `next_run_at` 会被网关重启重排（实测把 05:57 挪到 06:29，空窗 2.5h）；哨兵现在除「网关重启 / 静默 5 分钟 / 长静默恢复」外，还有 **⑤ overdue**——镜像 `finished_at` 超过 2.5 小时、没在同步、也不满足 20 分钟新鲜闸，就补一轮。回归＝`test-hb-sentinel.py` S8/S9。
- **导出超时只杀直接子进程 → 孤儿导出会撞死下一轮**（0.4.15，2026-10-24 实案）：连续两轮 3300s 超时，原因是 `subprocess` 超时只杀直接子进程（`run_in_runtime` 启动器），真正的导出进程活下来占住 `profiles/<档>/state.db` 与镜像；下一轮再起一个，两个导出同时写同一批文件 → 双双卡死、RSS 涨到 1.6GB。现在三处收口：①导出改 `Popen + communicate(timeout=2000)`，超时 `_kill_tree()` 杀整树；②每轮启动后 `_reap_orphan_exporters()` 只回收「父进程已消失」的导出（识别条件：python 解释器 ＋ **真实参数**里以 `export_all_profiles.py` 结尾 ＋ 带 `--home`/`--out`；只看 argv 不看整条命令行文本，否则自己的 `-c` 探针只要代码里提到这些字眼就会自匹配）；③包装器 `hb-autosync` 超时也杀整树。回归＝`scripts/test-reap-exporters.py`（3 场景）。**判据：** 超时后先看 `ps` 里有没有 `export_all_profiles` 进程，再看下一轮是否同时存在两个导出。
- **官方 schema 会加触发器，fast-import 只摘"索引类"**（v0.22.5 实测）：库里现有 **13 个**触发器——除 display 4 个 + FTS 6 个（这 10 个由 fast-import 摘除、导入后重算/rebuild/原样恢复），还有 3 个**保持生效**：`messages_message_uid_insert`（插入时填 `message_uid`）、`messages_transcript_revision_update`（维护会话修订号）、`sessions_runtime_ledger_delete_guard`（删除护栏，与导入无关）。恢复结束会自检 `message_uid` 空值并打印 `uid 空值 0/N`；**空值 >0 ⇒ 官方改名/失效信号**，先核对官方 schema 再改 `INDEX_TRIGGERS`，别盲摘。**触发器总数随官方版本变**（实测 v0.22.2＝11 个＝10 索引＋uid；v0.22.5＝13 个）——脚本与验收里别写死数量，只断言「索引类 10 个可摘可原样还原」＋「uid 空值 0/N」。v0.22.5 实测：21,693 条巨会话 `--fast-import` 恢复 + FTS 重建 **17.4s**、`message_uid` 全非空。
- **对账三个数别混用（审计的「活库消息数」不含孤立组）**：`audit-coverage.py` 打印的 `活库 X 会话/Y 消息` 里 Y 只数「sessions 行存在」的消息；`孤立 N 组/M 条`（messages 有行、sessions 无会话行）是**单列的**。裸 `SELECT COUNT(*) FROM messages` 会比 Y 多出 M（实案 proj-83：24,930 vs 25,748，差 818 ＝ 孤立 4 组 818 条）。恢复对账、子代理汇报里拿裸 COUNT(*) 去比镜像文件，会把孤立组误读成「备份落后/丢数据」——**先分解成「会话内消息＋孤立组消息」两个数再比**；镜像侧消息数一律按文件解析（分片按片名拼接），不信目录里的文件数。
- **`restore_sessions.py --source` 收的是「父目录」不是 profile 目录**：结构必须是 `<src>/<profile>/*.jsonl`（`--source …/src` 对应 `src/main/…`）；指到 `src/main` 会去找 `src/main/main`，报源目录不存在。
- **超大会话导出守卫**：`sessions export` 默认对 >20000 条消息的会话整包报错（且 rc=0 无产物）。官方解法 = `hermes config set sessions.max_export_messages 0`（新机上首次同步前设置一次即可）。
- **GitHub 单文件 100MiB 硬限**：会话导出件按会话拆分；v0.3 起原样文本存储、**>12MB 的巨会话按 12MB 切片**（`<sid>.part-NNNN.jsonl`；0.4.10 由 90→45MB，0.4.26 再降到 12MB——尾部稳定化后每轮只有「变动的那个分片」重写，阈值直接决定单轮重写量：45MB 时稳态仍 249MB/轮、其中 208MB 是 10 个活动会话的末片；12MB 把末片压到 1/4，距 100MB 硬限仍有 8 倍余量）。**改阈值/改格式后必须做一次「多分片真恢复」**：挑一个切成 >10 片的会话，按片拷到 `<src>/<profile>/` 走 `restore_sessions.py --source … --fast-import --verify`（实测 243MB 会话切成 21 片、恢复 73,546 条、`verify_failed=0`）——单跑 `test-split` 只证明字节/语义等价，证明不了导入管线能吃下多片。字节等价由 `scripts/test-split.py` 回归（切片+按片号拼接 == 原始行，逐字节）。
- **量重写量用自带工具**：`python tools/churn-report.py`（默认镜像 HEAD~1..HEAD）按目录汇总新增 blob 字节，并列出 >5MB 文件；判据——同一个会话的多个 `.part-` 同时出现在 >5MB 列表里，说明它仍被整份重写。注意 git `--raw` 对带空格的路径会加引号，解析必须用 `-z`（本工具已处理；2026-10-24 实案：`desktop-overlay/Local Storage` 曾被按空格切列误记成 `Storage/leveldb`）。
- **头部字段一变，整份分片全成新 blob**（0.4.18，2026-10-24 实测）：会话是「一行=一个会话」的 JSON，头部有 `message_count` / token 计数 / `timings` / `last_activity_at` / `transcript_revision` 等**每轮都会变**的统计字段；头部一变，整行字节全变 ⇒ 固定字节偏移切出的**每一个分片都重写**（实名单轮 827MB，10 个巨会话各 3–6 个 45MB 分片全改；仓体积约 150MB/小时增长）。修法＝`scripts/session_layout.py` 的**尾部稳定化**：零解析、字符串感知地**只保留身份字段（`KEEP_KEYS`：id/source/user_id/session_key/chat_id/…）与 `messages` 在前，其余顶层成员全部移到对象末尾**（不改任何内容的字节），于是未变会话完全一致、有追加的只改末尾分片。**别写「正向列举易变字段」**——第一版正是那么写的，漏了 `last_activity_description`、`runtime_revision` 等同样会变的字段，整行照样被推翻、白改一版；反向白名单才成立。只对 >5MB 的会话行生效（小文件整份重写无所谓），解析失败一律原样保留。回归＝`scripts/test-layout.py`（6 项：等价/幂等/字段落尾/追加前缀稳定/40MB 性能/前半段严格＝KEEP）；**整轮收益的验收口径**＝`scripts/churn-report.py` 量的是**格式切换后的第二个轮次**（切换那一轮含一次性重排，见下一条），且改动字节应显著下降；不能只凭单元测试就说「体积问题解决了」。
- **体积改动的验收必须跨「两个稳定化轮次」，不能拿格式切换那一轮当判据**：改存储格式/切片策略后的**第一轮**会把受影响文件整体重写一遍（一次性成本），单看那一轮的重写量会把「重排」误读成「没修好」——实案 827MB → 967MB 看着更糟，其实 967 里混着全部大会话的首次重排。做法：记下基线 → 等**下一轮**（两个轮次都是新格式）→ 用 `scripts/churn-report.py` 量 → 只有「未变会话不再出现、巨会话只剩末片」才算成立；连续两轮都仍有整份巨会话重写，说明变的是**内容**（压缩改写历史等），要换更深的方案（一条消息一行 / 按内容寻址），继续调参数没用。
- **历史体积机制（关键）**：git 仓库＝最新快照＋**每个被改文件的每一版**——同步的「强制覆盖」语义只作用于最新树（≈镜像工作区），**挡不住历史堆积**（实测：全历史 blob 2.01GB vs 最新树 0.72GB；单会话 10 个版本＝313MB）。根因＝会话导出是**超长单行 JSON**、头部元数据每轮必变 × 整包 gzip → deflate 级联 → 版本间字节重叠 **0%** → git 增量全失效 → 每轮 ~226MB 完整入库（仓库 ~0.5GB/天）。**已治理（v0.3，0.3.0）＝原样文本格式：同数据对照实测 +79.40MB/轮 → +0.01MB（整件）/ +2.25MB（分片）级；机理与三方案对照见 `references/session-storage.md`。** 单文件 95MB 同步闸由分片格式顺带拆除（分片后最大件 12MB）；体积读数用**本地对象库**实测（GitHub `size` 字段有滞后）。
- **意外中断与并发自愈**：自带单实例防撞锁——**v3.2 起锁内记录持有者 pid：持有进程已消失（触发端被掐/崩溃）立即接管，3h 陈旧窗仅兜底**（早期版本纯 3h 窗口＝被掐后可能空窗最长 3 小时）；被强杀遗留的 `.git/index.lock`（>120s）自动清除并重试一次。真机实测：进程在 `git add` 瞬间被杀 → 下一轮同步报 `rc=128` —— 自愈代码即为覆盖该情形；被掐轮次的**未推提交由下一轮随带补推（零丢失）**。- **网关重启与在跑轮次的三种组合**（先取证据、都别慌）：① drain 杀掉父脚本、`sync.py` 作为**脱离进程继续跑完**——`hermes cron runs` 会停在 `running` 或落 `unknown`，**判轮次死活只认锁文件里的 pid 存活**（存活＝还在跑、会自行落地；已死＝孤儿锁由下一轮立即接管）；② 在途提交被掐——判「有没有丢数据」**不看 run 状态**，用 `git merge-base --is-ancestor <该轮提交> <远端HEAD>`：命中＝已进远端历史、零丢失；未命中才去查采集/推送段；③ 父脚本被杀时外层超时兜底一并消失 ⇒ 该轮不受 `script_timeout` 约束，必要时人工盯或回收。另：两轮相撞时后来者**立即让路**（日志 `已有另一轮同步在进行（锁年龄 Ns）——本轮退出，让路`）——设计行为，不是失败。
- **推送断连（exit 3）≠ 没推上去**：`send-pack: unexpected disconnect while reading sideband packet` / `Connection to ssh.github.com closed by remote host` 是传输层断线——GitHub 可能已收完整包并更新 ref，只是客户端没拿到确认。**判据只认远端口**：`git ls-remote origin main`（或 `… HEAD`）与 `git rev-parse HEAD` 比对；**别拿本地 `origin/main` 跟踪引用当远端**（失败的 push 不更新它，两种误读都会发生）。一致 ⇒ 零丢失、失败仅是确认缺失：不重推、不 force、不改机制，`hb-sync-health.json` 记 1 次失败、下轮照常；不一致 ⇒ 提交仍在本地、下一轮自动补推。**判「这轮为什么慢」先看在跑 worker 数**：十几张看板 worker 并行时导出段会涨 2–3 倍（实测 485–627s → 1481s），整轮逼近外层预算但仍在预算内，别当卡死。
- **失败诊断锚**：同步失败先读 ① `meta/sync-state.json` 的 `git_error` / `committed` / `pushed` / `finished_at`（失败路径也会写 meta）；② 镜像 `logs/last-run.log`（每轮全量日志）。**提交里的 meta 滞后一版**——write_meta 在 commit 之后执行，所以最新提交里包含的是**上一轮**的 sync-state（工作区 `meta/sync-state.json` 常显 modified，属正常，随下轮提交）。
- **慢轮诊断（阶段预算＋廉价探针）**：正常档一轮 5–8 分钟（收集 1–2 分钟 / 批量导出 ~2 分钟 / 拆分＋`git add` 秒级 / 提交与推送 1–3 分钟）；明显超出即按段找放大器。**`logs/last-run.log` 在轮次结束时整份落盘，中途没有文件可读** ⇒ 要么等落地，要么用廉价探针：锁文件 pid 存活（用 `psutil.Process(pid)` 判定——`ps -W` 只显示可执行名、不含命令行参数，`ps -W | grep 脚本名` 永远匹配不到 python 子进程）、`.git/index.lock` 存在＝正在 add/commit、`git count-objects -v` 的 loose 数、包文件 mtime（repack 痕迹）、进程 IO/CPU（psutil）。**轮次未结束时不要对镜像跑全树重探针**（`du -sm`、`git status`、递归 ls）：持索引锁时 `git status` 会挂住、大目录 `du` 会跑满超时。日志内建阶段行：`git 提交完成：Ns（含 add 与自动 gc）`、`git 推送完成：Ns`。**包装脚本必须把子进程输出流式落盘**（`stdout=<文件句柄>`，不要 `capture_output`）：超时被掐的那一刻 `capture_output` 会连子进程 stdout 一起丢，超时轮就变成零证据；落盘后从日志尾直接指认卡在哪个阶段，成功仍以「日志尾含完成标记」判定（保持静默）。**归因先做对照实验**：干净树上跑同一条 git 命令（`git add -A` 秒级返回＝git 本身不慢，慢在索引被打脏后的整树重哈希），别急着赖 gc 或网络。三类已治理的放大器：每档一次 CLI 启动的固定开销 → 单进程批量导出；**重建式采集**（清空目录再重拷——copy2 保得住 mtime，但文件重建让 ctime 全变，git 判索引失效、整树重哈希 ~3GB，实测提交段 8–10 分钟）→ 写前比对「内容相同不重写」＋ prune 收敛（覆盖层与 sessions 同一把刀）；`git gc --auto` 全量 repack（松散对象过阈值即整包重写、可耗十分钟）→ 压降每轮对象产出即自然摊薄，**勿关 `gc.auto`**（保持 git 默认行为）。
- **官方导入尺寸上限**（≤5MB/≤1万条/单次 ≤25MB 等）：面向 Web 场景；恢复工具在**本进程内**放宽这些常量（不改官方文件）。若官方改版导致常量改名，工具会记录并继续（检查 `hasattr`）。
- **导出不脱敏**：会话文件含对话原文与凭据讨论 → 仓必须永远私有。
- **备份范围（0.3.8 起）**：`kanban/` 只备数据库（`boards/*/kanban.db`，约 30MB）；任务运行工作区/证据/日志属过程记录**不入云**（实测占该目录 96%：758MB/4.3 万文件，含整份 git 克隆）——恢复后看板卡片与 runs 在、任务临时工作区不在（预期，别当缺件报）。**范围归 owner**：审计发现白名单外的数据（凭据、快照等）只**报告**——列出路径、体积、可否重取，由 owner 一句话定是否纳入；不擅自扩大或缩小备份范围（那是政策，不是技术优化）。
- **跨档只读边界（备份/审计/演练通用）**：审计与演练必然读到**别的 profile** 的库与会话文件——**读可以，写不行**。发现「镜像有、活库没有」的会话（外部回滚/手工清理造成）时：把文件复制到 `temp/hcb-lost-sessions-<date>/src/<profile>/`、写一份含导入命令的 README、然后**只报告不导回**——那是别的会话的数据，导入/删除都要 owner 明确点头。同理：恢复演练一律落**空白 `--home`**（先用 `mkdir -p <B>/home/profiles` 建好目录；`--source` 收的是含 `<profile>/` 的父目录），跑完 `rm -rf`，绝不指向真实 home。
- **桌面端 Local Storage 被占用**：群聊/置顶数据只有桌面端退出后才能安全覆盖 → 收尾器处理。
- **半壳档**（如 `profiles/.deleted`、只余 `state.db` 的已退役档）：导出会失败/跳过，属预期；采集时已排除 `.deleted`。
- **重启网关会连坐当前对话**：`hermes gateway restart` 由收尾器在桌面端关闭后执行，不要在对话中途手动跑。
- **后台轮的完成通知会滞后且成批到达**：通知里的命令输出可能对应**已被后续轮取代**的旧轮（失败也可能早被补推/修复）。处理前先对时——读 `state/hb-sync-runs.jsonl` 最后一行的轮次时间与 `state/hb-sync-health.json` 的 `updated_at`；旧通知若已被后继轮补齐，只记一笔、不重跑、不回滚已改配置。
- **改在跑 cron 的包装脚本＝先实跑一次再放手**：实案 10-23 00:55 `hb-autosync.py` 因缺 `import time` 一秒即失败（`NameError: name 'time' is not defined`，且 except 分支传了未绑定的 `started` 又抛 `UnboundLocalError` 把真因盖住）——此前改脚本**没实跑**（现成回归 `scripts/test-hb-health.py` 也没跑），错误一直留到下一个 2 小时触发点才暴露。规矩：改 `hb-sentinel.py` / `hb-autosync.py` 后，先在终端 `python <HERMES_HOME>/scripts/<脚本>.py` 实跑一次（或跑 `test-hb-health.py`）看 rc，再用 `hermes cron run` 核；`bump_unfinished` 一类兜底分支里的变量必须在 `try` 之前绑定。**另一面：双脚本互备确实救场**——本轮 00:55 周期件秒失败，哨兵（2 分钟）在 44 秒后自动补跑并成功（runs.jsonl 00:56:33 一轮 committed+pushed），所以周期件坏掉不必然造成备份缺口，判「有没有丢」看 runs.jsonl 而非 cron 状态；两枚脚本别合并成一只。
- **`runs.jsonl` 只记「进程内走到的出口」，被外部硬杀的那一轮没有行**（2026-10-24 实测）：`hb-autosync.py` 在 01:45:28 起跑、01:48:27 被网关 shutdown drain（`cron_drain_timeout=30s`）掐掉，`runs.jsonl` **没有这一行**（末行仍是 21:44 成功轮），`hb-sync-child.log` 的 mtime 停在 01:45:39。⇒ 「有没有丢」不能单看 runs.jsonl 的行数，必须三件交叉：`executions.db`（该轮 `unknown`）+ `hb-sync-child.log` mtime + 远端 HEAD 比对。上一轮遗留的未推提交照旧由下一轮补推，零丢失。
- **网关因「检出被换版本」自停也会连坐在跑轮次**：`gateway.log` 出现 `ERROR gateway.runtime_downgrade_exit: This gateway loaded …, but its checkout is now at …, a release that predates the gateway runtime` ⇒ 有人（update/回滚）把 checkout 切到更老的版本，网关按设计自杀，在跑的 cron 被 drain 掐掉。此时排查顺序是**先让检出稳定**（`git -C hermes-agent reflog` 看切版本的时刻与来源），不要去动备份脚本。
- **多机/线上领先**：push 被拒会自动 `pull --no-rebase` 重试一次；出现合并冲突则中止待人工（口径：线上优先、本地另存，勿 force）。

## 更新 / 重启后自检（约 5 分钟；官方版本变了必跑）

顺序固定，全部看完再下结论：

1. `hermes --version` 确认新版本与 commit；`tail logs/update.log` 看更新是否真的收尾（是否重启过网关）。
2. 网关：`gateway_state.json` 的 mtime 新鲜度 + `pid` + `platforms.*.state`（`weixin`/`qqbot` 应为 connected）+ 进程表里有且只有一个 `hermes_cli.main gatew*`。**`gateway_state.json` 只在 default home 一份**（多档共用一个 multiplexer）。
3. **`ESTOP` 文件在不在**（`%LOCALAPPDATA%/hermes/ESTOP`）：`hermes pause` 跨重启保留，会让 cron 静默停摆而网关看起来一切正常；在就 `hermes resume`。
4. cron 是否还在跳：读 `cron/executions.db` 最近几行（哨兵应每 2–3 分钟一条 `completed`），不要只信 `cron list` 的 Last Run。
5. 同步线：锁文件在不在 + `state/hb-sync-runs.jsonl` 最后一行 + `git ls-remote` 与本地 HEAD；健康文件 `consecutive_failures`。**更新后跑满一整天再判断稳定性**——更新期间那几轮会因资源争用变慢（实测 export 1181s / git 1367s），不是机制坏了。
6. 环境漂移：`hermes update` 会换 tools python（实测 3.14.7→3.14.9）并换出旧包——`python -c "import psutil"` 之类可选依赖要复验；`HERMES_HOME/scripts/` 里的运行副本与技能副本用 sha256 比对（更新不应覆盖，但要证）。**同时复核网关启动器**（`gateway-service/Hermes_Gateway.vbs`/`.cmd`）：它们内嵌安装时的解释器绝对路径，换 python 后路径失效 → 计划任务照报 `Last Result 0` 但网关根本没起来（`hermes gateway status` 报 `No gateway process detected`）；修法是 `hermes gateway install --start-on-login --no-start-now` 重生成启动器再 `schtasks /Run`，详见 `windows-autostart-ops`。
7. 配置仍在：`hermes config get sessions.max_export_messages` 应为 `0`（否则巨会话导出会整包失败且 rc=0 无产物）。
8. **恢复链路复验（最要紧）**：跑 `references/restore-drill.md` 的「低成本冒烟」两例（小档整档 + 一个巨会话），确认官方导入 API / schema 变动没有破坏恢复。

## Verification

- 同步后：`meta/sync-state.json` 的 `pushed` 为 true；`git ls-remote origin` 与本地提交一致。
- **每轮结构化记录**：`$HERMES_HOME/state/hb-sync-runs.jsonl` 一行一轮（时长／分段耗时 collect·export·git／退出码／committed·pushed／字节数）——追轮次历史、算成功率与分段成本先读它；连续失败计数在 `state/hb-sync-health.json`，≥3 轮失败写 `state/hb-sync-needs-attention` 标记（不推送、不打扰，成功一轮自动清）。
- 恢复后：置顶对话在；微信/QQ 发消息能续接旧线程；群聊记录在；各档 `hermes -p <档> sessions stats` 数量与备份一致；主档出词正常（provider 可用）。
- 全量验收入口：`templates/RESTORE.md` 第 6 节清单。
- **全量审计（大改后或定期）**：四路核对（维护笔记③）＋版本字符串全扫（如 `grep -rn '0\.3\.' <技能目录>`，捕捉状态行/示例漂移）＋点火一轮真同步核 `git ls-remote` 前进；线上设置与隐私面 → `github-security-audit` 技能。
- **完整性＝两条腿，缺一条都不算数**：①**演练**（镜像 → 沙箱，证明"能恢复"）②**覆盖率审计**（活库 → 镜像，证明"备的是全部"）。审计默认只审会话层；**要连文件层（`--with-files`）一起跑**才算覆盖配置/记忆/技能/平台数据那一半。判据一句话：缺会话／多会话／镜像条数超前／以解析失败＝**硬伤**（退出码 1），**落后＝正常增量**，孤立消息组＝活库自身残留不计。
- **审计的两类"非硬伤"要会读，别急着当漏备**：①**文件层轮内新建豁免**（0.4.13）——白名单目录/文件在镜像缺失，但源内容 mtime 晚于本轮 `started_at` 时，判「轮内新建、下一轮会采」，输出在 `· 轮内新建…` 一行、不计硬伤（实案：`desktop/interrupted_turns.json` 由桌面端在轮后写入，被误判为漏备）；②**镜像独有会话**（审计报「多会话」）——通常是活库被外部事件换过（state.db 回滚/还原、手工清理），不是漏备也不是采集错：下一轮会从最新树 prune，**git 历史仍保留**；要留档先另存文件（含分片），要导回用 `git show <sha>:sessions/main/<sid>.jsonl` 取出后走 `--source` 导入。看到「多会话」先问「活库最近是否被回滚过」，别从活库删东西。
- **审计 PASS 必须与负向对照成对出现**：改了审计器或采集白名单后跑 `scripts/test-audit-coverage.py`（8 场景合成夹具，断言"该红的会红"）。只有 PASS 没有负向对照＝**没有证据**（审计可能根本没在看）；断言要落在输出字段（`缺 N`／`新建豁免 N`／`超前 N`），只断言退出码会把"因别的原因失败"误当通过。
- **验收以「最后同步时刻」为界**：快照之后的新会话/改动不在备份里（属预期，下轮同步带上）。对账做三向差集——真机∩快照（应精确一致）、真机−快照（＝同步后新建）、快照−真机（＝曾删除；快照保留历史）；给用户解释时先讲这层边界，避免把预期差异当故障。
- **时间锚点**：报「损失窗口/新鲜度」前先实测当前时间（`date`）并按当下重算——隔夜或隔数轮后旧读数的时间锚（如「今晚」）即失效，禁止沿用旧时点表述。
- **沙箱演示/验收**：在 `temp` 下克隆＋恢复（`--home` 指向沙箱），全程不动真机；大件会话（>6MB）可先改名加 `.hold` 后缀暂缓，中小件先跑完出对账，再恢复原名单独补入（总量不变）。
- **恢复侧验收＝至少两遍独立全量演练、均 0 差异**（第二遍必须换仓状态＋换工具来源，不可省；配方、对账口径与判读坑 → `references/restore-drill.md`）。
