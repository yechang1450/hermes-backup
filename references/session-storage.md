# 会话存储格式与历史体积

## 导出格式真相（先读这条再去解析/比对）

- `hermes sessions export --format jsonl` 输出：**一行 = 一个会话**；一行内容 = **整个会话的单个超长 JSON**（全部消息都在「这一行」里；112–245MB 量级的单行都会出现）。
- 解析/比对前先数行数（`raw.split(b'\n')`——大文件常只有 2 个元素＝巨行＋结尾空串）。不要按「一行一消息」的直觉写代码。
- 会话 JSON 顶层约 65 个字段：元数据（message_count / *_tokens / last_active(_at) / cost_* / model_config / timings / tool_call_count …）＋ `messages` 数组。
- **`messages` 数组＝严格尾部追加**（实测：相邻两版 514 条重叠消息逐条深比较完全相同；新版＝旧版＋21 条）——这是增量友好存储成立的依据（追加只动尾部、旧区段字节不变）。

## 历史体积机制（为什么整包 gz 是错的）

- 每个真实回合都改动头部约 13 个元数据字段；**整包 gzip 对「头部一点变化」的反应＝deflate 流从头到尾字节全变**（实测两版 gz 的字节重叠 **0.0%**）→ git delta 找不到任何可复用区段 → **每版完整入库**。
- 实测代价：每轮同步 13–22 个会话文件变化、合计 **~226MB** 入历史（5–7 个巨头会话各自整版重传：77.8＋66.8＋36.2＋18.4＋12.7＋9.4＋7.2MB）；仓库增长 ~0.5GB/天（3–4 轮/天）。
- 「覆盖」错觉：同步的强制覆盖语义只作用于**最新树**（≈镜像工作区大小）；git 仓库＝最新树＋每版历史（实测 2.01GB vs 0.72GB；单会话 10 个版本＝313MB）。**本地体量不是仓库体量的上限。**
- GitHub 口径：**性能建议 <5GB、软上限 ≤10GB**（换代码库请按 `references/generations.md` 的 12GB 阈值执行）→ 按现值数周内触线（**v0.3 已治理，见下节；单文件 95MB 同步闸的逼近风险由分片拆除**）。

## v0.3 存储格式（已落地，0.3.0）——原样文本 ＋ 单实例锁

最终选型：**会话文件原样存文本，不压缩**——`sessions/<档>/<sid>.jsonl`（一行=一个会话的原始超长 JSON，逐字节保真）；单件 **>12MB** 的巨会话按 12MB 切片为 `<sid>.part-0001.jsonl`、`<sid>.part-0002.jsonl`…（拼接顺序＝文件名序，恢复侧逐字节还原）。**阈值演进：90MB → 45MB（0.4.10）→ 12MB（0.4.26，见本文末「分片阈值」节）**；45MB 那一步的动机是低于 GitHub「建议 ≤50MB」、对 100MB 硬限留 2.2 倍余量，12MB 那一步的动机是尾部稳定化后单轮重写量≈末片大小——阈值直接决定每轮重写多少。等价性由 `scripts/test-split.py` 回归（合成会话切片后按片号拼接：小会话逐字节相同、>5MB 会话语义等价）。

原理：git 自带 delta 引擎本就是为**未预压缩的文本**设计的——只要不把文件 gzip 掉，版本间未变区段就能被逐块命中，增量才是增量的样子。另配单实例防撞锁（`HERMES_HOME/state/hermes-backup-sync.lock`；崩溃遗留 3h 后自动接管）。

实测对照（同一真实大会话连续两版；scratch 仓库各自 `git gc` 后取 `count-objects -vH` 的 size-pack 差）：

| 方案 | 一轮入库增量 |
|---|---|
| 现行整包 gz | **+79.40 MB** |
| meta + 消息分块 gz（备选，未采用） | +0.02 MB |
| **原样文本（已采用）** | **+0.01 MB（整件）／+2.25 MB（切片后）** |

选型理由：数值最优、**零解析零重编码（100% 字节保真）**、代码与 CPU 最省；备选「meta + 分块 gz」数值相当，但需要解析＋重序列化（保真风险）与额外分块逻辑，弃用。

兼容与迁移：

