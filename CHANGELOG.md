## 1.1.3 — 2026-10-31 · 纯本机模式不再碰 remote ＋ 运行副本回归"壳"

- **`ensure_repo(need_remote=...)`**：纯本机模式（无 `--push`）**完全不碰 remote**——不再要求解析备份仓地址，
  也不再每轮把已删除的远端地址写回 `origin`。此前每轮都会重建 origin，留下
  `refs/remotes/origin/HEAD → 0000000…` 的空指针，`git fsck` 直接 rc=2。
- **`HERMES_HOME\scripts\sync.py` 恢复成"转调壳"**：换机后发现它是整份副本（88KB），已与技能源漂移
  （缺分块渲染、缺内存硬闸、缺纯本机模式），而手工轮与 cron 轮打的都是这份旧的。现在只有 914 字节，
  直接 `runpy` 执行技能目录那份，改一处即生效。原文件已备份 `.bak-20261031-shim`。

## 1.1.2 — 2026-10-31 · 验收口径对齐纯本机模式

- 还有三处假判据残留（`SKILL.md` 两处、`references/operations.md` 一处）写着「末行 `pushed=True`」——
  纯本机模式下 `pushed` 恒为 `null`，照着这条验收会把**正常轮判成失败**。统一改成
  「`committed=True` + 镜像 HEAD 前进」；只有显式 `--push` 的轮次才核 `pushed=True` 与 `git ls-remote`。

## 1.1.1 — 2026-10-31 · 修文档完整性误报

- `SKILL.md` 指向**另一个技能**的 `hermes-mechanics/scripts/session-ctl.py`，被 selfcheck 的文档引用检查
  判成悬空（它只按本仓文件核对）⇒ 把该文件名加进 `EXTERNAL_NAMES` 豁免清单。自检复跑 **16 项 / 0 失败 / 63.4s**。

## 1.1.0 — 2026-10-31 · 改名 hermes-backup ＋ 转纯本机线下备份

主人令：`hermes-cloud-backup` 全量改名 `hermes-backup`；备份改为**只在本机**做，GitHub 上的**数据仓删除**，
只保留技能源码仓并同步改名。

