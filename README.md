# hermes-backup

把整个 Hermes Agent 备份到**本机的 git 镜像仓**（默认 `~/hermes-backup`，纯线下、不联网）；重装/换机后，在新的 Hermes 里一句话恢复。
Windows、**零必需第三方依赖**（Python 标准库 + PowerShell；`psutil` 属可选增强——装了才有更彻底的
进程树回收/孤儿清理，没装则自动降级，同步本身照常跑）。

## 它做什么

- **同步**（源机）：一条命令把 配置/凭据/记忆/技能/会话历史/桌面数据（群聊、置顶、聊天附图）采集进镜像工作区 → commit → push。
- **恢复**（新机，本机镜像同理）：从镜像仓 clone → 文件层恢复 → 会话层恢复（走 Hermes 官方导入管线）→ 平台路由回写 → 桌面端收尾器 → 重启即回到重装前。
- **私有保障**：推送前自动检查仓库可见性；非私有自动改回；改不回则拒绝推送。

## 安装

方式一（推荐，完整且最稳）：

```
git clone https://github.com/yechang1450/hermes-backup.git "%LOCALAPPDATA%\hermes\skills\hermes-backup"
```

方式二（Hermes 官方命令直装；会自动带上脚本与模板，网络不稳时重试）：

```
hermes skills install https://raw.githubusercontent.com/yechang1450/hermes-backup/main/SKILL.md
```

## 快速开始（源机）

1. 首次初始化（自动创建私有仓并首推）：

   ```
   python %LOCALAPPDATA%\hermes\skills\hermes-backup\scripts\sync.py --owner <你的GitHub用户名> --create-remote
   ```

2. 日常同步：

   ```
   python %LOCALAPPDATA%\hermes\skills\hermes-backup\scripts\sync.py
   ```

## 自检（改完/定期）

```bash
python scripts/selfcheck.py                          # 约 1 分钟：编译 + 全部 test-*.py 回归 + 文档引用 + 公开面卫生
python scripts/selfcheck.py --repo ~/hermes-backup  # 追加镜像分片体检（PARTS-OK）
python scripts/selfcheck.py --repo ~/hermes-backup --audit   # 追加会话层覆盖率审计（2–5 分钟）
```

末行 `SELFCHECK-PASS` / `SELFCHECK-FAIL`，退出码 0 / 2。**只读**：不写镜像、不碰活库、不联网。

日常该看什么、什么信号该动手 → `references/operations.md`；备份到底备了哪些 → `references/backup-scope.md`；
版本演进 → `CHANGELOG.md`。

## 恢复（新机）

把备份仓地址交给新机上的 Hermes Agent（或人）：

> 从我的 GitHub 私有仓库 `<用户名>/hermes-backup` 克隆到 ~/hermes-backup，读 RESTORE.md 按步骤恢复。

全程只有一步需要人工：中途完全退出一次桌面端（收尾器自动完成替换并重新拉起）。

## 前提

- Windows 11 + Hermes Agent（常规安装布局：`%LOCALAPPDATA%\hermes\` 下有 `hermes-agent/`、`bin/`、`tools/python-*/`）。
- Python 与 git 用 Hermes 自带的即可。**`gh`（GitHub CLI）实际必需**：建仓、解析属主与私有性检查都靠它；
  没有 gh 时同步会**拒绝推送**（可用 `--allow-unverified-privacy` 显式放行，但私有性就得你自己保证）。
  `gh` 不必在 PATH 里 —— 脚本会先查 PATH、再查官方安装位置（`%ProgramFiles%\GitHub CLI\gh.exe` 等），
  从备份恢复只拷了 exe 的情形也能用。

## 安全

- **备份仓必须永远保持私有**：内容含凭据与私人对话。
- 技能本体不含任何密钥；全部操作在本机执行；唯一网络行为是推送到你自己的仓库。
- 技能目录下的 `local.json` 是机器本地配置，不进入任何仓库。可选键 `preserve_sessions`：
  列出「本机已退役、但备份要原样留档」的 profile 档名（例如只恢复 default 档的新机上那批旧 bot 档）；
  列进去的档不计入 shrink guard 的分母、不参与整档删除判定、prune 时原样保留。**不要把 `main` 写进去。**

## 许可与权利

Copyright © 2026 yechang1450. All rights reserved.

No additional license or permission is granted. 本仓未提供开源许可：
源码公开可读，但不构成任何使用、复制、修改或再分发的授权；如需授权请另行联系作者。

受 GitHub 服务条款约束的浏览与 fork 不受此声明影响。