- 恢复侧**三形态并存**：`.jsonl`（新）／`.part-NNNN.jsonl`（新分片）／`.jsonl.gz`（旧整包）都可读；`restore_sessions.py` 按会话分组、分片按名序拼接后走官方导入管线；`concat_sessions.py`（桌面导入拼包）同步支持。
- 无需重写历史：下一轮同步自然产出新格式；旧 gz 文件留在 git 历史中（保留回滚能力）。
- 体量注记：以不压缩换增量友好 → 镜像工作区与仓库最新树尺寸增大（文本级）；本地对象库仍按增量增长。总量闸随 v3 调至 4GB；单文件闸 95MB 降级为兜底（切片后最大件 12MB）。

## 实测方法（复用于任何「体积/增量」问题）

- **逐文件版本账**：`for c in $(git log --format=%H -- <path>); do git cat-file -s "$c:<path>"; done` ⇒ 版本数 × 各版体积（证明历史堆积）。
- **入库增量对照实验**：scratch 仓库提交 v1 → `git gc` → 记 `count-objects -vH` 的 size-pack → 提交 v2 → `git gc` → 再记 → 差值＝**真正入库字节**。
  - 关键：**delta 压缩只在打包时发生**——不跑 `git gc` 看到的是松散对象的全尺寸，会得出错误结论。
- 比对两版字节前先想清格式：**单行巨 JSON 别做逐字节/分块平移比对**（长度微变会整体平移，「全部不同」是假象）；先 `json.loads` 做**结构级比对**（逐字段、逐消息元素）才说明问题。
- 体积结论一律用**本地对象库**直接算；GitHub 的 `size` 字段更新有滞后，不作实时依据。

## 单轮 churn 归因（「谁在把仓库撑大」的现成流程）

不要靠感觉判断「这轮是不是又重写了半个库」，三步落到字节：

```bash
# 1) 这轮改了哪些 blob（含新旧 sha 与路径）
git -C <repo> diff-tree -r --no-commit-id --raw HEAD~1 HEAD
# 2) 按目录聚合「新增内容字节」：批量取尺寸再累加（比 git diff --stat 准）
python - <<'EOF'
import subprocess, collections
out = subprocess.run(['git','-C','<repo>','diff-tree','-r','--no-commit-id','--raw','HEAD~1','HEAD'],
                     capture_output=True, text=True).stdout
rows = [(p[3], p[-1]) for p in (l.split() for l in out.splitlines()) if len(p) >= 5]
shas = sorted({s for s, _ in rows if s.strip('0').strip()})
sz = {}
pr = subprocess.run(['git','-C','<repo>','cat-file','--batch-check=%(objectname) %(objectsize)'],
                    input='\n'.join(shas), capture_output=True, text=True)
for ln in pr.stdout.splitlines():
    a, b = ln.split(); sz[a] = int(b)
agg = collections.Counter()
for s, path in rows:
    agg['/'.join(path.split('/')[:2])] += sz.get(s, 0)
for k, v in agg.most_common(10):
    print('%-34s %8.1f MB' % (k, v / 1048576))
EOF
# 3) 行级分布（哪些目录文件数最多——与 2 对照看是「少量大文件」还是「大量小文件」）
git -C <repo> diff --numstat HEAD~1 HEAD | awk '{split($3,p,"/"); d=p[1]"/"p[2]; n[d]++} END {for (k in n) print n[k], k}' | sort -rn | head
```

判读（比「仓又大了」有用得多）：

- **同一会话的多个 part 同时变、而该会话并没有新消息** ⇒ 头部统计字段在变，整行字节移位，所有分片都成新 blob（见下节「尾部稳定化」）。
- **只有末片变** ⇒ 正常追加，成本 ≈ 一个 `PART_MAX`（活跃巨会话每轮的上限）。
- **`home-overlay/` / `profiles-overlay/` 大面积变化** ⇒ 采集层在重建（mtime/ctime 变化让 git 重哈希），不是会话数据；去查 `reset_dir` / 写入跳过是否失效。
- **分片是按固定字节切出来的（每片恒为 `PART_MAX`）**：拿单个 part 做逐字节比对**没有意义**，要比就比拼接后的整份，或直接看 diff 里首个差异偏移。
- 远端速率用两次 `gh api repos/<owner>/<repo> --jq .size`（KB）取差除以小时数；**`.size` 有滞后，只用来算趋势与换代 ETA，不用来判单轮成败**。

## 链路限制（GitHub 官方数字）