- **改名**：技能目录 / frontmatter `name` / 全部文档与脚本字符串（63 处、29 个文件）/ `HERMES_HOME\scripts\`
  运行副本的 `SYNC_SCRIPT` 路径 / 项目目录 `build/hermes-backup` 全部切换；旧名零残留（`.git` 内部除外）。
- **转纯本机**：`sync.py` 默认**只 commit 到本地镜像、不推送**（新增 `--push` 才推，`--no-push` 保留兼容但已是默认）；
  `local.json` 增 `mode: "local-only"`、`repo` 置空（防误推到**公开**的技能仓）。
- **GitHub 收口**：gen-1（archived，4.5GB）先 `git clone --mirror` 落到
  `temp/gen1-archive/gen1-hermes-backup.git`（63 提交）再删；gen-2 删前已证本地 `main/master/pre-395-takeover`
  三个 ref 全在（本地 HEAD == 远端）；技能仓 `hermes-cloud-backup` → `hermes-backup`（仍 public）。
- **内存硬闸**（新机 64GB 统一内存）：导出前查可用内存——**<3GB 跳过本轮（exit 2）**、**<8GB 退逐档 CLI**；
  配合既有的分块渲染（`CHUNK=4`）与 stabilize 的 2.4× 守卫，杜绝整机 OOM。
- **本机实测的一课**：`--allow-shrink` 是逃生口不是常规参数。新机第一轮曾把它当默认用，`sessions/main`
  被砍 2,078→1,596 文件，39 个「只在镜像、从未提交」的会话文件永久丢失（本机单盘、无副本）。

## 1.0.22 — 2026-10-31 · 退役 profile：从「会被清空」到「原样留档」

新机只恢复 default 一档、16 个 bot 档全部退役后的真实事故。同一件事踩了两道闸：

- **shrink guard 永久拒绝**：`_count_live_sessions` 对不存在的 `profiles/<name>/` 报 0
  ⇒ 1522（活的）/ 3431（备份）= 44.3% < 50%，每轮 `exit 6`，镜像一个字节都推不上去。
- **`_deletable_profiles` 判「可整档删」**：同样因为档目录整个不存在 ⇒ 放开闸就会把
  17 个档 1909 个会话文件 `rmtree`，再撞批量删除闸（33.9% > 30%）⇒ 白跑一轮。
- **`profiles-overlay` 被整层清空 8,284 个文件**（这一条最致命，且与上两条独立）：
  `collect_profiles` 用「本轮访问过什么」反推该删什么，活库 0 档 ⇒ 什么都没访问 ⇒
  全删。2026-10-31 18:52 实案：工作树 `profiles-overlay/` 变成 0 文件，靠
  `git checkout -- .` 5 秒回滚；00:18 那轮同样是它，只因 gh 拒推才没提交。

修法（三处，判据都只读 `local.json`，代码里不写死档名）：

- `preserve_sessions` 新配置项：列出的档**不计入 shrink 分母、不参与整档删除判定、
  prune 时原样保留**（历史仍按「镜像独有、可随时 `git show` 取回」）。`main` 不允许写进去。
- `prune_unvisited(root, keep_paths=...)`：新增保留子树参数，用于护住退役档目录。
- `collect_profiles`：活库 **0 档**且镜像非空时**整层跳过清理**并告警——「源侧没有」
  在新机上不等于「该删」。这是与 shrink guard 同源的第二道零值保护。

顺带：`_gh_exe()` 先看 PATH、再查官方安装位置（`%ProgramFiles%\GitHub CLI\gh.exe` 等）。
从备份恢复只拷了 exe、没走安装器时 PATH 是空的，`shutil.which` 返回 None ⇒ `ensure_private`
fail-closed 拒推（00:18 那轮正是被它救了，但也因此永远推不上去）。

测试：`test-shrink-guard.py` 加 S7（退役档放行 + 不进可删档，6 项）；
`test-prune-protect.py` 加 S5（keep 子树保留）、S6/S6b（零档整层保留、有活档时退役档仍在）。

## 1.0.21 — 2026-10-31 · 两路审计后的静默失败清扫

两路子代理只读审计（文档↔代码漂移 / 全新克隆可跑性 / 代码级静默失败 / 公开面卫生）后的一批修复。
共同主题：**「打印成功」与「真的做了」之间的缝**。

- `sync.py` 批量导出路径原本直接 `return`：某档 0 产出（`guard_zero_sessions` 已标 `ok=False`）
  也照样走到「sync 完成」+ `exit 0`，包装器还会把失败计数清零 ⇒ 现在批量路径同样统计
  `failed_profiles` 并按 `exit 5` 记账。
- `sync.py` 提交前新增**批量删除闸**（`MassDeleteError`）：一轮里要删掉 ≥100 个且 >30% 的已跟踪文件
  ⇒ 判误删、中止提交、`git reset` + `git checkout -- .` 把镜像回滚到 HEAD（未跟踪的新文件保留），`exit 6`。
  这是对「任何一层被整批清掉」的兜底（会话闸只数会话）。
- `sync.py` 整档删除授权收紧（`_deletable_profiles`）：只有**活库里连档目录都没了**才 `rmtree`；
  `profile.yaml` 暂时缺失/改名的档一律保留（原逻辑会整档清掉）。
- `sync.py` 采集阶段兜异常：`PermissionError`/磁盘满/库被锁原来直接 traceback，runs.jsonl、健康计数、meta 全不写
  ⇒ 现在按 `exit 2` 记账。
- `sync.py` 推送前校验分支必须是 `main`（否则提交到别支却 push main，`pushed=True` 而远端没变）。
- `write_meta(..., success=False)`：失败轮与 `--collect-only` **不再写 `finished_at`**（它是「上次成功同步」
  的唯一凭据，被覆盖会把哨兵的 20 分钟新鲜度闸与 overdue 判断一起重置）。
- 静默失败清扫（各自都有「空集/拼错/坏文件仍报成功」的原缺陷）：
  `audit-coverage.py`（0 档目录 ⇒ 硬伤；读不到采集白名单 ⇒ 硬伤）、`restore_files.py`（`--what` 拼错 ⇒ rc=2）、
  `concat_sessions.py`（0 个会话 ⇒ rc=2）、`hb-sentinel.py`（锁文件解析不出 pid ⇒ 按陈旧锁）、
  `selfcheck.py`（0 套测试 ⇒ 失败）、`export_all_profiles.py`（官方运行时改为**延迟导入**，`--help` 不再建骨架/等 33s）。
- 新增 `scripts/test-commit-guards.py`（7 项：批量删除闸 4 + 整档删除授权 3，假 git 不碰真仓）。
- 文档对齐：`ROTATE_AT_GB` 12→**8**（与 `generations.md`「触发条件」一致）、README 删掉与 `UNLICENSED` 矛盾的
  `## License / MIT`、`--fast-import` 统一 2–4 倍、`restore_sessions --repo` 默认值描述改正、写死的「N 套/N 项/N 个场景」
  改为指路、文件清单补 `check-paths.py`/`start-finish-restore.ps1`、`.gitignore` 加 `*.bak-*`。

