---
name: hermes-cloud-backup
description: Back up/restore Hermes state via a private GitHub repo.
version: 0.4.6
author: yechang1450, Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [backup, restore, github, cloud, sync]
    related_skills: []
---

# hermes-cloud-backup — Hermes 数据云备份 / 恢复

> **维护笔记（发布前必读）**：①官方策展器会持续回写本技能文件（回写带 CRLF）；发布时把技能目录拷入暂存后先 `sed -i 's/\r$//'` 归一（git 的 `text=auto` 在提交侧也会归一 blob），再用「全新克隆 ↔ 暂存」逐字节比对核实——**判 CRLF 用 python 字节计数，不要用 MSYS grep（管道层会造假 CR）**。②公开仓默认分支为 **main**（`git push origin HEAD`）。③一致性四处：技能目录 ↔ 暂存 `temp/hcb-publish` ↔ 公开仓 ↔ 数据镜像 tools/。④改会话存储格式或恢复工具时，同步更新**镜像根 README** 的格式描述（该文件不被 sync 自动生成；手改后随下轮同步提交）。

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
| 覆盖率审计（镜像 vs 活库） | `python <SKILL>/scripts/audit-coverage.py --repo <REPO> --home <HERMES_HOME>`（PASS 打印 `AUDIT-PASS`，硬伤退出码 1） |
| 数据仓换代（体积到阈值） | `bash <SKILL>/scripts/rotate-generation.sh --from <旧代> --to <新代> --dry-run` 先看计划，再去掉 `--dry-run` 真跑（见 references/generations.md） |
| 路由回写（微信/QQ 续接） | `python <REPO>/tools/apply_routing.py` |
| 桌面端收尾器 | `pwsh -NoProfile -File <REPO>/tools/finish_restore.ps1 -Repo <REPO>` |

> 机器本地配置（镜像目录等）放技能目录 `local.json`（不进任何仓库）；镜像目录默认自动探测，`--mirror` 可指定。

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
- `scripts/audit-coverage.py` — 覆盖率审计：镜像（会话文件） vs 活库（state.db）逐会话对账；判据＝不缺/不多/不超前（"落后"是上一轮快照后的正常增量，"孤立消息组"单列）
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
3. 节流建议：每日 ≤1–2 次或按需。**v0.3 存储格式已落地（0.3.0）**：会话原样存文本、>90MB 自动分片，每轮入历史的增量从 ~226MB 降到 ~0.01–2.25MB/文件级（实测）——提高频率（自动同步）已无体积顾虑。

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