- 推送频率：建议 ≤6 次/分钟/仓库；单次推送 **2GB 强制上限**；单文件 **100MB 强制**（建议 1MB）；仓库磁盘建议 **≤10GB**（软）。
- 结论：**频次从来不是瓶颈**（现用量 3–4 次/天）；唯一要盯的是**体积增长**。

## 导出提速（0.3.5，sync v3.3，2026-10-20）

- 根因：每档一次 `hermes` CLI 启动（含插件加载）是单轮最大浪费——16 档 × 20–70s 固定开销；档位从 11 涨到 16、主库涨到 1.7GB 后，忙时单轮可超 55 分钟被超时截杀（2026-10-19 夜真实发生 3 次）。
- 方案：`export_all_profiles.py` 在单个官方运行时进程里直调 `export_projection / export_all / render_sessions_export`（与 `hermes sessions export --format jsonl` 同一数据路径）——**实测输出逐字节一致**，全部 16 档 82–101s 完成（原 11–15 分钟，约 8x）。
- 安全：失败自动回退逐档 CLI（原路径原样保留）；尺寸守卫按官方 `sessions.max_export_messages` 语义复刻。
- **官方旗标先探针、别想当然**（已实测）：`--session-id` 一次只认一个值；`--after / --newer-than` 按**会话起始时间**过滤（"老会话近期有活动"的增量场景无效，实测导出 0 条）——想按变化增量导出只能直调 `export_all`，不要拿过滤旗标拼。
- **逐字节保真坑**：CLI 导出在 Windows 上写 **CRLF**（默认换行翻译）；复刻端若用 `newline="\n"` 会出现每行 1 字节差异。复刻写法＝`open(p, "w", encoding="utf-8")`（留默认换行），验证法＝「同参数 CLI 连跑两次输出逐字节相同」作金标准再 `cmp`。
- 批量导出与 CLI bare export 同内存画像（全库一次性加载）；进程内导出必须经 `run_in_runtime.py` 引导，不裸跑。

## 写入优化（0.3.6，sync v3.4，2026-10-21）
- 旧行为：每轮先清空 `sessions/` 再全量重写 → 7000+ 文件 mtime 全变 → `git add` 对近 2GB 全树重新哈希（忙时 10–20 分钟，是慢轮的最大单项）。
- 新行为：写盘前比对内容，**相同就不重写**（mtime 保持不动）；过期文件/目录由 `prune_stale` 收敛（不再整目录清空）。
- 依据（2026-10-21 08:25 轮实测）：全轮只有 **44 个会话文件**真实变化（257 文件/8005 行净差异），旧行为却让 git 重算整棵树；该轮总时长 43 分钟。
- **更正（2026-10-21 09:28 实测）**：慢轮尾段**不是** auto-gc（干净树上 `git add -A` 仅 6 秒）——真因＝**覆盖层每轮被 `reset_dir` 重建，4.5 万个小文件的 ctime 全变，git 因此每轮重哈希 ~3GB**（home-overlay 794MB/45,716 文件 + desktop-overlay 393MB + sessions 1.79GB）。v3.5 把同一「内容相同不重写」套到全部覆盖层，prune 同步扩到三层。
- 日志：`git 提交完成：Ns（含 add 与自动 gc）` / `git 推送完成：Ns`——便于以后一眼定位尾段去向。

## 备份范围与容量（0.3.8，2026-10-21）

- **kanban 只备数据库**（`boards/*/kanban.db`，实测 30MB/84 库）；任务运行工作区、证据、日志属过程记录不入云——实测该目录 96% 的体积（约 730MB/4.3 万文件，含 105 个 .pack 的整份 git 克隆）都是这类过程件。
- 层级体量快照（2026-10-21 20:00 实测）：sessions 1.79GB（会话文本）、desktop-overlay 393MB（composer 图片等用户内容）、home-overlay 794MB→**约 36MB**（kanban 瘦身后）、profiles-overlay 65MB、supplement 微小。
- **GitHub 侧体积** `gh api repos/<owner>/<repo> --jq .size`（KB）：2026-10-21 实测 **4,169,638 ≈ 3.98GB**，官方口径理想 <1GB、**强烈建议 <5GB**；
- 增速（活动高峰实测）：树 +22MB/小时 ≈ 530MB/天，压缩后约 +150–200MB/天；
- **换代阈值**：远端体积 ≥12GB（2026-10-25 由 4.5GB 上调；或克隆 >20 分钟 / 收到 GitHub 提醒）→ 按 `references/generations.md` 换代归档（新建一代仓、旧仓冻结为只读档案，不重写历史、不停同步）。

