# 数据仓「换代」归档流程（预案，2026-10-21 立）

> 用途：GitHub 仓体积接近官方建议线时，换一代新仓继续同步，旧仓冻结为只读历史档案。
> 原则：**不 force-push、不重写任何已推送历史、不删除旧仓**（旧代=完整历史，永久可克隆）。
> 线上系统的同步**全程不停**（换代是「新仓 + 推一次全量 + 切默认名 + 归档旧仓」，无停机步骤）。

## 为什么要换代
- GitHub 官方：仓库体积**理想 <1GB、强烈建议 <5GB**（无公开硬上限，但影响 clone 与基础设施）；
- git 的 pack 只增不减（历史永久保留），所以「缩体积」只有两条路：重写历史（**禁用**）或换代（本流程）；
- 换代把「历史负担」留在旧代仓里，新代仓从单次快照起步，体积回到 ~1.5GB 级。

## 触发条件（任一满足即执行）
1. `gh api repos/<owner>/<repo> --jq .size` ≥ **4,500,000**（≈4.5GB）；
2. 全新克隆耗时 > 20 分钟（同一网络条件下）；
3. GitHub Support 发来体积提醒邮件。

## 手工步骤（等价，供核对/兜底；约 1–2 小时，可分两次做）
```bash
# 0) 前置：镜像干净、本地=远端、无同步在跑（锁不存在）
git -C D:/hermes-backup status --porcelain          # 应为空
git -C D:/hermes-backup rev-parse HEAD              # 与 ls-remote 一致
# 1) 压缩本地对象（可选但建议；让首次推送的包更小）
git -C D:/hermes-backup gc --quiet
# 2) 建新代私有仓（名字：hermes-backup-<YYYYqN>，如 hermes-backup-2026q4）
gh repo create <owner>/hermes-backup-2026q4 --private
# 3) 从镜像推一份全量到新仓
git -C D:/hermes-backup remote add gen2 git@github.com:<owner>/hermes-backup-2026q4.git
git -C D:/hermes-backup push -u gen2 main
# 4) 校验：远端 HEAD 与本地一致；另起干净目录克隆抽查（文件数 + 恢复冒烟）
git -C D:/hermes-backup ls-remote gen2 HEAD
# 5) 切默认：把 mirror 的 origin 指向新仓 + 同步脚本默认 repo-name 改为新名
git -C D:/hermes-backup remote set-url origin git@github.com:<owner>/hermes-backup-2026q4.git
#    编辑 scripts/sync.py 的 --repo-name 默认值 → 新代名；发新版本（升 version）
# 6) 跑一轮真实同步，确认提交落到新仓（git ls-remote origin HEAD == 本地 HEAD）
# 7) 归档旧仓（只读，但仍可克隆）
gh api -X PATCH repos/<owner>/hermes-backup -f archived=true
# 8) 文档：RESTORE.md 注明「本仓为第 N 代；最新代：<name>」
```
回退：任一步失败 → `remote set-url origin` 指回旧仓、默认名改回，旧仓未归档前零影响。

## 一条命令（0.4.6 起，推荐）

```bash
# 先看计划（只读，不动任何东西）
bash scripts/rotate-generation.sh --from <当前代仓名> --to <新代仓名> --dry-run
# 真跑（幂等、可续跑；全程持 sync 锁，sync 会主动让路）
bash scripts/rotate-generation.sh --from <当前代仓名> --to <新代仓名>
# 可选：--mirror D:/hermes-backup  --owner <gh-user>  --skip-archive
```

脚本做的事＝下面手工步骤 1–8 的自动化版本，额外保证：
- 前置检查（工作树只允许 `meta/`、`logs/` 的轮后改动；无同步锁；local==remote）；
- **孤儿快照单提交**（新仓历史清零，体积=当前树，避开单次 push 2GB 限制）；
- 浅克隆抽查（文件数必须与 `git ls-files` 一致、抽一个完整会话能解析）；
- 切换后**写 `local.json` 的 `repo` 字段**（0.4.6 起 sync.py 的默认仓名来自这里，不再写死在代码里）；
- 幂等：若远端已有同一快照提交，跳过推送直接继续（失败后重跑即续接）。

## 恢复侧口径（多代并存）
- **恢复一律用最新一代仓**（RESTORE.md 在每一代仓内都随附）；
- 需要更早的历史数据：克隆旧代仓（archived 仍可克隆）→ 按其内 RESTORE.md 恢复；
- 各代之间不合并、不交叉推送。

## 现状记录（每次换代时更新）
| 代号 | 仓名 | 启用日期 | 停用原因 | 归档状态 |
|---|---|---|---|---|
| 第 1 代 | `hermes-backup` | 2026-10-14 | 体积 4.28GB 接近 GitHub 建议线（2026-10-21 实测）；已按本流程换代 | **archived（只读档案，可克隆）** |
| **第 2 代** | **`hermes-backup-2026q4`** | **2026-10-21** | **当前在用**（孤儿快照单提交种子 508143dd；快照 11,406 文件 / 1,384 会话件） | 在用 |
| 第 3 代 | （待建） | — | — | — |