- **跑会 import `hermes_state` 的脚本必须经 `tools/run_in_runtime.py`**：在本机终端里 `python <脚本>.py` 会被启动器以**另一个解释器**（缺 ruamel 等运行期依赖）经 runpy 重跑，报 `ModuleNotFoundError: No module named 'ruamel'`；就是 `python -c "import <模块>"` 也不能豁免（`SessionDB(...)` 构造会把进程切到工具树 python）。正确形态一律是 `python <REPO>/tools/run_in_runtime.py <脚本> [参数]`——恢复、对账、A/B 测试脚本皆同。同理：**恢复/对账不要与 `hermes update` 并发**，更新收尾持有 checkout 锁，卡住的子进程会 0% CPU 空转（判据：RSS 几 MB、CPU 0、日志停在启动横幅）。
- **`sync.py` 的 home 只按 `LOCALAPPDATA` 解析（不读 `HERMES_HOME` 环境变量）**：`HERMES_HOME` 只影响两个包装脚本（哨兵/周期）与 hook 的路径。写单测时必须**注入 `sync.HERMES_HOME / HEALTH_FILE / ATTENTION_MARKER / RUNS_LOG`** 模块属性，否则会写进真实 `state/`（2026-10-23 实测踩过：把假失败记进了真健康文件）。
- **超大会话导出守卫**：`sessions export` 默认对 >20000 条消息的会话整包报错（且 rc=0 无产物）。官方解法 = `hermes config set sessions.max_export_messages 0`（新机上首次同步前设置一次即可）。
- **GitHub 单文件 100MiB 硬限**：会话导出件按会话拆分；v0.3 起原样文本存储、>90MB 的巨会话按 90MB 切片（`<sid>.part-NNNN.jsonl`）——历史体积问题见下条。
- **历史体积机制（关键）**：git 仓库＝最新快照＋**每个被改文件的每一版**——同步的「强制覆盖」语义只作用于最新树（≈镜像工作区），**挡不住历史堆积**（实测：全历史 blob 2.01GB vs 最新树 0.72GB；单会话 10 个版本＝313MB）。根因＝会话导出是**超长单行 JSON**、头部元数据每轮必变 × 整包 gzip → deflate 级联 → 版本间字节重叠 **0%** → git 增量全失效 → 每轮 ~226MB 完整入库（仓库 ~0.5GB/天）。**已治理（v0.3，0.3.0）＝原样文本格式：同数据对照实测 +79.40MB/轮 → +0.01MB（整件）/ +2.25MB（分片）级；机理与三方案对照见 `references/session-storage.md`。** 单文件 95MB 同步闸由分片格式顺带拆除（分片后最大件 90MB）；体积读数用**本地对象库**实测（GitHub `size` 字段有滞后）。
- **意外中断与并发自愈**：自带单实例防撞锁——**v3.2 起锁内记录持有者 pid：持有进程已消失（触发端被掐/崩溃）立即接管，3h 陈旧窗仅兜底**（早期版本纯 3h 窗口＝被掐后可能空窗最长 3 小时）；被强杀遗留的 `.git/index.lock`（>120s）自动清除并重试一次。真机实测：进程在 `git add` 瞬间被杀 → 下一轮同步报 `rc=128` —— 自愈代码即为覆盖该情形；被掐轮次的**未推提交由下一轮随带补推（零丢失）**。- **网关重启与在跑轮次的三种组合**（先取证据、都别慌）：① drain 杀掉父脚本、`sync.py` 作为**脱离进程继续跑完**——`hermes cron runs` 会停在 `running` 或落 `unknown`，**判轮次死活只认锁文件里的 pid 存活**（存活＝还在跑、会自行落地；已死＝孤儿锁由下一轮立即接管）；② 在途提交被掐——判「有没有丢数据」**不看 run 状态**，用 `git merge-base --is-ancestor <该轮提交> <远端HEAD>`：命中＝已进远端历史、零丢失；未命中才去查采集/推送段；③ 父脚本被杀时外层超时兜底一并消失 ⇒ 该轮不受 `script_timeout` 约束，必要时人工盯或回收。另：两轮相撞时后来者**立即让路**（日志 `已有另一轮同步在进行（锁年龄 Ns）——本轮退出，让路`）——设计行为，不是失败。
- **推送断连（exit 3）≠ 没推上去**：`send-pack: unexpected disconnect while reading sideband packet` / `Connection to ssh.github.com closed by remote host` 是传输层断线——GitHub 可能已收完整包并更新 ref，只是客户端没拿到确认。**判据只认远端口**：`git ls-remote origin main`（或 `… HEAD`）与 `git rev-parse HEAD` 比对；**别拿本地 `origin/main` 跟踪引用当远端**（失败的 push 不更新它，两种误读都会发生）。一致 ⇒ 零丢失、失败仅是确认缺失：不重推、不 force、不改机制，`hb-sync-health.json` 记 1 次失败、下轮照常；不一致 ⇒ 提交仍在本地、下一轮自动补推。**判「这轮为什么慢」先看在跑 worker 数**：十几张看板 worker 并行时导出段会涨 2–3 倍（实测 485–627s → 1481s），整轮逼近外层预算但仍在预算内，别当卡死。
- **失败诊断锚**：同步失败先读 ① `meta/sync-state.json` 的 `git_error` / `committed` / `pushed` / `finished_at`（失败路径也会写 meta）；② 镜像 `logs/last-run.log`（每轮全量日志）。**提交里的 meta 滞后一版**——write_meta 在 commit 之后执行，所以最新提交里包含的是**上一轮**的 sync-state（工作区 `meta/sync-state.json` 常显 modified，属正常，随下轮提交）。
- **慢轮诊断（阶段预算＋廉价探针）**：正常档一轮 5–8 分钟（收集 1–2 分钟 / 批量导出 ~2 分钟 / 拆分＋`git add` 秒级 / 提交与推送 1–3 分钟）；明显超出即按段找放大器。**`logs/last-run.log` 在轮次结束时整份落盘，中途没有文件可读** ⇒ 要么等落地，要么用廉价探针：锁文件 pid 存活（用 `psutil.Process(pid)` 判定——`ps -W` 只显示可执行名、不含命令行参数，`ps -W | grep 脚本名` 永远匹配不到 python 子进程）、`.git/index.lock` 存在＝正在 add/commit、`git count-objects -v` 的 loose 数、包文件 mtime（repack 痕迹）、进程 IO/CPU（psutil）。**轮次未结束时不要对镜像跑全树重探针**（`du -sm`、`git status`、递归 ls）：持索引锁时 `git status` 会挂住、大目录 `du` 会跑满超时。日志内建阶段行：`git 提交完成：Ns（含 add 与自动 gc）`、`git 推送完成：Ns`。**包装脚本必须把子进程输出流式落盘**（`stdout=<文件句柄>`，不要 `capture_output`）：超时被掐的那一刻 `capture_output` 会连子进程 stdout 一起丢，超时轮就变成零证据；落盘后从日志尾直接指认卡在哪个阶段，成功仍以「日志尾含完成标记」判定（保持静默）。**归因先做对照实验**：干净树上跑同一条 git 命令（`git add -A` 秒级返回＝git 本身不慢，慢在索引被打脏后的整树重哈希），别急着赖 gc 或网络。三类已治理的放大器：每档一次 CLI 启动的固定开销 → 单进程批量导出；**重建式采集**（清空目录再重拷——copy2 保得住 mtime，但文件重建让 ctime 全变，git 判索引失效、整树重哈希 ~3GB，实测提交段 8–10 分钟）→ 写前比对「内容相同不重写」＋ prune 收敛（覆盖层与 sessions 同一把刀）；`git gc --auto` 全量 repack（松散对象过阈值即整包重写、可耗十分钟）→ 压降每轮对象产出即自然摊薄，**勿关 `gc.auto`**（保持 git 默认行为）。
- **官方导入尺寸上限**（≤5MB/≤1万条/单次 ≤25MB 等）：面向 Web 场景；恢复工具在**本进程内**放宽这些常量（不改官方文件）。若官方改版导致常量改名，工具会记录并继续（检查 `hasattr`）。
- **导出不脱敏**：会话文件含对话原文与凭据讨论 → 仓必须永远私有。
- **备份范围（0.3.8 起）**：`kanban/` 只备数据库（`boards/*/kanban.db`，约 30MB）；任务运行工作区/证据/日志属过程记录**不入云**（实测占该目录 96%：758MB/4.3 万文件，含整份 git 克隆）——恢复后看板卡片与 runs 在、任务临时工作区不在（预期，别当缺件报）。
- **桌面端 Local Storage 被占用**：群聊/置顶数据只有桌面端退出后才能安全覆盖 → 收尾器处理。
- **半壳档**（如 `profiles/.deleted`、只余 `state.db` 的已退役档）：导出会失败/跳过，属预期；采集时已排除 `.deleted`。
- **重启网关会连坐当前对话**：`hermes gateway restart` 由收尾器在桌面端关闭后执行，不要在对话中途手动跑。
- **多机/线上领先**：push 被拒会自动 `pull --no-rebase` 重试一次；出现合并冲突则中止待人工（口径：线上优先、本地另存，勿 force）。