## 恢复提速 `--fast-import`（0.4.0，2026-10-22）

**问题**：官方导入路径 `_import_session_row` 对会话里的归档消息**逐行** `UPDATE messages SET active=0, compacted=? WHERE id=?`；该 UPDATE 每命中一行就触发 `messages_display_visibility_update`，其第三条语句要**重写同会话、同 `display_identity` 的全部可见兄弟行**。可见行的每一次「可见→隐藏」翻转都扫一遍全组 ⇒ **O(n²)**。实测（2026-10-21 第三轮演练）：单会话 72,571 条消息占掉 73 分钟导入里的 **约 50 分钟**。

**做法**（全走官方件，不改官方文件）：
1. 导入前从目标库 `sqlite_master` 读取四个 display 触发器（`messages_display_order_insert` / `messages_display_visibility_update` / `messages_display_identity_update` / `messages_display_identity_delete`）的**原文 DDL** 并 DROP；
2. 正常跑官方 `import_sessions()`；
3. 对每个已导入会话调用官方 `SessionDB._ensure_display_order(sid)`（内部 `_reconcile_display_orders`：1000 条/批，按 `_display_dedupe_key` 重算 `display_identity=sha256(key)`、`display_order=同组首个可见行 id`，仅漂移才写）；
4. 一次性 `UPDATE messages SET display_order = id WHERE display_order IS NULL`（补齐不可见行，官方原由 INSERT 触发器补的等价值）；
5. 用第 1 步捕获的 DDL 原样重建四个触发器。

**为什么安全**：
- 官方读路径本身会自愈——`_ensure_display_order` 在 `include_compacted and not include_inactive` 的读取分支里被调用（`hermes_state_messages.py:1418`），即使第 3 步没做，下次读也会补；
- 只动显示投影列（`display_order`/`display_identity`），**不碰** `active`/`compacted`/内容/时间戳；FTS 与 `message_uid` 触发器全程保留；
- 失败兜底是"重建自己的 DDL"：即使中途异常，`finally` 仍会尝试恢复触发器，且显示列最坏是"下次读取时自愈"。

**实测（2026-10-22）**：
- 等价性（会话 `20261018_212441_6ddd5f`，2,780 条 / 2,593 归档 / 2,003 compacted）：控制组 28.5s vs 新路径 4.4s+0.7s＝**约 5.6 倍**；2,780 行基础字段 0 差异，**可见行 2,190 行 display_order/display_identity 0 差异**，不可见行 590 行也 0 差异；缺索引探针两库均为空；触发器 4→4 且 DDL 逐字一致；
- 全量演练（第 2 代仓、1,491 会话）：见 `temp/hb-drill4-20261022/RECEIPT.md`（53m24s，差异 0 项）。

### FTS 也一并摘除（0.4.1）

巨会话导入 39 分钟里，display 重建只占 123 秒——**大头的剩余成本在 FTS**：`messages_fts_*` 与 `messages_fts_trigram_*` 六个触发器逐行 tokenize 正文，导入路径的剪枝删除还要逐行从索引里摘除。做法同 display：

1. 导入前把六个 FTS 触发器一起摘掉（连同 display 共 **10 个**）；
2. 导入后用官方 FTS5 命令一次性重建：`INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`、`INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')`（外部内容表，从 `messages` 全量重排索引）；
3. 最后用捕获的原文 DDL 恢复 10 个触发器。

**A/B 实测（巨会话 `20260927_212311_3a35f0`，72,625 条消息，2026-10-22）**：
| 口径 | 导入总耗时 | FTS 查询 `compaction` |
|---|---|---|
| A：display + FTS 全摘，导入后 rebuild | **55.8s** | 主索引 1,080 条 / trigram 40 条 |
| B：只摘 display（0.4.0 行为） | 143.2s | 1,080 / 40（与 A 完全一致） |

⇒ **2.57 倍**，且检索结果逐项一致、行数一致（72,625）。代价是重建后索引由 FTS5 统一生成（与触发器逐行维护的最终状态等价，官方 `'rebuild'` 命令语义）。

## 覆盖率审计 `audit-coverage.py`（0.4.5，2026-10-23）

演练证明"镜像能恢复"，这个工具回答另一半：**镜像里装的是不是活库的全部内容**。