## 1.0.20 — 2026-10-31 · 新机/未恢复保护（shrink guard）

换新机后的一次真实事故倒逼出来的闸门：新装 Hermes 的活库只有 87 个会话，哨兵按 overdue 自动跑了一轮，
把镜像里 3,603 个会话中的 2,311 个文件、8,284 个 profile 文件、1,569 个 home 文件当成「残留」清掉；
**只因 `gh` 不可用拒绝推送，备份才没被覆盖**（工作树被清空、未提交）。原有的零会话闸
（`guard_zero_sessions`）只看「0 个」，拦不住「有一点但极少」。

- `sync.py` 采集前新增 `shrink_guard()`：活库会话数 < 备份的 50%（且备份里已有 ≥20 个会话）⇒
  **拒绝本轮、镜像一个字节都不动**，`exit 6`，`report.aborted=shrink_guard`，原因里给两侧数字与缺口最大的档。
- 计数口径：镜像按**会话 id** 计（多分片只算一次），且取「工作树」与「`git ls-tree HEAD`」的**较大值**——
  否则工作树一旦被误 prune 过，基线就跟着缩水，闸门永远不响。
- 活库读不到（`state.db` 损坏/被占用）的档记为未知、不参与判定；一个档都读不到时放行（不猜）。
- 新增 `--allow-shrink`：确要用本机现状覆盖备份时显式放行（例如主人确认「旧数据不要了」）。
- 新增 `scripts/test-shrink-guard.py`（6 场景 8 项断言，含「工作树被清空仍按 HEAD 拦得住」）。

## 1.0.19 — 2026-10-30 · 选择性恢复 + 巨会话瘦身

同机重装（而不是换新机）时，整机覆盖是错的做法：会把本机更新的配置回退掉。补一套「挑着恢复」的流程。

- 新增 `references/selective-restore.md`：先 clone 再谈方案 → 给出带体积/会话数的可选菜单 → 配置文件**逐键合并**
  → 执行顺序与回滚。含会话续接（`session_key`/`chat_id`/`origin_json` 回填）、配对名单
  （`platforms/pairing/<platform>-approved.json` 不随 `--what home,profiles` 走）、巨会话翻页三节。
- 新增 `scripts/compare-config.py`：备份的 `config.yaml` 与本机的逐键差异，只读，用来先看差异再决定合并哪些键。
- 新增 `scripts/split-archive-session.py`：把 `active=0` 的归档行迁到独立「归档会话」（`--apply` 才写库，幂等可回滚），
  用于巨会话降体量；文档同时写明**它不等于 UI 修好**。
- SKILL 正文：`gh` 的定位改准（只在日常同步里需要，恢复全程不需要）；新增「流程 B′」；三条实测坑入正文。

## 1.0.18 — 2026-10-29 · 全量恢复演练实测回填

用一次「全新克隆 → 空 home」的**全量**演练拿到真实数字（此前手册里是估算）：

- 克隆 **476s** / 14,719 文件；文件层 **8s** / 覆盖 10,700；会话层 **426s** / **imported=3538、failed=0、verify_failed=0**；
  补缺件 3s。**合计约 15 分钟**，产出 **17 档 / 3,538 会话 / 580,902 条消息**。
- 逐档对账：**全部 uid 空 0、`quick_check` ok**，且每档「镜像文件数 ≥ 恢复会话数」（差值＝被拆片的巨会话）。
- 手册删掉旧的「可至数十分钟」估算与两套并存的历史数字，只留本次实测基准。

## 1.0.17 — 2026-10-29 · 恢复期路径修正

- 新增 `scripts/check-paths.py`：扫出恢复后残留的**旧机器绝对路径**（config.yaml 的 hook、cron 的 workdir、
  各 bot 档 config、scripts/），默认只报告（rc=0 无残留 / rc=2 有非本机前缀），`--fix` 才改写（先 `.bak-paths-<时间>`，
  只动白名单文本件，不碰 `.db`/日志/会话）。实测：本机 41 个文件 113 处全部等于当前用户目录 → rc=0；
  假 home 用 `<盘>:\Users\<旧用户名>` 造样本 → 报告 rc=2、`--fix` 4/4 改写且保留原斜杠风格。
