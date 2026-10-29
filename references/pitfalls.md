# Pitfalls — 实战坑清单（hermes-cloud-backup）

> 由 `SKILL.md` 的 Pitfalls 段拆出。每条都是实机踩过并修复过的，带版本号与判据；排查时按主题找，别整篇通读。
> **条数随修随增——本文件不写死总数（也不要在 SKILL/README 里写死），只维护下面的分类目录。**

## 目录

- [会话与分片](#会话与分片)
- [导出与采集](#导出与采集)
- [git 与推送](#git-与推送)
- [恢复与迁移](#恢复与迁移)
- [哨兵与自愈](#哨兵与自愈)
- [运行环境与并发（Windows / 子代理 / 锁）](#运行环境与并发2026-10-26-增补)
- [其它](#其它)

## 会话与分片

- **恢复时「0 个会话」不再算成功**（0.4.56，2026-10-26 实测踩到）：`--source` 指错目录时原来打印一行「源目录里没有会话文件」
  然后 `imported=0` 以 **rc=0** 退出，等于什么都没恢复却报告成功。现在空档目录 → **rc=2** + 明确提示，确属预期加 `--allow-empty`；
  源根目录不存在仍 rc=1。另实测：坏 JSON 分片不会崩，计入 `failed` 并让整轮 rc=3（好档照常导入）。

- **末片上限方案已被实测否决（0.4.31 回退）**：把末片压到 3MB 看似能减少尾部重写，实测反而更差——
  切片边界随总长浮动，每次追加要重算「倒二片+末片」，churn 从 137MB 升到 **156MB**。分片边界必须由**固定偏移**决定。
  教训：改动分片/稳定化后，先看真实一轮的 `churn-report.py` 与 `gh api .size`，别只看单文件大小。

- **代码回退 ≠ 数据归位**：切片/存储代码改动后，镜像里的旧文件**不会自动变形**——写入侧对「内容相同」的文件直接跳过，所以旧形态会一直留着（实测：代码已改回固定 12MB 边界，11 个会话仍是末片形态，直到跑过一轮真同步才归位）。判据是**外部可观测的形态指纹**，不是读代码：
  `python <SKILL>/scripts/check-parts.py --repo <镜像>` —— 断言片号连续、除末片外每片**正好** PART_MAX、末片 ≤ PART_MAX、无「单体＋分片」混用；返回 `PARTS-FAIL` 就说明镜像还是别的切法，跑一轮同步再验。
  同理适用于任何格式切换：改完代码必须**落地一轮**并用产物形状（片大小、文件数、DB 能否打开）验收，别拿「代码看起来对」当结论。

- **对账三个数别混用（审计的「活库消息数」不含孤立组）**：`audit-coverage.py` 打印的 `活库 X 会话/Y 消息` 里 Y 只数「sessions 行存在」的消息；`孤立 N 组/M 条`（messages 有行、sessions 无会话行）是**单列的**。裸 `SELECT COUNT(*) FROM messages` 会比 Y 多出 M（实案 proj-83：24,930 vs 25,748，差 818 ＝ 孤立 4 组 818 条）。恢复对账、子代理汇报里拿裸 COUNT(*) 去比镜像文件，会把孤立组误读成「备份落后/丢数据」——**先分解成「会话内消息＋孤立组消息」两个数再比**；镜像侧消息数一律按文件解析（分片按片名拼接），不信目录里的文件数。

- **GitHub 单文件 100MiB 硬限**：会话导出件按会话拆分；v0.3 起原样文本存储、**>12MB 的巨会话按 12MB 切片**（`<sid>.part-NNNN.jsonl`；0.4.10 由 90→45MB，0.4.26 再降到 12MB——尾部稳定化后每轮只有「变动的那个分片」重写，阈值直接决定单轮重写量：45MB 时稳态仍 249MB/轮、其中 208MB 是 10 个活动会话的末片；12MB 把末片压到 1/4，距 100MB 硬限仍有 8 倍余量）。**改阈值/改格式后必须做一次「多分片真恢复」**：挑一个切成 >10 片的会话，按片拷到 `<src>/<profile>/` 走 `restore_sessions.py --source … --fast-import --verify`（实测 243MB 会话切成 21 片、恢复 73,546 条、`verify_failed=0`）——单跑 `test-split` 只证明字节/语义等价，证明不了导入管线能吃下多片。字节等价由 `scripts/test-split.py` 回归（切片+按片号拼接 == 原始行，逐字节）。

- **头部字段一变，整份分片全成新 blob**（0.4.18，2026-10-24 实测）：会话是「一行=一个会话」的 JSON，头部有 `message_count` / token 计数 / `timings` / `last_activity_at` / `transcript_revision` 等**每轮都会变**的统计字段；头部一变，整行字节全变 ⇒ 固定字节偏移切出的**每一个分片都重写**（实名单轮 827MB，10 个巨会话各 3–6 个 45MB 分片全改；仓体积约 150MB/小时增长）。修法＝`scripts/session_layout.py` 的**尾部稳定化**：零解析、字符串感知地**只保留身份字段（`KEEP_KEYS`：id/source/user_id/session_key/chat_id/…）与 `messages` 在前，其余顶层成员全部移到对象末尾**（不改任何内容的字节），于是未变会话完全一致、有追加的只改末尾分片。**别写「正向列举易变字段」**——第一版正是那么写的，漏了 `last_activity_description`、`runtime_revision` 等同样会变的字段，整行照样被推翻、白改一版；反向白名单才成立。只对 >5MB 的会话行生效（小文件整份重写无所谓），解析失败一律原样保留。回归＝`scripts/test-layout.py`（6 项：等价/幂等/字段落尾/追加前缀稳定/40MB 性能/前半段严格＝KEEP）；**整轮收益的验收口径**＝`scripts/churn-report.py` 量的是**格式切换后的第二个轮次**（切换那一轮含一次性重排，见下一条），且改动字节应显著下降；不能只凭单元测试就说「体积问题解决了」。

- **体积改动的验收必须跨「两个稳定化轮次」，不能拿格式切换那一轮当判据**：改存储格式/切片策略后的**第一轮**会把受影响文件整体重写一遍（一次性成本），单看那一轮的重写量会把「重排」误读成「没修好」——实案 827MB → 967MB 看着更糟，其实 967 里混着全部大会话的首次重排。做法：记下基线 → 等**下一轮**（两个轮次都是新格式）→ 用 `scripts/churn-report.py` 量 → 只有「未变会话不再出现、巨会话只剩末片」才算成立；连续两轮都仍有整份巨会话重写，说明变的是**内容**（压缩改写历史等），要换更深的方案（一条消息一行 / 按内容寻址），继续调参数没用。

- **历史体积机制（关键）**：git 仓库＝最新快照＋**每个被改文件的每一版**——同步的「强制覆盖」语义只作用于最新树（≈镜像工作区），**挡不住历史堆积**（实测：全历史 blob 2.01GB vs 最新树 0.72GB；单会话 10 个版本＝313MB）。根因＝会话导出是**超长单行 JSON**、头部元数据每轮必变 × 整包 gzip → deflate 级联 → 版本间字节重叠 **0%** → git 增量全失效 → 每轮 ~226MB 完整入库（仓库 ~0.5GB/天）。**已治理（v0.3，0.3.0）＝原样文本格式：同数据对照实测 +79.40MB/轮 → +0.01MB（整件）/ +2.25MB（分片）级；机理与三方案对照见 `references/session-storage.md`。** 单文件 95MB 同步闸由分片格式顺带拆除（分片后最大件 12MB）；体积读数用**本地对象库**实测（GitHub `size` 字段有滞后）。

- **导出不脱敏**：会话文件含对话原文与凭据讨论 → 仓必须永远私有。

- **跨档只读边界（备份/审计/演练通用）**：审计与演练必然读到**别的 profile** 的库与会话文件——**读可以，写不行**。发现「镜像有、活库没有」的会话（外部回滚/手工清理造成）时：把文件复制到 `temp/hcb-lost-sessions-<date>/src/<profile>/`、写一份含导入命令的 README、然后**只报告不导回**——那是别的会话的数据，导入/删除都要 owner 明确点头。同理：恢复演练一律落**空白 `--home`**（先用 `mkdir -p <B>/home/profiles` 建好目录；`--source` 收的是含 `<profile>/` 的父目录），跑完 `rm -rf`，绝不指向真实 home。

- **改存储格式前先量「浪费占比」**（2026-10-26 实测）：外部调研建议做「header 独立 + 8MiB 消息组 + manifest」压 churn，
  用真实一轮量（29 个变化分片、新版本合计 74,254,024 字节）后：8MiB 组只省 **11.3%**、只看末片 **0%**
  （分片上限 12MB 与组大小 8MiB 只差 4MB，27/29 个分片根本封不了组）；理论上限（header 独立 + payload 对齐）55.3%，
  组降到 1MiB 也只 45.2%——而早期消息被 merge/absorb 会让整段位移（某分片稳定前缀仅 10.5%、另一个 0%），任何分组都省不到。
  **结论：不做**（2–3 人日 + 改格式 + 恢复链回归不划算）。方法可供以后复用：对同一段 `git diff` 的每个变化分片算
  `LCP + LCS`（不重叠）÷ 新版本字节，按「末片/非末片」分组看。判据：**省不到 40% 就不动存储格式。**

## 导出与采集

- **推送预算 900s→1500s、外层 3300s→3900s**（0.4.23）：一次性大重排那轮（900MB 级）推送超 900s 被掐，属合法大包不是故障；外层同步放宽到 3900s（collect+export+commit 最坏 ~1900s）。日常轮次仍只有几十 MB，读数不会变。

- **自定义拷贝路径必须登记 `_COPIED`，否则会被 prune 当残留删掉**（0.4.30，2026-10-25 实案）：`copy_kanban_dbs` 直接调 `snapshot_sqlite()`（绕过了 `copy_file` 的登记），结果 `prune_unvisited` 把 11 个板库当"本轮未访问"全删了——镜像是空的、且没有任何报错。凡是不走 `copy_file()` 的写镜像路径，都必须 `_COPIED.add(str(目标路径))`。回归＝`test-kanban-scope.py` 的 S5（copy 后跑 prune，断言文件仍在）。

- **镜像仓要关掉自动 repack**（0.4.37）：`git gc --auto` 会在同步轮内触发全量 repack，实测把一轮从 ~300s 拖到 **1,465s**
  （git 阶段 1,090s）。镜像的 `.git/config` 已设 `gc.auto=0`；需要时**手动** `git -C <镜像> gc --quiet`（判据：`git count-objects -v` 的 `count` > 20,000），
  且只在没有同步轮在跑的时候做。**实测关闭后 git 段 1,090s → 103s**（同机器、同一轮导出量）。

- **两份 sync.py 不要分叉**（0.4.55）：cron 的 `hb-sentinel.py` / `hb-autosync.py` 放在 `HERMES_HOME/scripts/`（cron 只能放这里），
  但它们**指向技能目录里的 `sync.py` 正本**。所以：
  · 技能目录 `scripts/` 要齐 `sync.py`、`session_layout.py`、`run_in_runtime.py`、`export_all_profiles.py`、`restore_sessions.py`（缺 `export_all_profiles.py`
    会让批量导出静默退化，比正常慢 3–5 倍；现已打印 `!! 批量导出不可用（缺 …）`）；
  · `HERMES_HOME/scripts/` 只需 `hb-sentinel.py` + `hb-autosync.py`；若那里也放了一份 `sync.py`，**它必须是转调壳**而不是副本
    （2026-10-26 实案：副本缺 helper，手动轮走慢路径而 cron 正常，很难发现）。
  · 自查：`python "$HERMES_HOME/scripts/sync.py" --help` 能出 usage；`ls "$HERMES_HOME/skills/hermes-cloud-backup/scripts/export_all_profiles.py"` 存在。

- **整档删除必须显式授权**（0.4.49，2026-10-26 代码审查发现）：`prune_stale` 原实现「keep 里没有的档就 rmtree」——
  某档导出失败/零产出时会把它**整档历史清掉**再 commit+push。现在签名是 `prune_stale(root, keep, allow_delete=…)`，
  只有「已确认活库里不存在的档」才允许整档删除，其余只清目录内多余文件；导出无产出时该档本轮受保护。回归＝`test-prune-protect.py`（S1–S3）。

- **「导出成功但 0 个会话」是第二道闸，`allow_delete` 盖不住它**（1.0.7，2026-10-26 实案）：某轮批量导出对 main 抛
  `MemoryError`，逐档 CLI 回退 `rc=0` 却 `sessions=0 files=0` ⇒ `split_to_session_parts` 得 0 个文件 ⇒ `keep` 为空 ⇒
  `prune_stale` 把 `sessions/main` **1,470 个文件整目录删掉**，且整轮仍报 `exit 0` 并推送。现在两处闸：
  ①`guard_zero_sessions()`——0 会话、而该档历史上已有文件 ⇒ 判失败、旧文件全部进 `keep`、本轮不 prune；
  ②任一档失败 ⇒ 其它档照常提交推送，但本轮记 **`exit 5`**（进健康计数，连续 3 轮触发 needs-attention）。
  回归＝`test-zero-session.py`（5 项，含「空档不误判」与「有产出不干预」）。**铁律：`rc=0` + 0 产出 ≠ 成功，先问「它本来有没有东西」。**

- **快照回退不能回到 copy_file**（0.4.49）：`copy_file` 对 `.db` 分支进 `snapshot_sqlite`，而快照失败又回退 `copy_file` ⇒ 互递归
  （实测喂坏库递归到深层后返回 False、目标文件根本没写）。回退改为 `shutil.copy2 + os.replace`。回归＝`test-prune-protect.py` S4（0.03s、文件落地）。

- **路由表按 session_key 去重**（0.4.47）：`gateway_routing` 里同一 key 可能在多个 scope（历史 HERMES_HOME 路径）下各有一行，
  原样导出会让恢复端按「目标机 scope」逐行 INSERT 撞 UNIQUE —— 实测本机 50 行 / 40 key，`apply_routing.py` **直接失败**（事务回滚、零写入）。
  现在 `dump_routing()` 去重（优先本机 scope、其次最新 `updated_at`，report 里 `routing_rows_raw` 记原始行数），
  `apply_routing.py` 再加 `ON CONFLICT(scope, session_key) DO UPDATE` 兜底 + `--dry-run`（实测 new=40、幂等复跑 unchanged=40）。

- **kanban 备份必须严守白名单**（0.4.29）：只备 `boards/<board>/kanban.db`（含 `boards/_archived/<board>/kanban.db`）与顶层 `kanban.db`。旧实现用 `rglob("*.db")`，把任务工作区/证据里的 73 个 `school80.db` 之类也备上了（镜像 84 个 db / 36MB），违背「工作区属过程记录」的政策、每轮还多快照 70+ 个无关库。回归＝`test-kanban-scope.py`（4 项）。**改动后下一轮会自动 prune 掉多备的工作区库。**

- **SQLite 必须用在线备份 API 快照，不能直接拷**（0.4.28）：看板库是 `journal_mode=wal`，而 `-wal`/`-shm` 被排除在备份外 ⇒ 只拷 `.db` 会丢掉尚未 checkpoint 的已提交事务；实测极端情况下**连建表语句都可能不在 `.db` 里**（普通拷贝打开即 `no such table`）。现在 `copy_file()` 对所有 `.db` 走 `snapshot_sqlite()`（`sqlite3.Connection.backup()`）：读穿 WAL、产出自包含紧凑快照，失败自动回退普通拷贝。覆盖 `kanban/*.db`、`shared-state.db`、`projects.db` 以及各档 `projects.db`。回归＝`test-kanban-snapshot.py`（3 项）。

- **导出超时只杀直接子进程 → 孤儿导出会撞死下一轮**（0.4.15，2026-10-24 实案）：连续两轮 3300s 超时，原因是 `subprocess` 超时只杀直接子进程（`run_in_runtime` 启动器），真正的导出进程活下来占住 `profiles/<档>/state.db` 与镜像；下一轮再起一个，两个导出同时写同一批文件 → 双双卡死、RSS 涨到 1.6GB。现在三处收口：①导出改 `Popen + communicate(timeout=2000)`，超时 `_kill_tree()` 杀整树；②每轮启动后 `_reap_orphan_exporters()` 只回收「父进程已消失」的导出（识别条件：python 解释器 ＋ **真实参数**里以 `export_all_profiles.py` 结尾 ＋ 带 `--home`/`--out`；只看 argv 不看整条命令行文本，否则自己的 `-c` 探针只要代码里提到这些字眼就会自匹配）；③包装器 `hb-autosync` 超时也杀整树。回归＝`scripts/test-reap-exporters.py`（3 场景）。**判据：** 超时后先看 `ps` 里有没有 `export_all_profiles` 进程，再看下一轮是否同时存在两个导出。
  **超时判据后来改成「按进展」**（1.0.4）：固定墙钟会把「慢但在干活」的导出当卡死掐掉（实测 1200s 硬预算把一轮从 1200s 拖成 48 分钟——回退逐档 CLI 更慢）。
  现在每 30s 看一次**进程 IO**（`psutil` 取该进程树 read+write 字节增量），涨就重置 deadline，连续 1200s 不涨才 `_kill_tree`，硬上限 3600s。
  **不能按输出目录判进展**：`export_all_profiles` 是整档在内存里渲染完才落盘，main 那种 1.6GB 的档在写文件前目录一直是空的。

- **整档渲染会 MemoryError，必须分块**（1.0.7）：`render_sessions_export(全部会话)` 把整档拼成一个 str（main 约 1.6GB），机器一紧张就抛
  `MemoryError`，而导出脚本是「逐档 try/except + 继续」，于是一个档静默缺席。改法＝按**同一份 `sessions` 列表**切块（`CHUNK=4`）逐块渲染并写入，
  峰值只剩一个 chunk；实测输出与整档渲染 **sha256 逐字节相同**，main 1,548 会话/1.80GB 可正常导出。
  **不要**改成「按 id 逐个 `export_session()` 再拼接」——`export_all()` 会对 `tool_names` 等字段做后处理（存哈希），
  `export_session()` 存原始 JSON，两者 sha256 不同（实测首个差异就在 `tool_names`）。

- **超大会话导出守卫**：`sessions export` 默认对 >20000 条消息的会话整包报错（且 rc=0 无产物）。官方解法 = `hermes config set sessions.max_export_messages 0`（新机上首次同步前设置一次即可）。

- **量重写量用自带工具**：`python tools/churn-report.py`（默认镜像 HEAD~1..HEAD）按目录汇总新增 blob 字节，并列出 >5MB 文件；判据——同一个会话的多个 `.part-` 同时出现在 >5MB 列表里，说明它仍被整份重写。注意 git `--raw` 对带空格的路径会加引号，解析必须用 `-z`（本工具已处理；2026-10-24 实案：`desktop-overlay/Local Storage` 曾被按空格切列误记成 `Storage/leveldb`）。

- **意外中断与并发自愈**：自带单实例防撞锁——**v3.2 起锁内记录持有者 pid：持有进程已消失（触发端被掐/崩溃）立即接管，3h 陈旧窗仅兜底**（早期版本纯 3h 窗口＝被掐后可能空窗最长 3 小时）；被强杀遗留的 `.git/index.lock`（>120s）自动清除并重试一次。真机实测：进程在 `git add` 瞬间被杀 → 下一轮同步报 `rc=128` —— 自愈代码即为覆盖该情形；被掐轮次的**未推提交由下一轮随带补推（零丢失）**。- **网关重启与在跑轮次的三种组合**（先取证据、都别慌）：① drain 杀掉父脚本、`sync.py` 作为**脱离进程继续跑完**——`hermes cron runs` 会停在 `running` 或落 `unknown`，**判轮次死活只认锁文件里的 pid 存活**（存活＝还在跑、会自行落地；已死＝孤儿锁由下一轮立即接管）；② 在途提交被掐——判「有没有丢数据」**不看 run 状态**，用 `git merge-base --is-ancestor <该轮提交> <远端HEAD>`：命中＝已进远端历史、零丢失；未命中才去查采集/推送段；③ 父脚本被杀时外层超时兜底一并消失 ⇒ 该轮不受 `script_timeout` 约束，必要时人工盯或回收。另：两轮相撞时后来者**立即让路**（日志 `已有另一轮同步在进行（锁年龄 Ns）——本轮退出，让路`）——设计行为，不是失败。

- **推送断连（exit 3）≠ 没推上去**：`send-pack: unexpected disconnect while reading sideband packet` / `Connection to ssh.github.com closed by remote host` 是传输层断线——GitHub 可能已收完整包并更新 ref，只是客户端没拿到确认。**判据只认远端口**：`git ls-remote origin main`（或 `… HEAD`）与 `git rev-parse HEAD` 比对；**别拿本地 `origin/main` 跟踪引用当远端**（失败的 push 不更新它，两种误读都会发生）。一致 ⇒ 零丢失、失败仅是确认缺失：不重推、不 force、不改机制，`hb-sync-health.json` 记 1 次失败、下轮照常；不一致 ⇒ 提交仍在本地、下一轮自动补推。**判「这轮为什么慢」先看在跑 worker 数**：十几张看板 worker 并行时导出段会涨 2–3 倍（实测 485–627s → 1481s），整轮逼近外层预算但仍在预算内，别当卡死。

- **慢轮诊断（阶段预算＋廉价探针）**：正常档一轮 5–8 分钟（收集 1–2 分钟 / 批量导出 ~2 分钟 / 拆分＋`git add` 秒级 / 提交与推送 1–3 分钟）；明显超出即按段找放大器。**`logs/last-run.log` 在轮次结束时整份落盘，中途没有文件可读** ⇒ 要么等落地，要么用廉价探针：锁文件 pid 存活（用 `psutil.Process(pid)` 判定——`ps -W` 只显示可执行名、不含命令行参数，`ps -W | grep 脚本名` 永远匹配不到 python 子进程）、`.git/index.lock` 存在＝正在 add/commit、`git count-objects -v` 的 loose 数、包文件 mtime（repack 痕迹）、进程 IO/CPU（psutil）。**轮次未结束时不要对镜像跑全树重探针**（`du -sm`、`git status`、递归 ls）：持索引锁时 `git status` 会挂住、大目录 `du` 会跑满超时。日志内建阶段行：`git 提交完成：Ns（含 add 与自动 gc）`、`git 推送完成：Ns`。**包装脚本必须把子进程输出流式落盘**（`stdout=<文件句柄>`，不要 `capture_output`）：超时被掐的那一刻 `capture_output` 会连子进程 stdout 一起丢，超时轮就变成零证据；落盘后从日志尾直接指认卡在哪个阶段，成功仍以「日志尾含完成标记」判定（保持静默）。**归因先做对照实验**：干净树上跑同一条 git 命令（`git add -A` 秒级返回＝git 本身不慢，慢在索引被打脏后的整树重哈希），别急着赖 gc 或网络。三类已治理的放大器：每档一次 CLI 启动的固定开销 → 单进程批量导出；**重建式采集**（清空目录再重拷——copy2 保得住 mtime，但文件重建让 ctime 全变，git 判索引失效、整树重哈希 ~3GB，实测提交段 8–10 分钟）→ 写前比对「内容相同不重写」＋ prune 收敛（覆盖层与 sessions 同一把刀）；`git gc --auto` 全量 repack（松散对象过阈值即整包重写、可耗十分钟）→ **它会在轮内同步执行**，实测把一轮从 ~300s 拖到 1,465s（git 段 1,090s）⇒ 镜像仓设 `gc.auto=0`，改为按需手动 `git -C <镜像> gc --quiet`（判据：`git count-objects -v` 的 `count` > 20,000），且只在没有同步在跑的时候做。

- **备份范围（0.3.8 起）**：`kanban/` 只备数据库（`boards/*/kanban.db`，约 30MB）；任务运行工作区/证据/日志属过程记录**不入云**（实测占该目录 96%：约 730MB/4.3 万文件，含整份 git 克隆）——恢复后看板卡片与 runs 在、任务临时工作区不在（预期，别当缺件报）。**范围归 owner**：审计发现白名单外的数据（凭据、快照等）只**报告**——列出路径、体积、可否重取，由 owner 一句话定是否纳入；不擅自扩大或缩小备份范围（那是政策，不是技术优化）。

- **半壳档**（如 `profiles/.deleted`、只余 `state.db` 的已退役档）：导出会失败/跳过，属预期；采集时已排除 `.deleted`。

- **改在跑 cron 的包装脚本＝先实跑一次再放手**：实案 10-23 00:55 `hb-autosync.py` 因缺 `import time` 一秒即失败（`NameError: name 'time' is not defined`，且 except 分支传了未绑定的 `started` 又抛 `UnboundLocalError` 把真因盖住）——此前改脚本**没实跑**（现成回归 `scripts/test-hb-health.py` 也没跑），错误一直留到下一个 2 小时触发点才暴露。规矩：改 `hb-sentinel.py` / `hb-autosync.py` 后，先在终端 `python <HERMES_HOME>/scripts/<脚本>.py` 实跑一次（或跑 `test-hb-health.py`）看 rc，再用 `hermes cron run` 核；`bump_unfinished` 一类兜底分支里的变量必须在 `try` 之前绑定。**另一面：双脚本互备确实救场**——本轮 00:55 周期件秒失败，哨兵（2 分钟）在 44 秒后自动补跑并成功（runs.jsonl 00:56:33 一轮 committed+pushed），所以周期件坏掉不必然造成备份缺口，判「有没有丢」看 runs.jsonl 而非 cron 状态；两枚脚本别合并成一只。**同一个道理适用于改 `sync.py` 本身：八套单元测试全绿也不等于端到端安全**——它们各自造假目录，走不到「真实采集 → 真实 prune」的交互（实案：`_COPIED` 漏登记让 11 个板库被静默删光，而 8 套测试全过）。改过采集 / 写入 / prune 任一路径后：跑一次真入口（`sync.py --collect-only` 或一轮短同步）＋ 当场数产物（镜像板库应为 11 个、工作区 db 应为 0），少数一次就少一个生产事故。

- **`runs.jsonl` 只记「进程内走到的出口」，被外部硬杀的那一轮没有行**（2026-10-24 实测）：`hb-autosync.py` 在 01:45:28 起跑、01:48:27 被网关 shutdown drain（`cron_drain_timeout=30s`）掐掉，`runs.jsonl` **没有这一行**（末行仍是 21:44 成功轮），`hb-sync-child.log` 的 mtime 停在 01:45:39。⇒ 「有没有丢」不能单看 runs.jsonl 的行数，必须三件交叉：`executions.db`（该轮 `unknown`）+ `hb-sync-child.log` mtime + 远端 HEAD 比对。上一轮遗留的未推提交照旧由下一轮补推，零丢失。

## git 与推送

- **`sync.py` 的 home 只按 `LOCALAPPDATA` 解析（不读 `HERMES_HOME` 环境变量）**：`HERMES_HOME` 只影响两个包装脚本（哨兵/周期）与 hook 的路径。写单测时必须**注入 `sync.HERMES_HOME / HEALTH_FILE / ATTENTION_MARKER / RUNS_LOG`** 模块属性，否则会写进真实 `state/`（2026-10-23 实测踩过：把假失败记进了真健康文件）。

- **推送卡在"零字节"时先看进程树，别只看网络**（0.4.9）：`subprocess` 超时只杀直接子进程，`git pack-objects` / `ssh` 这些孙子会留下来占住管道 → **之后每次重试都秒卡、零字节**（2026-10-23 实案：一轮推 10 分钟零字节、三次重试全卡，GitHub 侧只报 `Connection … closed by remote host`）。现在的 `sync.py`：①所有 git 网络操作注入 `GIT_SSH_COMMAND` 保活（ServerAliveInterval=20 / CountMax=30 / TCPKeepAlive）；②超时杀**整棵进程树**；③失败后回收本仓的**孤儿** git/ssh（只认命令行带本镜像路径或 `ssh.github.com … git-receive-pack`、且父进程已消失的）。判据三件套：`git push` 子进程存在 + `pack-objects` CPU 不涨 + ssh 读写 0 字节 ⇒ 管道死了，不是网速慢。

- **换代必须分批推送（2GB 上限）**（0.4.33）：`scripts/seed_generation.sh` 用临时 index 逐批 `add -f → write-tree → commit-tree → push`（每批 ≤800MB），最后校验文件数与树；`rotate-generation.sh` 已改为调它。
  三个必踩坑：① `read-tree --empty` 不带 `GIT_INDEX_FILE` 会**清空镜像真实 index**；② `git ls-files -z | python - <<'PY'` 里 heredoc 会顶掉管道 stdin（分批恒为 0）；③ 临时 index 是空的，所有文件都"未跟踪"，仓库 `.gitignore` 会让 `add` 失败 ⇒ 必须 `-f`。
  **1.0.3 又踩三个（全在真实环境才暴露）**：
  ④ **不能按 `ls-files` 名单从工作树 `git add`**：桌面端 leveldb 一直在增删，枚举与 add 之间少一个文件就 `fatal: pathspec … did not match any files`、整批 rc=128。
  ⑤ **`update-index --index-info` 会遵守 `.gitignore`**：会话分片是 `add -f` 进仓的，走 index-info 直接被 "Ignoring path …" 跳过 ⇒ 组装出的树是**空树** `4b825dc…`。
  ⑥ **跑动中 HEAD 会前进**：同步轮并发提交后，结尾「种子文件数 vs HEAD 文件数」必然不等（16207 vs 16262）。
  最终形态：整轮**钉住开始时的 `BASE=$(git rev-parse HEAD)`**（分批、组树、计数全读 `BASE`，不碰工作树）；每批 `read-tree "$BASE"` +
  `update-index --force-remove -z --stdin` 反向删掉本批之外的路径；第 i 棵树＝前 i 批的**并集**（累积），末树必须等于 `BASE^{tree}`。
  实测：5 批 / 16,262 文件 / `SEED-OK`，第二遍 7 秒 no-op（"远端已是完整树"）。

- **私有性检查默认 fail-closed**（0.4.52，2026-10-26）：原实现遇到「属主解析不到 / gh 不可用 / 查询失败」一律**跳过检查照推**，
  而实测有 3 轮就是这样推上去的（日志 `!! 未解析到仓库属主，跳过私有检查`）。这个仓装着凭据与私人对话，检查没做成就不该推——
  现在改为**拒绝推送**，确需在无法验证的环境里推要显式加 `--allow-unverified-privacy`。
  **失败日志必须带 rc 与可见输出**（1.0.9）：原来只拼 `stderr`，而 `gh` 网络失败时 stderr 是空的，日志就变成
  「私有性查询失败： ——」，事后无法区分是网络、凭据还是仓库名错。现在写成 `私有性查询失败 rc=1：gh: Not Found (HTTP 404)`
  （`stderr or stdout or "无输出"` 兜底）。一般规律：**任何 fail-closed 分支的日志都要带上能定性的最少信息**（rc + 可见输出），
  否则下一次只能靠猜。真实演练：某轮因网络抖动查不到私有性 ⇒ exit 4 拒推，下一轮自动恢复——fail-closed 没误伤也没静默放行。

- **远程 URL 解析要容忍端口**（0.4.52）：`ssh://git@ssh.github.com:443/owner/repo.git`（备用通道）原正则把 `:443` 当 owner 段、解析失败，
  于是属主为空 → 私有性检查被跳过。正则已加 `(?::\d+)?`；实测 5 种 URL 形态（ssh+端口/ssh/https/.git 有无）全部解析正确。

- **总量安全闸跟着换代阈值走**（0.4.49）：`SIZE_GUARD_TOTAL` 原固定 4GB，而换代阈值已上调到 12GB ⇒ 同步会先于换代在 4GB 停摆。
  现在 `SIZE_GUARD_TOTAL = (ROTATE_AT_GB + 1) GB`，即先换代、再撞闸。

- **校验远程状态要用具体 ref，不要用 `HEAD`**（0.4.35）：`git ls-remote <本地路径> HEAD` 在本地传输下返回空，
  曾把「5 批全部推送成功」误判成失败（exit 1）。一律取 `refs/heads/main`（必要处加重试）。

- **容量判据只看 `gh api … --jq .size`**：`churn-report.py` 是「新增 blob 字节」，不是仓体积增长（整份重写的文件 git 仍会 delta）。实测同一时段 churn 130–155MB/轮，而远端只涨 ≈14MB/小时。

- **失败诊断锚**：同步失败先读 ① `meta/sync-state.json` 的 `git_error` / `committed` / `pushed` / `finished_at`（失败路径也会写 meta）；② 镜像 `logs/last-run.log`（每轮全量日志）。**提交里的 meta 滞后一版**——write_meta 在 commit 之后执行，所以最新提交里包含的是**上一轮**的 sync-state（工作区 `meta/sync-state.json` 常显 modified，属正常，随下轮提交）。

- **网关因「检出被换版本」自停也会连坐在跑轮次**：`gateway.log` 出现 `ERROR gateway.runtime_downgrade_exit: This gateway loaded …, but its checkout is now at …, a release that predates the gateway runtime` ⇒ 有人（update/回滚）把 checkout 切到更老的版本，网关按设计自杀，在跑的 cron 被 drain 掐掉。此时排查顺序是**先让检出稳定**（`git -C hermes-agent reflog` 看切版本的时刻与来源），不要去动备份脚本。

- **多机/线上领先**：push 被拒会自动 `pull --no-rebase` 重试一次；出现合并冲突则中止待人工（口径：线上优先、本地另存，勿 force）。

## 恢复与迁移

- **恢复要带 `--verify`**（0.4.22）：`restore_sessions.py --fast-import --verify` 会在导入后**逐会话**把「镜像里的消息条数」与「恢复库里的行数」对齐，不一致就打印明细并让退出码=3。以前演练靠人肉比对（我手动抽 7 个会话），现在一条命令出结论；实测 mentoragent 11/638 「不一致 0 个」。

- **跑会 import `hermes_state` 的脚本必须经 `<REPO>/tools/run_in_runtime.py`**（备份仓内副本；技能仓只有 `scripts/`）：在本机终端里 `python <脚本>.py` 会被启动器以**另一个解释器**（缺 ruamel 等运行期依赖）经 runpy 重跑，报 `ModuleNotFoundError: No module named 'ruamel'`；就是 `python -c "import <模块>"` 也不能豁免（`SessionDB(...)` 构造会把进程切到工具树 python）。正确形态一律是 `python <REPO>/tools/run_in_runtime.py <脚本> [参数]`——恢复、对账、A/B 测试脚本皆同。同理：**恢复/对账不要与 `hermes update` 并发**，更新收尾持有 checkout 锁，卡住的子进程会 0% CPU 空转（判据：RSS 几 MB、CPU 0、日志停在启动横幅）。

- **文档引用的文件必须在仓里**（0.4.50）：`scripts/check-parts.py`、`scripts/verify_drill.py`、`references/restore-drill.md`
  三个文件此前只在本地、**从未发布**——按 SKILL.md 执行会直接「文件不存在」。已随本版发布（check-parts 实跑镜像：`PARTS-OK`）。

- **入口脚本的 `--help` 不得触发真动作**（0.4.50）：`hb-sentinel.py` / `hb-autosync.py` 原来没有参数解析，
  `--help` 会被忽略并**真跑一轮同步**（实测子代理这么做触发过一次真实同步）；`verify_drill.py` 把 `--help` 当路径直接崩。三个都加了短路。

- **模块级 `MIRROR` 也要走解析链**（0.4.47）：`import sync` 后直接调 `dump_routing()`/`sync_tools()` 而不经 `main()` 时，
  旧默认值是 `~/hermes-backup` —— 实测把 supplement 写进了 `C:\Users\<user>\hermes-backup`。现在模块级默认值同样按 env → local.json → `~/hermes-backup` → `~/hermes-backup` 解析。

- **哨兵有超期兜底**（0.4.27）：周期 cron 的 `next_run_at` 会被网关重启重排（实测把 05:57 挪到 06:29，空窗 2.5h）；哨兵现在除「网关重启 / 静默 5 分钟 / 长静默恢复」外，还有 **⑤ overdue**——镜像 `finished_at` 超过 2.5 小时、没在同步、也不满足 20 分钟新鲜闸，就补一轮。回归＝`test-hb-sentinel.py` S8/S9。

- **官方 schema 会加触发器，fast-import 只摘"索引类"**（v0.22.5 实测）：库里现有 **13 个**触发器——除 display 4 个 + FTS 6 个（这 10 个由 fast-import 摘除、导入后重算/rebuild/原样恢复），还有 3 个**保持生效**：`messages_message_uid_insert`（插入时填 `message_uid`）、`messages_transcript_revision_update`（维护会话修订号）、`sessions_runtime_ledger_delete_guard`（删除护栏，与导入无关）。恢复结束会自检 `message_uid` 空值并打印 `uid 空值 0/N`；**空值 >0 ⇒ 官方改名/失效信号**，先核对官方 schema 再改 `INDEX_TRIGGERS`，别盲摘。**触发器总数随官方版本变**（实测 v0.22.2＝11 个＝10 索引＋uid；v0.22.5＝13 个）——脚本与验收里别写死数量，只断言「索引类 10 个可摘可原样还原」＋「uid 空值 0/N」。v0.22.5 实测：21,693 条巨会话 `--fast-import` 恢复 + FTS 重建 **17.4s**、`message_uid` 全非空。

- **`restore_sessions.py --source` 收的是「父目录」不是 profile 目录**：结构必须是 `<src>/<profile>/*.jsonl`（`--source …/src` 对应 `src/main/…`）；指到 `src/main` 会去找 `src/main/main`，报源目录不存在。

- **官方导入尺寸上限**（≤5MB/≤1万条/单次 ≤25MB 等）：面向 Web 场景；恢复工具在**本进程内**放宽这些常量（不改官方文件）。若官方改版导致常量改名，工具会记录并继续（检查 `hasattr`）。

## 哨兵与自愈

- **成功轮要清 `last_error`**（0.4.17）：健康文件是连续性记账的单一来源，失败时写 `last_error`、成功时必须置 `None`——否则「上一轮 timeout 3300s」会一直挂着，被后读的人（包括我）误判成当前仍在失败。`repaired_note` 属历史注记，保留不动。

- **哨兵看锁的「活性」而不是「存在」**（0.4.58，2026-10-26 审查发现）：原来只要 `SYNC_LOCK` 文件在就跳过触发——
  某一轮被杀留下锁后，哨兵就再也不会补跑（直到人工删锁）。现在读锁里的 pid，持有进程已死 ⇒ 视为没在跑，照常触发
  （读不懂就当在跑，保守）。回归＝`test-hb-sentinel.py` 的 S5（活 pid 不触发）/ S5b（死 pid 不阻塞）。

- **首轮失败后不会永远不再重试**（0.4.58）：`overdue` 原要求「有上次成功时间」，从未成功过时 `age=None` 就永远不触发；
  现在补 `overdue-no-state`：从无成功记录且上次触发已超 2.5h ⇒ 再补一轮。回归＝S10。

- **哨兵与周期入口的单轮超时统一 3900s**（0.4.50）：哨兵原写 3300s，与文档/autosync 的 3900s 不一致。

- **重启网关会连坐当前对话**：`hermes gateway restart` 由收尾器在桌面端关闭后执行，不要在对话中途手动跑。

## 其它

- **新机上第一次同步报「Please tell me who you are」**（0.4.62）：镜像是用户手工 clone 的（`.git` 已存在，
  走不到 `git init` 分支），而本机没配 git 身份 ⇒ commit 失败。现在 `ensure_repo()` 会「缺什么补什么」：
  本仓 `user.email` 缺 → 补 `<owner>@users.noreply.github.com`；`user.name` 缺 → 补 owner（有全局身份时不动）。
  实测：清空全局配置的临时仓，调用后两项都补上。

- **桌面端 Local Storage 被占用**：群聊/置顶数据只有桌面端退出后才能安全覆盖 → 收尾器处理。

- **后台轮的完成通知会滞后且成批到达**：通知里的命令输出可能对应**已被后续轮取代**的旧轮（失败也可能早被补推/修复）。处理前先对时——读 `state/hb-sync-runs.jsonl` 最后一行的轮次时间与 `state/hb-sync-health.json` 的 `updated_at`；旧通知若已被后继轮补齐，只记一笔、不重跑、不回滚已改配置。

## 运行环境与并发（2026-10-26 增补）

- **Windows 上判进程活不能用 `kill -0` / `ps`**（1.0.3/1.0.4 实测）：MSYS 的 `kill -0 <pid>` 只看得到 MSYS 进程，
  原生 Windows 进程一律报「不存在」——`rotate-generation.sh` 因此把**活锁当死锁**放行（差点在同步轮中途换代）。
  一律用 `python -c "import psutil; psutil.pid_exists(pid)"`（或 `psutil.Process(pid).is_running()`）；`ps -W | grep 脚本名` 也永远匹配不到 python 子进程（只显示可执行名）。
  另：同步锁文件内容是 **JSON**（`{"pid": N, "started": …}`），用 `tr -dc '0-9'` 抽 pid 会把时间戳拼进去，必须按字段解析。
- **并发时给手工作业也挂锁**：手工跑 push/repack/换代这类会写镜像的活时，手写一份 `state/hermes-backup-sync.lock`（含自己的 pid），
  让 cron 轮看到「已有同步在跑」而让路；完事再删。否则哨兵的轮次会和手工 push 抢同一个仓（ref 锁冲突 / 互相拖慢）。

- **子代理会把 `HERMES_HOME`/`TEMP` 泄漏到共享 shell**：Hermes 终端是共享快照，子代理 `export` 的环境变量会留在里面。
  实测：某子代理把 `HERMES_HOME` 指到它的沙箱后，后续 `audit-coverage.py --profile X` 对着**假 home** 跑、报「跳过」并 rc=2；
  `TEMP=/tmp` 还会让 Node 的临时目录解析错位。收工/换手前先 `echo $HERMES_HOME` 核对；一切写操作都用**显式 `--home`**。
- **锁要原子建、且只删自己的**（0.4.64）：`exists()` 再 `write_text()` 是 check-then-write，两个触发源可能同时通过检查；
  且退出钩子若不校验 pid，会把"接管者"的锁删掉 → 双写者同时 add/commit。现在 `acquire_lock` 用 `O_CREAT|O_EXCL`，
  `release_lock` 只在锁内 pid == 自己时才删。
- **导出阶段的超时要记账**（0.4.64）：逐档 CLI 的 `subprocess.run(timeout=900)` 抛 `TimeoutExpired`，原来 main 只 catch `RuntimeError`
  ⇒ traceback 直接退出、**不写 runs.jsonl / health / meta**（连续失败计数永不增长、告警永不触发）。现在一并按失败记账。
- **分片必须 1..N 连续**（0.4.64）：缺片时拼出来是坏 JSON，会被报成「读取失败」掩盖真因；`load_session` 现在先校验编号，
  直接说「缺分片 [2]」。
- **fast-import 的触发器还原要放 `finally`**（0.4.64）：重建过程中途抛错（库被占用很常见）会让 display/FTS 触发器永久缺失，
  之后所有消息都不再进索引且没人知道。

- **同步轮期间跑重活，会把一轮从 500s 拉到 2700s**（2026-10-29 实案）：同一天里干净轮 **485s / 496s**，
  而撞上 `git reset --hard`（3.4GB）与自检/克隆的那两轮是 **1605s / 2697s**。第一反应容易归因成"新磁盘慢"——
  实测 3,000 个小文件写入 C: **2,227/s**、D: 1,954/s，小文件并不慢，慢在并发争抢。
  口径：**报"变慢"前先确认那一轮旁边有没有别的重活**；要测基线就挑干净窗口，别拿被污染的样本下结论。