```bash
python scripts/audit-coverage.py --repo D:/hermes-backup --home %LOCALAPPDATA%/hermes [--json out.json]
```

判据（硬伤 ⇒ 退出码 1）：
| 项 | 含义 | 是否硬伤 |
|---|---|---|
| 缺会话 | 活库有会话行、镜像无文件 | **是**（除"轮内新建"豁免，见下） |
| 多会话 | 镜像有文件、活库无会话行 | **是**（prune 应收掉） |
| 镜像超前 | 同一会话镜像条数 **多于** 活库 | **是**（数据错位/库回滚） |
| 落后 | 同一会话镜像条数**少于**活库 | 否（上一轮快照后的增量） |
| 孤立消息组 | `messages` 有行、`sessions` 无会话行 | 否（活库历史残留，导出按会话走，本来就不进镜像） |
| 解析失败 | 会话文件 JSON 解不开 | **是** |

"轮内新建"豁免：一轮是「先采集（约 10–80s）→ 导出 → 提交 → 推送」，采集之后新建的会话本来就不该在这轮镜像里；豁免线取 `meta/sync-state.json` 的 **`started_at`**（不是 `finished_at`）。

首跑（2026-10-23 01:26）：活库 1,854 会话 / 352,698 消息 ⇒ 镜像 1,846 / 350,923；缺 0（豁免 7）、多 0、超前 0、落后 905 条、孤立 26 组 / 3,904 条、解析失败 0 ⇒ **AUDIT-PASS**（25 秒）。

### 文件层（`--with-files`，0.4.7）

会话层只覆盖"对话"，文件层覆盖"配置/记忆/技能/平台数据"这一半：

- **白名单单一来源**：直接 `import sync` 读 `HOME_FILES` / `HOME_DIRS`，不复制一份清单（防止两处漂移）。
- **分类**：`采集`（白名单内）/ `采集(profiles-overlay)`（`profiles/` 由 `collect_profiles()` 单独落盘）/ `采集(按会话文件)`（`sessions/`）/ `排除(运行时)`（缓存、二进制、锁、库 WAL）/ `未分类`（**可能漏备**）。
- **硬伤只有一种**：白名单内、且目录里确有可采集内容（排除锁/`__pycache__`/`node_modules` 后 >0 个文件）、但镜像里没有 → 说明采集链断了。空目录不算（git 不跟踪空目录）。
- `state.db` 标为 `排除(会话按导出承载)`：它 1.9GB，内容由按会话导出的 `.jsonl` 承载，不整库入仓。

首跑（2026-10-23 02:0x）：21 个采集项、**硬伤 0**、未分类 1＝`secrets/`（114 字节的凭据文件，是否纳入备份由 owner 定）。副产物：`lsp/`（10,071 文件 / 83MB）、`state-snapshots/`（565MB，10-12 重装前快照）、`profiles/`（17GB，已由 profiles-overlay 承载）都被正确识别为运行时/已覆盖，不再误报。

### 负向对照（0.4.8）

"审计一直 PASS"可能只是"审计不看东西"。`scripts/test-audit-coverage.py` 用合成夹具（假 HERMES_HOME + 假镜像）跑 8 个场景，全部断言退出码与关键输出：

| 场景 | 期望 |
|---|---|
| 全等 | PASS |
| 镜像缺一个会话 | FAIL 且点名该会话 |
| 镜像多一个会话 | FAIL |
| 镜像条数多于活库（超前） | FAIL |
| 镜像条数少于活库（落后） | PASS（正常增量） |
| 本轮开始后新建会话 | PASS（豁免，`缺 0(新建豁免 1)`） |
| 孤立消息组（有消息无会话行） | PASS 且计数正确 |
| 会话文件坏 JSON | FAIL |

实测 **8/8 通过**。<br>运行：`python scripts/test-audit-coverage.py`（夹具落在 `TMPDIR` 下，跑完自清）。

**判读坑**：负向对照首次跑红，先怀疑**夹具的基准**而不是审计器——"全等"场景必须真全等（夹具的基准镜像漏写一个会话文件，就会让"全等→缺 1"并带红五个场景）。先修夹具，再怀疑工具。夹具一律用现建 sqlite ＋ 假会话文件，**不指向真库**（否则会复制真实数据进临时目录，也测不准边界）。

## 尾部稳定化：让"追加消息"只改动文件尾部（0.4.18）