- `RESTORE.md` 增加 **2b 节**：新机用户名与旧机不同时先查后改（用户名相同也要跑一次只读检查）。
- `TOOL_FILES` 收录 `check-paths.py`，保证新机克隆后 `tools/` 里就有它（已实测计数与 `--help`）。

## 1.0.16 — 2026-10-29 · 新机实战前的恢复手册重写

明天要在新机器上实战恢复，把 `templates/RESTORE.md` 按「照着敲就能跑」重写：

- **路径写法**：全篇改成正斜杠 + `export HB="$HOME/hermes-backup"` 变量。原文写成
  `~/hermes-backup\tools\restore_files.py`，bash 会吃掉反斜杠拼成 `~/hermes-backuptoolsrestore_files.py`，
  照抄必报"找不到文件"。
- **参数补齐**：会话层补 `--repo "$HB" --home "$LOCALAPPDATA/hermes"`；补缺件同样补 `--repo`；
  明确 `--create` 只有 `restore_files.py` 有（`restore_sessions.py` 传它会 rc=2）。
- **稀疏检出列表**去掉 `templates`（数据仓根本没有该目录，根目录只有 `RESTORE.md`）。
- **第 5 步改为壳脚本** `tools/start-finish-restore.ps1`：原来内联拼 `Invoke-CimMethod ... CommandLine`
  经 bash→pwsh 三层转义必然 ParserError（实测），壳里已把拼接做掉，退出码 0=已拉起 / 2=找不到收尾器。
- **验收节**补两条实测坑：非 main 档的库在 `profiles/<档>/state.db`；镜像文件数 ≥ 会话数（巨会话分片）。
- `TOOL_FILES` 增加 `start-finish-restore.ps1`，保证数据仓里有它（否则新机克隆后第 5 步无脚本可用）。

## 1.0.15 — 2026-10-29

- 文档与仓库同步：README 里的 `D:/hermes-backup` 全部改成 `~/hermes-backup`（保留 owner 的许可与权利声明）；
  SKILL.md 的"换盘变慢"那段以技能目录的**改正版**为准（旧版把长尾归因成磁盘慢）；
  新增 `references/host-migration.md`（换盘/换目录后"链路指到新址 + 数据一件没少"的审计方法）。

## 1.0.14 更正 — 2026-10-29

- **版本号错位已修**：1.0.13 那次只改了 CHANGELOG 标题，`SKILL.md` 的 `version:` 仍停在 1.0.12
  （替换误打在了 CHANGELOG 变量上）。本版把 `SKILL.md` 对齐到 **1.0.14**，此后两者必须一致。

## 1.0.14 — 2026-10-29

- 迁移后恢复演练（克隆 487s / 14,582 文件 / mentoragent **20 会话 1,860 消息**、uid 空 0、quick_check ok、复跑 0/20；main 只读 **2,254 文件 = 2,174 会话 / 393,147 消息**）通过，按演练踩点补 pitfalls：**非 main 档的库在 `home/profiles/<p>/state.db`**，开 `home/state.db` 会报 no such table。

## 1.0.13 — 2026-10-29

- 更正 1.0.12 的归因（见下）并补实测：干净轮 485–496s、小文件写 C: 2,227/s vs D: 1,954/s。

# CHANGELOG — hermes-backup

> 只记「用户会发现的变化」和「踩过的坑怎么修的」。每条后面跟版本号，便于 `git show` 回溯。

## 1.0.12 — 2026-10-29

- **单轮兜底 3900s → 5400s**。镜像迁到 `~/hermes-backup` 后出现过 1605–2697s 的长尾轮，按「长尾 ×2」留余量。
- **⚠️ 归因更正（同日晚）**：长尾**不是**新磁盘慢。实测同目录 3,000 个小文件：C: 写 **2,227 文件/秒**、D: 写 1,954；
  单文件顺序写 C: 104MB/s、D: 343MB/s，但同步负载是小文件为主。真实原因是**同步轮期间跑了重活**——
  10:29 那轮撞上 3.4GB 的 `git reset --hard`，11:10 那轮与手动轮重叠。**干净轮实测 485s / 496s**
  （旧址 282–325s，+75%，与数据量和磁盘都相符）。5400s 保留，但它防的是并发与长尾，不是磁盘。

