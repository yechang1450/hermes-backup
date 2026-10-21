# RESTORE.md — 新机恢复操作手册（给 Agent 逐步执行）

> **多代口径**：数据仓按"代"归档（各代名与启用/归档时间以技能 `references/generations.md` 登记为准）。
> 恢复**一律用最新一代仓**（其内 RESTORE.md 为准）；旧代仓已 `archived`（只读，仍可克隆），仅供追溯更早历史。
>
> **未验证通道（明示）**：桌面端「导入」按钮未经实操验证（需 Hermes 全停 5–10 分钟，用户裁定不做）；
> 其数据载体（`concat_sessions.py` 拼包）已随每轮演练验证，导入按钮本身请在用时现场核验。

> 场景：刚装好全新 Hermes 的新机（重装/换机），要把备份仓 = 旧机器的完整状态恢复回来。
> 执行者：本机的新 Hermes Agent（或用户本人）。原则：**每步可核验、每步可回滚；顺序有依赖，不要打乱。**
> 全程只有一步需要用户配合：第 5 步退出桌面端（收尾器会自动完成并重新拉起）。

## 0. 前置检查

- **取得备份仓访问权**（私有仓，需与备份来源同一 GitHub 账号）：
  - 有 gh：`gh auth login`（clone 走 HTTPS 时由 gh 代管凭据）。
  - 无 gh：用 SSH —— `ssh-keygen -t ed25519` 生成新密钥 → 把公钥加到 GitHub 账号 → `ssh -T git@github.com` 验证。
- git 可用：若本机没有 git，可直接用 Hermes 自带的 PortableGit（`%LOCALAPPDATA%\hermes\tools\git-*\`，干净系统也有），或先装 Git for Windows。
- 本机 Hermes 已至少启动过一次（`%LOCALAPPDATA%\hermes\` 骨架存在，含 `hermes-agent/` 与 `bin/`）。

## 1. 克隆备份仓

```
git clone <备份仓地址> D:\hermes-backup        # 例如 git@github.com:<owner>/hermes-backup.git
```
（克隆目录任意；下文以 `D:\hermes-backup` 为例。）
完成标准：`D:\hermes-backup\home-overlay\`、`profiles-overlay\`、`sessions\`、`tools\` 在位。

## 2. 恢复文件层（配置/凭据/记忆/技能/cron/看板 + 各 bot 档）

```
python D:\hermes-backup\tools\restore_files.py --repo D:\hermes-backup
```
- 默认覆盖 `home-overlay/` → `%LOCALAPPDATA%\hermes\`，`profiles-overlay/<档>/` → `profiles\<档>\`。
- 被覆盖的旧文件自动备份到 `restore-out\pre-restore-backup\`（要回滚就从这里拿）。
- 完成标准：输出「覆盖 N 文件」且无报错；`HERMES_HOME\memories\MEMORY.md`、`config.yaml`、`.env` 已就位（重启后生效）。

## 3. 恢复会话层（全部历史对话）

```
python D:\hermes-backup\tools\run_in_runtime.py D:\hermes-backup\tools\restore_sessions.py --all
```
- 走官方 `SessionDB.import_sessions` 管线（与桌面端「导入」按钮同一代码路径）；进程内放宽官方导入尺寸上限以恢复超大档。
- 已存在的会话自动跳过（幂等）；逐个会话打印 `+ 已导入 / = 已存在`。
- 超大档（数万条消息）会排在最后、耗时较长（可至数十分钟），属正常；可后台跑。
- 若个别会话报 `database is locked`（桌面端在写库）：在桌面端退出后的空窗期重跑本步即可，已导入的自动跳过。
- 完成标准：末尾 `failed=0`，且 `imported + skipped` 与本仓会话数一致（新格式＝每会话 `.jsonl`，>90MB 的巨会话为 `.part-NNNN.jsonl` 分片；旧备份为 `.jsonl.gz`，均自动识别）。

## 4. 恢复补缺件（微信/QQ 线程映射）

```
python D:\hermes-backup\tools\apply_routing.py
```
- 把 `supplement\gateway_routing.json` 写回本机 `state.db` 的 `gateway_routing` 表（scope 自动改成本机路径）。
- 若报 database is locked：等桌面端退出后的空窗期重跑（收尾器阶段）。
- 完成标准：输出 `applied: new=N updated=N unchanged=N`。

## 5. 桌面端收尾（需要用户退出桌面端一次）

先启动收尾器（独立进程，不受对话进程影响）：

```
pwsh -NoProfile -Command "(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='pwsh -NoProfile -File \"D:\hermes-backup\tools\finish_restore.ps1\" -Repo \"D:\hermes-backup\"'}).ReturnValue"
```

然后对用户说：
> 现在请**完全退出 Hermes**（托盘图标也要退出）。退出后收尾器会自动完成最后一步，并把 Hermes 重新拉起来（最多等 20 分钟）。如果稍后没有自动打开，手动双击桌面 Hermes 图标即可。

收尾器动作：等待桌面端退出 → 覆盖 `desktop-overlay/` → `%APPDATA%\Hermes\`（群聊记录/置顶/bot 元数据/聊天附图）→ 重启网关 → 重新拉起桌面端。全程日志：`D:\hermes-backup\logs\finish-restore.log`；完成标记：`restore-out\FINISH-DONE.txt`。

## 6. 重启后验收清单（逐项核对，出对账）

1. 置顶对话回来了（会话页置顶区）。
2. 微信/QQ 通道连上（`hermes gateway status`），发消息确认**续接旧线程**。
3. 群聊记录与成员元数据在。
4. 各 bot 档：`hermes -p <档名> sessions stats` 数量与备份一致。
5. 主 provider 实测出词（按恢复后的配置能正常调用）。
6. 桌面端设置与你记忆一致（主题/布局/偏好）。

## 7. 收尾

- 恢复完成 = 本机成为线上版本的后代：此后 `python <技能目录>\scripts\sync.py` 继续日常同步。
- 自动同步：v0.3+ 备份内含 `home-overlay/scripts/hb-*.py`，且 `cron/` 任务随备份恢复——重启后自动同步会自行继续；若备份来自旧版本，按技能 `references/auto-sync.md`「安装」节补装。
- 若本机在恢复前产生了新数据：先 `git pull` 合并再同步；同名冲突 = 线上优先、本地另存（人工裁决）。

---
*本手册随 hermes-cloud-backup 技能分发；修改时保持每步可核验、可回滚。*