## Verification

- 同步后：`meta/sync-state.json` 的 `pushed` 为 true；`git ls-remote origin` 与本地提交一致。
- **每轮结构化记录**：`$HERMES_HOME/state/hb-sync-runs.jsonl` 一行一轮（时长／分段耗时 collect·export·git／退出码／committed·pushed／字节数）——追轮次历史、算成功率与分段成本先读它；连续失败计数在 `state/hb-sync-health.json`，≥3 轮失败写 `state/hb-sync-needs-attention` 标记（不推送、不打扰，成功一轮自动清）。
- 恢复后：置顶对话在；微信/QQ 发消息能续接旧线程；群聊记录在；各档 `hermes -p <档> sessions stats` 数量与备份一致；主档出词正常（provider 可用）。
- 全量验收入口：`templates/RESTORE.md` 第 6 节清单。
- **全量审计（大改后或定期）**：四路核对（维护笔记③）＋版本字符串全扫（如 `grep -rn '0\.3\.' <技能目录>`，捕捉状态行/示例漂移）＋点火一轮真同步核 `git ls-remote` 前进；线上设置与隐私面 → `github-security-audit` 技能。
- **验收以「最后同步时刻」为界**：快照之后的新会话/改动不在备份里（属预期，下轮同步带上）。对账做三向差集——真机∩快照（应精确一致）、真机−快照（＝同步后新建）、快照−真机（＝曾删除；快照保留历史）；给用户解释时先讲这层边界，避免把预期差异当故障。
- **时间锚点**：报「损失窗口/新鲜度」前先实测当前时间（`date`）并按当下重算——隔夜或隔数轮后旧读数的时间锚（如「今晚」）即失效，禁止沿用旧时点表述。
- **沙箱演示/验收**：在 `temp` 下克隆＋恢复（`--home` 指向沙箱），全程不动真机；大件会话（>6MB）可先改名加 `.hold` 后缀暂缓，中小件先跑完出对账，再恢复原名单独补入（总量不变）。
- **恢复侧验收＝至少两遍独立全量演练、均 0 差异**（第二遍必须换仓状态＋换工具来源，不可省；配方、对账口径与判读坑 → `references/restore-drill.md`）。