## 1.0.11 — 2026-10-29 · 目录迁移适配

宿主把工作区与镜像都换了根（原 `D:` 盘布局 → 现在 `~/workspace` 与 `~/hermes-backup`，
Windows 即 `C:\Users\<你>\...`），本版做适配：

- `sync.py` 的镜像默认值改为 **`Path.home() / "hermes-backup"`**（不再写死用户名——写死会把
  `C:/Users/<名>` 漏进公开仓）。解析顺序仍是：`--mirror` > `HERMES_BACKUP_MIRROR` > `local.json` > 默认。
- 修 4 个脚本里被批量替换出来的 **`Path("~/...")`**：Python 不展开 `~`，已全部改成 `Path.home()`。
- 文档里的绝对路径统一改成 **`~/hermes-backup`**（Windows 即 `C:\Users\<你>\hermes-backup`），
  公开仓零 `C:/Users/<名>` 痕迹。
- 新址实测：镜像 reset 到远端后跑一轮 **1605s（26.7 分钟）/ committed + pushed / HEAD == 远端**；
  同口径下旧址（D:，NVMe）是 214–470s。磁盘实测 **C: 写 104MB/s、读 86MB/s；D: 写 343MB/s、读 120MB/s**
  ⇒ 变慢是磁盘档位差异，不是代码回归。另清掉一个 2.3GB 的 `tmp_pack_*`（被杀推送留下的 garbage）。

## 1.0.10 — 2026-10-27

- **推翻自家的「换代」口径**（原写「12GB ⇒ 约 40 天/代，体积回到 1.5GB 级」）。实测：
  树 3.61GB、`.size` 3.67GB、历史对象多 29% 但**压缩体积只多 60MB（1.6%）**——git 把旧版本
  delta 掉了。⇒ **换代不会缩小数据，也不会重置树的增长**（新代仓同样从 3.6GB 起步、同样 220MB/天）。
- 触发条件从「体积 ≥12GB」改成四个**可感知的痛**：常规轮 push >10 分钟 / 克隆 >15 分钟 /
  `.size` >8GB / 历史占比 >20%。并写明：若因树本身大，换代无用，该讨论分仓 / 裁剪 / 接受更慢恢复。
- 风险面写清四条：历史不进新代（跨仓恢复）、30–60 分钟推送窗口、archive+切 origin 无自动回滚、
  本地 `.git` 不变小。

## 1.0.9 — 2026-10-27

- **P0 修复在生产上验证通过**：19:53 那轮批量导出 **17 档 / 782s**，`main` **1,556 会话 / 1,726MB**，
  无 MemoryError（分块渲染生效）；此后 20:46、23:09 两轮正常推送，`sessions/main` 已从恢复后的
  1,470 个文件长回 1,664 个。
- 修一处可诊断性缺陷：私有性查询失败时日志只有「私有性查询失败： ——」（gh 网络失败时 stderr 为空）。
  现在带 **rc + 可见输出**，例如 `私有性查询失败 rc=1：gh: Not Found (HTTP 404)`。
- 顺带记录一次真实演练：20:13 轮因网络抖动查不到私有性 ⇒ **按设计拒推**（exit 4），下一轮自动恢复
  ——fail-closed 没有误伤，也没有静默放行。

## 1.0.8 — 2026-10-26

- 把「消息级锚定分组」的实测结论写进 `references/session-storage.md`：真实数据 29 个变化分片 /
  74,254,024 字节，**8 MiB 组只省 11.3%，末片 0%**（分片上限 12 MiB 与组大小 8 MiB 只差 4 MiB，
  27/29 个分片封不了组）；收益天花板来自「早期消息被 merge/absorb 导致整段位移」。
  记录判定：**不做**（2–3 人日 + 改格式 + 恢复链回归），若将来优化则走「末片 append-only」。
- `export_all_profiles.py` 注释补一条实测：**不能用逐会话 `export_session()` 替代整档导出**
  （`export_all()` 会对 `tool_names` 做哈希后处理，两者 sha256 不同）。

## 1.0.7 — 2026-10-26 · 数据丢失事故（P0）

