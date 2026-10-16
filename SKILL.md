---
name: hermes-cloud-backup
description: Back up/restore Hermes state via a private GitHub repo.
version: 0.2.3
author: yechang1450, Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [backup, restore, github, cloud, sync]
    related_skills: []
---

# hermes-cloud-backup — Hermes 数据云备份 / 恢复

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
| 会话层恢复 | `python <REPO>/tools/run_in_runtime.py <REPO>/tools/restore_sessions.py --all` |
| 拼包（给桌面端导入用） | `python <REPO>/tools/concat_sessions.py main` |
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
- `templates/RESTORE.md` — 新机恢复手册（编号步骤 + 验收清单）

## 流程 A：日常同步（源机）

1. 在 `terminal` 里跑 `python <SKILL>/scripts/sync.py`（后台跑也可以；其日志在 `<镜像>/logs/`）。
   完成标准：末尾出现 `sync 完成：… committed=… pushed=True`，且 `git ls-remote` 与本地 HEAD 一致。
2. 铁规：**永不 force-push**；推送前脚本自检仓库私有性（gh 可用时；非私有会尝试改回、失败即中止推送）。
3. 节流建议：每日 1 次或按需；同一天高频跑的意义有限（只传变化的会话文件，开销小但不必要）。

## 流程 B：新机恢复（目标机）

**严格按 `templates/RESTORE.md` 的编号步骤执行**。要点顺序（此顺序有依赖，勿打乱）：

1. clone 备份仓到本机（`git clone git@github.com:<owner>/hermes-backup.git D:\hermes-backup`）。
2. 文件层覆盖（含覆盖前自动备份旧件到 `restore-out/pre-restore-backup/`）。
3. 会话层导入（走官方 `SessionDB.import_sessions` 管线；本工具进程内放宽官方导入尺寸上限，以恢复含超大档在内的全部历史；已存在的会话自动跳过，可重复跑）。
4. 路由回写（微信/QQ 线程 → 会话映射）。
5. 桌面端收尾器（**唯一需要用户配合的一步：完全退出桌面端一次**；收尾器自动完成剩余拷贝、重启网关、重新拉起桌面端）。
6. 重启后按 RESTORE.md 的验收清单逐项核对。

## Pitfalls

- **超大会话导出守卫**：`sessions export` 默认对 >20000 条消息的会话整包报错（且 rc=0 无产物）。官方解法 = `hermes config set sessions.max_export_messages 0`（本机已设）。
- **GitHub 单文件 100MiB 硬限**：会话导出件按「每会话一个 gz」拆分存放（最大单件 ~61MB）；切勿改回整包。
- **官方导入尺寸上限**（≤5MB/≤1万条/单次 ≤25MB 等）：面向 Web 场景；恢复工具在**本进程内**放宽这些常量（不改官方文件）。若官方改版导致常量改名，工具会记录并继续（检查 `hasattr`）。
- **导出不脱敏**：会话文件含对话原文与凭据讨论 → 仓必须永远私有。
- **桌面端 Local Storage 被占用**：群聊/置顶数据只有桌面端退出后才能安全覆盖 → 收尾器处理。
- **半壳档**（如 `profiles/.deleted`、只余 `state.db` 的已退役档）：导出会失败/跳过，属预期；采集时已排除 `.deleted`。
- **重启网关会连坐当前对话**：`hermes gateway restart` 由收尾器在桌面端关闭后执行，不要在对话中途手动跑。
- **多机/线上领先**：push 被拒会自动 `pull --no-rebase` 重试一次；出现合并冲突则中止待人工（口径：线上优先、本地另存，勿 force）。

## Verification

- 同步后：`meta/sync-state.json` 的 `pushed` 为 true；`git ls-remote origin` 与本地提交一致。
- 恢复后：置顶对话在；微信/QQ 发消息能续接旧线程；群聊记录在；各档 `hermes -p <档> sessions stats` 数量与备份一致；主档出词正常（provider 可用）。
- 全量验收入口：`templates/RESTORE.md` 第 6 节清单。
- **验收以「最后同步时刻」为界**：快照之后的新会话/改动不在备份里（属预期，下轮同步带上）。对账做三向差集——真机∩快照（应精确一致）、真机−快照（＝同步后新建）、快照−真机（＝曾删除；快照保留历史）；给用户解释时先讲这层边界，避免把预期差异当故障。
- **沙箱演示/验收**：在 `temp` 下克隆＋恢复（`--home` 指向沙箱），全程不动真机；大件会话（>6MB）可先改名加 `.hold` 后缀暂缓，中小件先跑完出对账，再恢复原名单独补入（总量不变）。
