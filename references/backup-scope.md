# 备份范围（唯一权威表）

> 改了这里 = 改产品行为。任何"要不要备 X"的讨论以本表为准；其他文档只许指向本文件，不再各写一份。
> 代码事实源：`scripts/sync.py` 顶部的 `HOME_FILES` / `HOME_DIRS` / `PROFILE_FILES` / `PROFILE_DIRS` / `ROAMING_*`，以及 `copy_kanban_dbs()`。

## 1. 主档（`HERMES_HOME`，落在镜像 `home-overlay/`）

| 类别 | 具体 | 说明 |
|---|---|---|
| **单文件** | `config.yaml` · `.env` · `auth.json` · `profile.yaml` · `SOUL.md` · `channel_directory.json` · `projects.db` · `shared-state.db` · `kanban.db` · `sessions/sessions.json` | 配置与凭据都在这里，**仓必须私有** |
| **目录** | `memories` · `cron` · `skills` · `kanban` · `weixin` · `pairing` · `platforms` · `hooks` · `vault` · `desktop` · `bot_relay` · `assets` · `pending` · `pending_messages` · `state` · `scripts` · `.curator_backups` | 整树覆盖（`skills/.hub` 只留两枚小锁文件） |
| **豁免** | `.reclaim-*` / `__pycache__` / `*.pyc` / `*.log`（`logs/` 除外）/ 各类临时件 | 见 `ignored()` |

## 2. 各 bot 档（`HERMES_HOME/profiles/<档>/`，落在 `profiles-overlay/<档>/`）

| 类别 | 具体 |
|---|---|
| 单文件 | `config.yaml` · `.env` · `auth.json` · `profile.yaml` · `SOUL.md` · `projects.db` |
| 目录 | `memories` · `cron` · `skills` · `pairing` · `platforms` · `plans` · `skins` · `workspace` · `hooks` · `assets` · `.curator_backups` |

## 3. 会话（落在 `sessions/<档>/`）

- 来源＝官方 `hermes sessions export --format jsonl`（单进程批量导出，与 CLI 逐字节一致）。
- 文件形态：`<sid>.jsonl`；>12MB 切成 `<sid>.part-NNNN.jsonl`（固定偏移边界）。
- **没有任何条数上限**（导入侧才有限制，恢复工具在进程内放宽）。

## 4. 桌面端（落在 `desktop-overlay/`）

`%APPDATA%/Hermes/` 的精选件：`Local Storage`（群聊记录/置顶/bot 元数据）、`composer-images`（聊天附图）、设置类 json；
以及 `%LOCALAPPDATA%/hermes-desktop` 的 `Local State` / `Preferences` 等。

## 5. 看板（落在 `home-overlay/kanban/`）

- **只备数据库**：`boards/<board>/kanban.db` + `boards/_archived/<board>/kanban.db` + 顶层 `kanban.db`。
- 用 SQLite **在线备份 API** 取一致快照（WAL 里已提交的事务也带上）。
- **不备**任务运行工作区 / 证据 / 日志——实测占该目录 96% 体积（约 730MB/4.3 万文件，含整份 git 克隆）。
  ⇒ 恢复后"看板卡片与 runs 在、任务临时工作区不在"，属预期。

## 6. 补缺件（落在 `supplement/`）

- `gateway_routing.json`：`state.db` 的 `gateway_routing` 表（微信/QQ 线程 → 会话映射），**按 session_key 去重**（优先本机 scope）。

## 7. 明确不备（及原因）

| 不备 | 原因 |
|---|---|
| `state.db` / 各档 `state.db` | 会话改由 `sessions/` 导出件承载（体积大、可重建） |
| 会话附件的原文（非 composer-images） | 体积不可控；恢复后缺图属已知限制 |
| `logs/*.log`、`cache/`、`scratch/` | 过程件 |
| `secrets/`（如 `wan30-tokenplan.key`） | 政策：由 owner 决定；当前**不备** |
| `.env.bak-*` / `config.yaml.bak-*` | 回滚当天的临时备份 |
| `kanban` 任务工作区 | 见 §5 |

## 8. 改范围的规程

1. 改 `sync.py` 的清单常量（**别只改文档**）。
2. 同步更新本表，并在 `CHANGELOG.md` 记一行（为什么改）。
3. 跑 `python scripts/selfcheck.py --repo <镜像>` 全绿。
4. 真跑一轮，用 `python scripts/audit-coverage.py --repo <镜像> --home <HERMES_HOME> --with-files` 核"新范围内无缺件、范围外不多备"。
5. **范围归 owner**：审计发现白名单外的新数据，只报告（路径/体积/可否重取），不擅自扩缩。