**事故**：16:34 那轮 `sessions/main` 导出抛 `MemoryError`（整档 1.6GB 在内存里拼成一个字符串），
回退逐档 CLI 又「成功」返回 `sessions=0 files=0` ⇒ `keep` 为空 ⇒ `prune_stale` 把
`sessions/main` 整个目录删掉（**1,470 个文件**），整轮仍报 `exit 0` 并推送。
数据已从 git 历史（`0f360035`）恢复为提交 `c17f7ae5`，未丢。

**三处修复**

1. **零产出保护** `guard_zero_sessions()`：导出 0 个会话、而该档历史上已有文件 ⇒ 判失败、
   旧文件全部保留、本轮不 prune 该档。回归 `scripts/test-zero-session.py`（5 项）。
2. **整轮按失败记**：任一档导出失败 ⇒ 其它档照常提交推送，但本轮 `exit 5`（会进健康计数、
   连续 3 轮触发 needs-attention），不再「删了数据还报成功」。
3. **导出改分块渲染**（`CHUNK=4`）：不再把整档拼成一个 str，峰值内存从「整档 + 整档」降到
   「整档 + 一个 chunk」。实测 main 1,548 会话 / 1.80GB 正常导出，无 MemoryError；
   输出与整档渲染**逐字节相同**（sha256 一致，已实测）。

**教训**：`rc=0` + 0 产出 ≠ 成功；任何「本轮没有产出」都要先问「是不是它本来有东西」。

## 1.0.6 — 2026-10-26

- 文档数字最终对账（自审手册第 1 路），修 5 处与代码不符：`auto-sync.md` 仍写「导出 ≤1200s」
  （实际已改按进展判：连续 1200s 无进展才杀、硬上限 3600s）、`operations.md` 的自检覆盖面与
  两条互相冲突的超时行、`README.md` 缺「公开面卫生」一项、`hb-sentinel.py` 注释里的旧预算公式。
- 实测：`selfcheck` 12 项 / 0 失败。

## 1.0.5 — 2026-10-26

- 新增 `scripts/privacy-scan.py`：**推前三道**（① 凭据文件被跟踪 ② 真实私人信息：身份证/手机号/
  邮箱/自定义禁用词 ③ 本机绝对路径 `C:/Users/<名>`、`/home/<名>`），支持 `--repo`、`--history`
  （扫全历史 diff）、`--terms-file`（真实姓名等只放本机 `local.json`，**绝不写进仓**）。
  已接入 `selfcheck`（第 12 项「公开面卫生」）。
- 口径写入 SKILL/operations：**私有仓日常 push 不请示；公开 / Release / npm publish / 加协作者或
  部署密钥才需授权**；**不改写已推历史**——历史里的旧痕迹只能在后续版本移除并在本文件记一笔。
- 按此口径自查本仓：当前 HEAD 三类零命中；**全历史有 1 处**本机用户目录绝对路径（`b86261c` 引入、
  `78fb418` 已移除，是测试夹具里的旧绝对路径）。用户名本身就是公开作者名，且约定不改写历史，
  故保留并在此披露（此处不再复写那串路径本身，避免又添一处）。
- 实测：`selfcheck` 12 项 / 0 失败 / 60.1s；`privacy-scan` 自扫与公开仓 HEAD 均 `PRIVACY-OK`。

## 1.0.4 — 2026-10-26

- **导出阶段的预算从"固定墙钟"改成"进展看门狗"**：机器被别的活占满时，同一次批量导出能从 210s
  涨到 1200s+（实案：11:20 轮撞 1200s 硬预算被掐 → 回退逐档 CLI → 整轮 48 分钟）；而真卡死时它
  一个字节都不写。现在每 30s 看一次**进程 IO**（不能看输出目录：`export_all_profiles` 是整档在
  内存里渲染完才落盘，main 那种 1.6GB 的档在写文件前目录一直是空的），涨了就续期，连续 1200s
  不涨才杀整树，硬上限 3600s。
- `test-layout` 只挑**能解析成 JSON** 的会话件作样本：镜像里存在被中断留下的空/截断件，选到就
  `JSONDecodeError` 假红（分片 `.part-NNNN` 本来就不是独立 JSON，不该被当样本）。
- 实测：改后一轮 **1027s**（批量导出 17 档 / 836s）正常收尾、pushed。

## 1.0.3 — 2026-10-26

换代种子脚本在真实环境里**连续三次实测失败**，三处都是"只有真跑才会暴露"的：

1. **leveldb 竞态**：按 `ls-files` 名单 `git add -f`，桌面端在枚举与 add 之间删掉一个 `.ldb` →
   `fatal: pathspec … did not match any files`，整批 rc=128（两遍复现）。
