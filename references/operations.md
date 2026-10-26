# 日常运维一页纸

> 目标：每天 30 秒确认「备份还活着」，出事时 3 分钟内知道先看哪里。命令都在技能目录 `scripts/` 下。

## 每天（30 秒）

```bash
# 1) 最近几轮有没有成功（一条一行）
tail -3 "$HERMES_HOME/state/hb-sync-runs.jsonl"

# 2) 健康账本（连续失败数必须是 0）
cat "$HERMES_HOME/state/hb-sync-health.json"

# 3) 有没有待处理标记（存在=连续 3 轮失败）
ls "$HERMES_HOME/state/hb-sync-needs-attention" 2>/dev/null && cat "$HERMES_HOME/state/hb-sync-needs-attention"
```

**正常长相**：`runs.jsonl` 末行 `exit 0`、`pushed true`、`duration_s` 250–500；`consecutive_failures=0`；无 marker。

## 每周（2 分钟）

```bash
python scripts/selfcheck.py --repo D:/hermes-backup     # 编译+9 套测试+文档引用+分片体检+覆盖率审计
python scripts/churn-report.py --repo D:/hermes-backup  # 这一轮实际重写多少（形状是否合理）
gh api repos/<owner>/<repo> --jq .size                  # 远端体积（判容量只认它）
git -C D:/hermes-backup ls-tree -r -l main | awk '{s+=$4} END {printf "%.2fGB\n", s/1073741824}'   # 树实量
```

## 什么时候该动手

| 信号 | 判读 | 动作 |
|---|---|---|
| `consecutive_failures` ≥1 | 上一轮真失败（先看 `last_error`） | 看 `state/hb-sync-child.log` 末尾 30 行；`database is locked`＝瞬时，等下一轮；`push` 超时＝网络，下一轮自愈 |
| marker 存在（≥3 轮） | 连续失败，数据没坏但没上云 | 处理根因后删 marker，跑一轮 `python scripts/sync.py` 验证 |
| `exit 3` + `connection closed by 198.18.*` | fake-ip 拦截（代理窗口） | 关掉代理或换 `ssh.github.com:443`；下一轮自愈 |
| 树体积增速 > 基线（约 9.2MB/小时） | 某个大文件在整份重写 | 跑 `churn-report.py`，看 `>5MB` 里是否同一会话多片 |
| 一轮超 30 分钟还没结束 | 导出阶段按「进展」判定：有进展会一直续期（硬上限 60 分钟），无进展 20 分钟才杀 | 看 `hb-sync-child.log` 停在哪一步、`hb-bulk-export.log` 有无报错 |
| 一轮超 15 分钟还没结束 | 有人/别的活在外面抢磁盘（导出阶段实测 210–600s） | 先看 `hb-sync-child.log` 停在哪一步；**别在轮内跑 `selfcheck --audit` 或大测试**（会把它拖到 1200s 预算超时） |
| 分片末片 ≠ 固定边界 | 阈值/代码变了但数据没重切 | 跑一轮同步让边界归位；`check-parts.py` 复核 |
| 远端 `.size` 接近 12GB | 该换代了 | 按 `references/generations.md` 跑 `rotate-generation.sh --dry-run` 再真跑 |

## 定期动作

- **换代**：`.size` ≥ 12GB（或克隆 >20 分钟）→ `rotate-generation.sh --from <旧> --to <新>`（先 `--dry-run`）。
- **本地仓整理**：镜像 `.git` 已设 `gc.auto=0`；当 `git -C D:/hermes-backup count-objects -v` 的 `count` > 20,000 时，
  在**没有同步在跑**的空窗跑 `git -C D:/hermes-backup gc --quiet`。
- **恢复演练**：每季度一次，按 `references/restore-drill.md` 走一遍（全新克隆 → 恢复一档 → 对账 0 差异）。

## 失败告警怎么接到人

默认 `--deliver local`（只落盘）。想真正收到通知，三种：

1. **投到某个 bot 的会话**：`hermes cron create "every 2h" --name "hermes-backup 周期同步" --script hb-autosync.py --no-agent --deliver bot-chat:<profile>` —— 失败摘要会作为消息进该档的 Bot Chat。
2. **投到平台**：`--deliver platform:<chat_id>`（微信/QQ 已连通的会话），手机直接收到。
3. **不接通知**：靠上面的"每天 30 秒"人工看（本机当前就是这种）。

> 改投递只改 cron 的 `--deliver`，不要改脚本；脚本的输出契约是「成功=空，失败=一段摘要」。

## 出事了按这个顺序看

1. `state/hb-sync-runs.jsonl` 末行 → 哪一轮、多久、exit、pushed
2. `state/hb-sync-child.log` 末 50 行 → 卡在哪个阶段（collect/export/git）
3. `state/hb-sync-health.json` → `last_error` 原文
4. `logs/last-run.log`（镜像内）→ 本轮完整日志
5. 还有疑问 → `python scripts/selfcheck.py --repo D:/hermes-backup`

## 推送前（30 秒，三道）

```bash
python scripts/privacy-scan.py                          # 本技能目录
python scripts/privacy-scan.py --repo <仓> --history    # 目标仓（含全历史）
```

判据：`PRIVACY-OK` 才算过。**约定**（owner 2026-10-26）：私有仓日常 push 不必请示；公开 / Release /
npm publish / 加协作者与部署密钥才是需要授权的对外动作。不改写已推历史——历史里发现旧痕迹，只能
在后续版本移除并在 CHANGELOG 记一笔。
