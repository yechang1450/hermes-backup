# hermes-cloud-backup

把整个 Hermes Agent 备份到你的**私有 GitHub 仓**；重装/换机后，在新的 Hermes 里一句话恢复。
Windows、**零必需第三方依赖**（Python 标准库 + PowerShell；`psutil` 属可选增强——装了才有更彻底的
进程树回收/孤儿清理，没装则自动降级，同步本身照常跑）。

## 它做什么

- **同步**（源机）：一条命令把 配置/凭据/记忆/技能/会话历史/桌面数据（群聊、置顶、聊天附图）采集进镜像工作区 → commit → push。
- **恢复**（新机）：从 GitHub clone → 文件层恢复 → 会话层恢复（走 Hermes 官方导入管线）→ 平台路由回写 → 桌面端收尾器 → 重启即回到重装前。
- **私有保障**：推送前自动检查仓库可见性；非私有自动改回；改不回则拒绝推送。

## 安装

方式一（推荐，完整且最稳）：

```
git clone https://github.com/yechang1450/hermes-cloud-backup.git "%LOCALAPPDATA%\hermes\skills\hermes-cloud-backup"
```

方式二（Hermes 官方命令直装；会自动带上脚本与模板，网络不稳时重试）：

```
hermes skills install https://raw.githubusercontent.com/yechang1450/hermes-cloud-backup/main/SKILL.md
```

## 快速开始（源机）

1. 首次初始化（自动创建私有仓并首推）：

   ```
   python %LOCALAPPDATA%\hermes\skills\hermes-cloud-backup\scripts\sync.py --owner <你的GitHub用户名> --create-remote
   ```

2. 日常同步：

   ```
   python %LOCALAPPDATA%\hermes\skills\hermes-cloud-backup\scripts\sync.py
   ```

## 自检（改完/定期）

```bash
python scripts/selfcheck.py                          # 约 1 分钟：编译 + 9 套回归 + 文档引用完整性
python scripts/selfcheck.py --repo D:/hermes-backup  # 追加镜像分片体检（PARTS-OK）
python scripts/selfcheck.py --repo D:/hermes-backup --audit   # 追加会话层覆盖率审计（2–5 分钟）
```

末行 `SELFCHECK-PASS` / `SELFCHECK-FAIL`，退出码 0 / 2。**只读**：不写镜像、不碰活库、不联网。

日常该看什么、什么信号该动手 → `references/operations.md`；备份到底备了哪些 → `references/backup-scope.md`；
版本演进 → `CHANGELOG.md`。

## 恢复（新机）

把备份仓地址交给新机上的 Hermes Agent（或人）：

> 从我的 GitHub 私有仓库 `<用户名>/hermes-backup` 克隆到 D:\hermes-backup，读 RESTORE.md 按步骤恢复。

全程只有一步需要人工：中途完全退出一次桌面端（收尾器自动完成替换并重新拉起）。

## 前提

- Windows 11 + Hermes Agent（常规安装布局：`%LOCALAPPDATA%\hermes\` 下有 `hermes-agent/`、`bin/`、`tools/python-*/`）。
- Python 与 git 用 Hermes 自带的即可。**`gh`（GitHub CLI）实际必需**：建仓、解析属主与私有性检查都靠它；
  没有 gh 时同步会**拒绝推送**（可用 `--allow-unverified-privacy` 显式放行，但私有性就得你自己保证）。

## 安全

- **备份仓必须永远保持私有**：内容含凭据与私人对话。
- 技能本体不含任何密钥；全部操作在本机执行；唯一网络行为是推送到你自己的仓库。
- 技能目录下的 `local.json` 是机器本地配置，不进入任何仓库。

## License

MIT