2. **`update-index --index-info` 会遵守 `.gitignore`**：会话分片是 `add -f` 进仓的，走 index-info
   直接被 "Ignoring path …" 跳过 → 组装出的树是空树 `4b825dc…`。
3. **HEAD 在跑动中前进**：同步轮并发提交后，结尾「种子文件数 vs HEAD 文件数」必然不等
   （16207 vs 16262），第一遍 rc=1；第二遍因远端树"不认识"拒绝续跑。

修法（三条一起才成立）：**整轮钉住开始时那个 commit**（`BASE`），分批与组装树全部读 `BASE`
（`ls-tree`/`read-tree`，不碰工作树）；每批用 `read-tree BASE` + `update-index --force-remove`
反向删掉本批之外的路径；第 i 棵树 = 前 i 批的并集。

实测：5 批 / 16,262 文件 / `SEED-OK 1e41ec54`；**第二遍 7 秒 no-op**（"远端已是完整树，无需推送"），
末树与 `BASE^{tree}` 逐字节一致。

## 1.0.2 — 2026-10-26

- **被杀的一轮在账本里没有任何记录**：包装只在超时/异常时补记，子进程非零退出直接走"打印失败"分支。
  现在非零退出也补一行 `runs.jsonl`（`exit_code="unfinished"`、`via="wrapper"`），"每轮一行"才成立。
- **批量导出失败只剩 300 字尾巴**，看不出是哪档、为什么（实案：`main` 缺件、原因被截掉）。
  现在全量输出落 `state/hb-bulk-export.log`。
- **导出预算 2000s → 1200s**（实测 210–600s，留 2x 余量）：真卡住时最多浪费 20 分钟，不是 33 分钟。
- `rotate-generation.sh` 两处：锁判活改用 psutil（MSYS 的 `kill -0` 看不到原生 Windows 进程，
  会把活锁当死锁）；工作树有未提交改动时不再直接退出（桌面端 leveldb 持续写入，永远等不到"干净"），
  改为真跑时先提交收口并尝试推送，dry-run 只报告。
- `selfcheck.py` 在检测到有同步在跑时明确告警（自检会写上百 MB、读全部会话库，会把那一轮拖到超时；
  实案：并发跑导致导出子进程卡在启动阶段 28 分钟）。

## 1.0.1 — 2026-10-26

- **孤儿导出回收的判据里硬编码了目录名** `hermes-backup`：换目录名安装/克隆时永远回收不到，
  孤儿会和下一轮抢同一批文件（连续两轮 3300s 超时的原始成因之一）。改为只认「参数里有
  `export_all_profiles.py` + 带 `--home/--out` + 父进程已死」——这正是**干净克隆实测**抓到的。
- `test-reap-exporters` 加并发保护：常驻哨兵/其它测试可能抢在观察前把孤儿收掉（偶发 `find=[]`），
  改为最多重试三次；克隆与本机各连跑 3 次全绿。

## 1.0.0 — 2026-10-26 · 首个功能完整版

**验收**：9 套回归全绿 · `selfcheck.py` 一条命令全绿（含全新克隆场景）· 恢复链演练差异 0 · 换代预演 + 种子双跑 no-op · 真同步轮 exit 0 且远端 == 本地。

**功能面**：同步（采集/导出/提交/推送）· 恢复（文件层/会话层/路由层/桌面收尾）· 换代（分批种子）· 自动同步（cron + 哨兵）· 审计（覆盖率/分片/演练）· 一条命令自检。

## 0.4.x — 2026-10-24 → 10-26 · 稳定性战役

### 数据正确性
- 看板库是 WAL 模式，直接拷 `.db` 会丢未 checkpoint 的已提交事务（极端情况连建表都没有）→ 改 SQLite 在线备份 API 快照（0.4.28）。
- kanban 备份越界：`rglob` 把 73 个任务工作区 db 也备进云 → 改白名单 `boards/<board>/kanban.db`（0.4.29）。
- 上述改造漏登记 `_COPIED`，导致 11 个板库被 prune 静默删除 → 补登记 + S5 回归（0.4.30）。
- **整档误删**：某档导出失败/零产出时 `prune_stale` 会把整档会话历史清空再推 → 改为「整档删除需 `allow_delete` 显式授权」（0.4.49）。
- **快照回退互递归**：`snapshot_sqlite` ↔ `copy_file` 互相调用，坏库静默不写 → 回退改 `copy2 + os.replace`（0.4.49）。
- **路由表恢复在真实数据上必失败**：同一 `session_key` 跨 scope 重复（50 行/40 key），按新 scope INSERT 撞 UNIQUE → 生成端去重 + 恢复端 UPSERT + `--dry-run`（0.4.47）。

