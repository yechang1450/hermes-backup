# RESTORE.md — 新机恢复操作手册（给 Agent 逐步执行）

> **多代口径**：数据仓按"代"归档（各代名与启用/归档时间以技能 `references/generations.md` 登记为准）。
> 恢复**一律用最新一代仓**（其内 RESTORE.md 为准）；旧代仓已 `archived`（只读，仍可克隆），仅供追溯更早历史。
>
> **未验证通道（明示）**：桌面端「导入」按钮未经实操验证（需 Hermes 全停 5–10 分钟，用户裁定不做）；
> 其数据载体（`concat_sessions.py` 拼包）已随每轮演练验证，导入按钮本身请在用时现场核验。

> 场景：刚装好全新 Hermes 的新机（重装/换机），要把备份仓 = 旧机器的完整状态恢复回来。
> 执行者：本机的新 Hermes Agent（或用户本人）。原则：**每步可核验、每步可回滚；顺序有依赖，不要打乱。**
> 全程只有一步需要用户配合：第 5 步退出桌面端（收尾器会自动完成并重新拉起）。

**先设一个变量，后面所有命令都用它**（git-bash；`$HOME` 在 Windows 上就是 `C:/Users/<你>`）：

```bash
export HB="$HOME/hermes-backup"
```

> ⚠️ **路径写法铁律**：bash 里一律用**正斜杠**。写成 `~/hermes-backup\tools\restore_files.py`
> 会被 bash 吃掉反斜杠、拼成 `~/hermes-backuptoolsrestore_files.py` 直接报"找不到文件"。
> `--repo`/`--home` 也都写成 `"$HB"` 这种加引号的变量，别手敲 Windows 反斜杠路径。

## 0. 前置检查

- **取得备份仓访问权**（私有仓，需与备份来源同一 GitHub 账号）：
  - 有 gh：`gh auth login`（clone 走 HTTPS 时由 gh 代管凭据）。
  - 无 gh：用 SSH —— `ssh-keygen -t ed25519` 生成新密钥 → 把公钥加到 GitHub 账号 → `ssh -T git@github.com` 验证。
- git 可用：若本机没有 git，可直接用 Hermes 自带的 PortableGit（`%LOCALAPPDATA%\hermes\tools\git-*\`，干净系统也有），或先装 Git for Windows。
- 本机 Hermes 已至少启动过一次（`%LOCALAPPDATA%\hermes\` 骨架存在，含 `hermes-agent/` 与 `bin/`）。

## 1. 克隆备份仓

**两种克隆方式，按需要选：**

```bash
# A) 全量（要恢复全部档、全部文件时用）
git clone --depth 1 <备份仓地址> "$HB"

# B) 只要某几档的会话时：部分克隆 + 稀疏检出
git clone --filter=blob:none --no-checkout <备份仓地址> "$HB"
cd "$HB" && git sparse-checkout init --cone
git sparse-checkout set sessions/main tools        # 需要哪档就写哪档（仓里没有 templates/）
git checkout
```
> B 的收益只在"只要一部分"时成立；要恢复全部档就用 A，别折腾稀疏。
> `--depth 1` 够用：恢复只读当前树，不需要历史（要从历史捞旧会话见第 8 节，那时再 `git fetch --unshallow`）。

**预检（30 秒内，不写任何东西）**：

```bash
git -C "$HB" rev-parse --short HEAD
git -C "$HB" ls-files | wc -l                      # 应有万级文件
python "$HB/tools/check-parts.py" --repo "$HB"      # 分片形态体检（PARTS-OK）
```

完成标准：`home-overlay/`、`profiles-overlay/`、`sessions/`、`tools/`、`supplement/` 在位。

## 2. 恢复文件层（配置/凭据/记忆/技能/cron/看板 + 各 bot 档）

```bash
# 先看计划（只读，不建目录、不写文件、不做备份）：
python "$HB/tools/run_in_runtime.py" "$HB/tools/restore_files.py" --repo "$HB" --dry-run