**现象（2026-10-24）**：单轮同步有 **827MB** 内容被重写——10 个巨会话的 3–6 个 45MB 分片**全部**变成新 blob，
而其中大部分会话并没有新消息。GitHub `.size` 8 小时从 1.56GB 涨到 2.82GB（≈150MB/小时）。

**根因**：会话导出是「一行=一个会话」的超长 JSON，头部含 `message_count`、token 计数、`timings`、
`last_activity_at`、`transcript_revision` 等**每轮都变**的统计字段。头部一改，整行字节随之偏移/不同，
按固定字节偏移切出来的每个分片都成了新内容——git 的 delta 完全失效。

**修法（0.4.20 起为反向白名单）**：`scripts/session_layout.py` 在切片前只把
**身份字段**（`id/source/created_source/user_id/session_key/chat_id/chat_type/thread_id/display_name/
origin_json/parent_session_id/started_at`）与 `messages` 留在前半段，其余顶层成员**整段剪下移到对象末尾**：
- 零解析：字符串感知的顶层扫描（跳过字符串里的逗号/括号），不 `json.loads`（243MB 会话要 ~2GB 峰值内存）；
- 不改内容：每个成员的字节原样保留，只改成员顺序；
- 效果：未变会话字节完全一致（写入跳过）；有追加的会话只在**末尾**产生差异 ⇒ 只有末片变化；
- 只对 >5MB 的会话行生效；解析失败/截断一律原样返回（绝不丢数据）。

**回归**：`scripts/test-layout.py` 六项——语义等价（`json.loads` 前后相同）、幂等、易变字段确实落在尾部、
追加一条消息后 `messages` 值前缀逐字节不变、40MB 合成会话 ~3s。

### 为什么第一版（0.4.18）没管用
第一版是**正向列举**"易变字段"（15 个统计字段）。实测立刻失效：头部还有 `last_activity_description`、
`runtime_revision`、`handoff_state` 等**也会变但不在此列**的字段，任何一个变化都会把整行推翻。
⇒ 改成**反向白名单**：不去猜哪些会变，只固定"身份 + 正文"必须在前，其余一律挪尾。

### 实证（2026-10-24，`3a35f0` 243MB 巨会话）
取 18:44 与 20:37 两轮的真实内容，各自按新规则稳定化后逐字节比较：

| 读数 | 值 |
|---|---|
| 稳定化后长度 | 235,390,683 vs 235,617,762 |
| **首个差异偏移** | **227,115,602（96.5%）** |
| 差异内容 | 恰好是新追加的一条消息（`id 230792`）|

⇒ 前 227MB 逐字节相同，只有尾部在动；下一轮起，未变会话零改动、有追加的只改末片。

### 仍然存在的次要churn（未处理，属"备份什么"的政策范围）
- `desktop-overlay/Local Storage/leveldb/**`（Chromium 本地存储，每轮 4–5MB）；
- `home-overlay/.curator_backups/**`（技能策展快照，每轮 4–8MB）；
- `profiles-overlay/<档>/**`（每档 30–150 个文件，每轮 0.6–2MB）。

三项合计 ~15MB/轮（≈7MB/小时），与"每轮 900MB"相比属次要；是否排除属备份范围决策，未擅自改。

### 分片阈值 45MB → 12MB（0.4.26）
尾部稳定化上线后，每轮重写量＝**发生变化的那些分片**之和。实测 45MB 阈值下稳态 249MB/轮，
其中 208MB 全是 10 个活动会话的末片（每个接近 45MB）。阈值降到 12MB 后，同样的活动量只需
重写 ≤12MB/会话；代价是文件数从 ~76 增到 ~300，git 与审计都能承受（拼接按片名排序，逻辑不变）。

**回归**：`scripts/test-split.py` 现在按 `sync.PART_MAX` 动态断言——切片后按片名拼回，
>5MB 的会话判**语义等价**（`json.loads` 相同），小会话仍判**逐字节相同**（因为大会话会被稳定化重排成员顺序）。

**改阈值后必做一次「多分片真恢复」**：字节/语义等价只证明切分无损，证明不了导入管线能吃下多片。
挑一个切成 >10 片的会话，按片拷进 `<src>/<profile>/`，跑
`restore_sessions.py --source <src> --profile <档> --fast-import --verify`；
实测 243MB 会话切成 **21 片**、恢复 **73,546 条**、`verify_failed=0`。