### 容量与分片
- 会话头部统计字段每轮变 → 全部分片重写（917–967MB/轮）→ 尾部稳定化（反向白名单）（0.4.18–0.22）。
- 分片阈值 90MB → 45MB → **12MB**（0.4.26）。
- 试过「末片压到 3MB」，实测反而更差（156MB/轮）→ **已回退**，边界必须固定偏移（0.4.31/0.4.33）。
- 每轮重写量 917MB → **99.9MB**；稳态 130–155MB。
- 容量判据纠正：`churn-report` 量的是「新增 blob 字节」，不是仓体积增长（0.4.36）。

### 换代
- 阈值 4.5GB → **12GB**（实测增长 9.2MB/h ⇒ 约 40 天/代）（0.4.36）。
- 整树单次 push 必撞 2GB 上限 → `seed_generation.sh` 分批推送（0.4.33）。
- 种子脚本四处坑：`read-tree` 清空镜像 index / heredoc 顶掉 stdin / 临时 index 必须 `-f` / `ls-remote <路径> HEAD` 恒空（0.4.33–0.35）。
- 种子改为**两遍按树续跑**：完整树 26s no-op、部分树续推、异形远端拒绝覆盖（0.4.66）。

### 恢复
- `restore_files` 无预演 → 加 `--dry-run` / `--create` / home 合法性检查（0.4.45）。
- 恢复时「0 个会话」算成功 → rc=2 + 提示，`--allow-empty` 可放行（0.4.56）。
- 分片缺片被报成「读取失败」→ 先校验编号，直接说缺哪片（0.4.64）。
- fast-import 摘掉的 display/FTS 触发器不在 `finally` 还原 → 中途抛错会让索引永久缺失（0.4.64）。
- 部分克隆 + 稀疏检出路径写进 RESTORE（只要某档会话时 1.9GB vs 全量 3.3GB）（0.4.33）。

### 运维与自动化
- 导出超时只杀父进程 → 孤儿撞死下一轮（0.4.15/16）。
- 哨兵看「锁文件存在」而非「持有者活着」→ 死锁永久挡住补跑；改为读 pid（0.4.58）。
- 首轮失败后永不重试 → 新增 `overdue-no-state`（0.4.58）。
- 镜像 `git gc --auto` 在轮内全量 repack，把一轮拖到 1,465s → `gc.auto=0`，git 段 1,090s → **103s**（0.4.37–0.39）。
- 私有性检查原为 fail-open（验证不了照推）→ 改 **fail-closed**，`--allow-unverified-privacy` 才放行（0.4.52）。

### 工程与文档
- 一次性补上 `check-parts.py` / `verify_drill.py` / `restore-drill.md` / `audit-playbook.md`（文档引用但仓里没有）（0.4.50/0.4.70）。
- `SKILL.md` 55KB → 26KB：Pitfalls 拆到 `references/pitfalls.md`（62 条全文保留）（0.4.60）。
- 文档数字与代码对齐 20+ 处（分片/换代预算/场景数/套数/倍速/体积）（0.4.65/0.4.70）。
- 入口脚本 `--help` 不再触发真动作（0.4.50/0.4.51）。
- 测试自足化：不依赖 `~/hermes-backup`、不依赖目录名、不靠固定 sleep（0.4.64/0.4.70）。

## 0.3.x — 2026-10-2x · 把备份做成可交付的技能

- 分层 v3：纯云、无本地兜底、单一真相源；`pull → collect → commit → push`，永不 force。
- 存储格式 v0.3：会话原样文本（`<sid>.jsonl`），兼容 `.jsonl` / `.part-NNNN.jsonl` / `.jsonl.gz` 三形态。
- 单实例防撞锁（记 pid，持有者消失立即接管）；推送走 `ssh.github.com:443` + 三次退避。
- 单进程批量导出（官方导出函数直调，实测 ~8x 且与 CLI 逐字节一致）。
- 断电式恢复：文件层 → 会话层（fast-import）→ 路由层 → 桌面收尾，每步可核验。