# 确认无误再真覆盖（新机 HERMES_HOME 还不存在时才需要 --create）：
python "$HB/tools/run_in_runtime.py" "$HB/tools/restore_files.py" --repo "$HB" --create
```
- 默认覆盖 `home-overlay/` → `%LOCALAPPDATA%\hermes\`，`profiles-overlay/<档>/` → `profiles\<档>\`。
- 目标 `HERMES_HOME` 不存在时会**拒绝**；新机请先启动一次 Hermes 生成骨架。
- 被覆盖的旧文件自动备份到 `restore-out\pre-restore-backup\`（要回滚从这里拿）。
- 完成标准：输出「覆盖 N 文件」且无报错；`HERMES_HOME\memories\MEMORY.md`、`config.yaml`、`.env` 已就位（重启后生效）。

## 2b. 若新机用户名与旧机不同（强烈建议做）

备份里的 `config.yaml`（hook 路径）、`cron/jobs.json`（workdir）、各 bot 档 config 与 `scripts/` 里的脚本
都写死了旧机器的用户目录。新机用户名不同（甚至只是换了盘符）时，这些引用会**静默失效**——
hook 不加载、cron 找不到脚本。先查再改：

```bash
python "$HB/tools/run_in_runtime.py" "$HB/tools/check-paths.py" --home "$LOCALAPPDATA/hermes"
#   rc=0 → 全部等于本机当前用户目录，不用管
#   rc=2 → 列出非本机的旧前缀（如 C:\Users\旧名），需要改写

# 真改写（先备份 .bak-paths-<时间>，只动白名单文本件，不碰 .db/日志/会话）：
python "$HB/tools/run_in_runtime.py" "$HB/tools/check-paths.py" --home "$LOCALAPPDATA/hermes" --fix --dry-run
python "$HB/tools/run_in_runtime.py" "$HB/tools/check-paths.py" --home "$LOCALAPPDATA/hermes" --fix
```
- 用户名相同就可以跳过本节（跑一次只读检查确认 rc=0 即可）。
- 改完要**重启 Hermes** 才生效（配置在进程启动时读取）。

## 3. 恢复会话层（全部历史对话）

```bash
python "$HB/tools/run_in_runtime.py" "$HB/tools/restore_sessions.py" \
  --all --fast-import --repo "$HB" --home "$LOCALAPPDATA/hermes"
```
- 走官方 `SessionDB.import_sessions` 管线（与桌面端「导入」按钮同一代码路径）；进程内放宽官方导入尺寸上限以恢复超大档。
- **必须传 `--repo`**：不传时默认取脚本上一级目录，只有"直接跑仓里那份 tools/"才对得上。
- 已存在的会话自动跳过（幂等）；逐个会话打印 `+ 已导入 / = 已存在`。
- 超大档（数万条消息）排在最后，耗时较长（可至数十分钟），属正常；**建议后台跑**（重定向到日志后 `tail -f`）。
- 若个别会话报 `database is locked`（桌面端在写库）：在桌面端退出后的空窗期重跑本步，已导入的自动跳过。
- 完成标准：末尾 `failed=0`，且 `imported + skipped` 与本仓会话数一致（新格式＝每会话 `.jsonl`，>12MB 的巨会话为 `.part-NNNN.jsonl` 分片；旧备份为 `.jsonl.gz`，均自动识别）。

## 4. 恢复补缺件（微信/QQ 线程映射）

```bash
python "$HB/tools/run_in_runtime.py" "$HB/tools/apply_routing.py" --repo "$HB" --dry-run   # 先看 new/updated/unchanged
python "$HB/tools/run_in_runtime.py" "$HB/tools/apply_routing.py" --repo "$HB"
```
- 把 `supplement/gateway_routing.json` 写回本机 `state.db` 的 `gateway_routing` 表（scope 自动改成本机路径）。
- **前置**：主档会话必须先恢复（第 3 步跑过），否则 `<HERMES_HOME>\state.db` 还不存在，本步报「目标库不存在」——这是正常的顺序依赖，不是故障。
- 重复执行幂等（第二次应全 unchanged）。
- 完成标准：输出 `applied: new=N updated=N unchanged=N`。

## 5. 桌面端收尾（需要用户退出桌面端一次）

先启动收尾器（独立进程，不受对话进程影响）：

```bash
pwsh -NoProfile -File "$HB/tools/start-finish-restore.ps1" -Repo "$HB"
# 只想看它会执行什么（不启动）：
pwsh -NoProfile -File "$HB/tools/start-finish-restore.ps1" -Repo "$HB" -DryRun
```

> 别自己内联拼 `Invoke-CimMethod ... CommandLine`：bash → pwsh → 字符串三层转义，反斜杠错一个就 ParserError
> （2026-10-29 实测）。壳脚本已把拼接做掉，退出码：0=已拉起，2=找不到收尾器。

然后对用户说：
> 现在请**完全退出 Hermes**（托盘图标也要退出）。退出后收尾器会自动完成最后一步，并把 Hermes 重新拉起来（最多等 20 分钟）。如果稍后没有自动打开，手动双击桌面 Hermes 图标即可。

收尾器动作：等待桌面端退出 → 覆盖 `desktop-overlay/` → `%APPDATA%\Hermes\`（群聊记录/置顶/bot 元数据/聊天附图）→ 重启网关 → 重新拉起桌面端。全程日志：`"$HB/logs/finish-restore.log"`；完成标记：`restore-out/FINISH-DONE.txt`。

## 6. 重启后验收清单（逐项核对，出对账）

1. 置顶对话回来了（会话页置顶区）。
2. 微信/QQ 通道连上（`hermes gateway status`），发消息确认**续接旧线程**。
3. 群聊记录与成员元数据在。
4. 各 bot 档：`hermes -p <档名> sessions stats` 数量与备份一致。
5. 主 provider 实测出词（按恢复后的配置能正常调用）。
6. 桌面端设置与你记忆一致（主题/布局/偏好）。

**核库时注意两件事**（2026-10-29 演练踩到）：
- **main 档**的库是 `<HERMES_HOME>/state.db`；**其它档**在 `<HERMES_HOME>/profiles/<档>/state.db`。
- 镜像里 `sessions/<档>/*` 的**文件数 ≥ 会话数**才对（一个 >12MB 的会话会被拆成多片 `part-NNNN`），别拿文件数当会话数直接对等。

```bash
python - <<'PY'
import sqlite3, os, glob
home = os.path.expandvars(r"%LOCALAPPDATA%\hermes")
dbs = [os.path.join(home, "state.db")] + sorted(glob.glob(os.path.join(home, "profiles", "*", "state.db")))
for db in dbs:
    if not os.path.exists(db):
        continue
    c = sqlite3.connect(db)
    try:
        s = c.execute("select count(*) from sessions").fetchone()[0]
        m = c.execute("select count(*) from messages").fetchone()[0]
        u = c.execute("select count(*) from messages where message_uid is null or message_uid=''").fetchone()[0]
        print(f"{db}: 会话 {s} / 消息 {m} / uid 空 {u} / {c.execute('pragma quick_check').fetchone()[0]}")
    except Exception as e:
        print(db, "→", e)
PY
```

## 7. 收尾

- 恢复完成 = 本机成为线上版本的后代：此后 `python <技能目录>/scripts/sync.py` 继续日常同步。
- 自动同步：备份内含 `home-overlay/scripts/hb-*.py`，且 `cron/` 任务随备份恢复——重启后自动同步会自行继续；若备份来自旧版本，按技能 `references/auto-sync.md`「安装」节补装。
- 若本机在恢复前产生了新数据：先 `git pull` 合并再同步；同名冲突 = 线上优先、本地另存（人工裁决）。

---

*本手册随 hermes-cloud-backup 技能分发；修改时保持每步可核验、可回滚。*

## 8. 从 git 历史取回"已被 prune 的会话"（活库后来把它删了）

镜像只跟活库同步：活库删掉的会话，下一轮会从**最新树**里 prune（历史仍在）。
如果某段对话是外部事件弄丢的（state.db 回滚/还原、手工清理），可以从历史里捞回来：

```bash
cd "$HB"
git fetch --unshallow 2>/dev/null || true            # --depth 1 克隆的仓先补历史
SID=20261024_113906_c858a0                            # 会话 id（文件名去掉 .jsonl / .part-NNNN）
git log --oneline -3 -- "sessions/main/$SID.jsonl"    # 找一个仍有它的提交
git show <sha>:"sessions/main/$SID.jsonl" > "$LOCALAPPDATA/Temp/$SID.jsonl"   # 分片则逐片取 part-0000…
python -c "import json,os;d=json.load(open(os.path.join(os.environ['LOCALAPPDATA'],'Temp','$SID.jsonl')));print(len(d['messages']))"
```

再把文件放进 `<src>/main/` 目录，用
`python "$HB/tools/run_in_runtime.py" "$HB/tools/restore_sessions.py" --source <src> --profile main --fast-import --verify --home "$LOCALAPPDATA/hermes"`
导回活库（导入是**追加**，不改现有会话；导入前先 `cp state.db state.db.bak-<时间>`）。

实测（2026-10-24）：取回 `20261024_113906_c858a0` → 1,815,974 字节、277 条消息，与镜像一致。
