# 选择性恢复配方（clone → 盘点 → 菜单 → 逐键合并）

**何时用**：用户要挑着恢复、只想拿回聊天记录、或者**备份来自同一台机器的较早时刻**（重装/升级后）——
这时整机覆盖会把他现在还能跑的环境改坏。全新机器的一键恢复仍按 `templates/RESTORE.md`。

---

## 0. 先 clone，再谈方案

```bash
HB="$HOME/hermes-backup"
git clone --depth 1 --single-branch "https://<TOKEN>@github.com/<owner>/<repo>.git" "$HB"
git -C "$HB" remote set-url origin "https://github.com/<owner>/<repo>.git"   # 别让 token 留在 .git/config
```

- URL 形式：**把 token 当用户名**（`https://<token>@github.com/...`）在 git-for-windows 上稳定；
  `https://x-access-token:<token>@...` 可能报 `fatal: expected flush after ref listing`，换前者即可。
- 克隆后先验：`git status --porcelain`（应为空）、`git fsck --no-dangling`（应无输出）、
  `git ls-files | wc -l`。
- **三个体积数别混**：`gh api .size`（含完整历史）/ 本地 `.git`（depth-1 单 pack，通常小得多）/
  工作树解包体积。给用户报「备份多大」时说清是哪一个。

## 1. 盘点：给真数，不给估计

按目录聚合，一次算清体积、文件数、每个档的会话数与分片数。要点：

- `.jsonl` 与 `.part-NNNN.jsonl` 要**合并计数**：一个 >12MB 的巨会话被拆成多片，直接数文件会把会话数数大。
- 目录口径：`home-overlay/`（主档 config/.env/auth/memories/skills/cron/scripts/kanban…）、
  `profiles-overlay/<档>/`（各档配置）、`sessions/<档>/`（各档会话）、`desktop-overlay/`、`tools/`、`supplement/`。
- **档必须成对看**：`profiles-overlay/<档>/` ＋ `sessions/<档>/`，只恢复一半会「配置在、历史没了」或反之。
- **别用 mtime 当「最后改动」**：clone 会把 mtime 刷成 checkout 时间。

输出形态（用户要的就是这张表）：

```
| ID | 单元 / 档 | 体积 | 会话数 | 一句话说明 |
```

## 2. 给菜单，不给流水账

每项一个 ID（`H1…` / `P1…`），附体积、会话数、说明，再给 2–3 个**套餐**：

| 套餐 | 内容 | 体积 | 取舍 |
|---|---|---|---|
| 精简 | 记忆 + 主档配置 + 主档会话 | 最小 | 其它档配置还在，要手工清 |
| 标准 | 全部对话历史，但 cron 先禁用、不动桌面端 | 中 | 最常用 |
| 全量 | 全部单元 + 桌面端 | 最大 | 恢复后必须立刻 `hermes pause` |

用户点 ID 或点套餐。**九步命令表是执行阶段的产物，不是决策界面**——先把它收起来。

## 3. 逐键合并（本文件的核心）

| 对象 | 做法 | 为什么 |
|---|---|---|
| `auth.json` | **默认跳过** | 凭据池（`providers` / `credential_pool`）。同一台机器重装后本机那份往往比备份新得多（实测 11.5KB vs 940B），覆盖＝丢刚登录的账号 |
| `.env` | **只补缺失键** | 共享键的值常常**完全一致**——逐个比一遍再下结论，别猜。只追加备份独有的键（如 `WEIXIN_*` / `QQ_*`），本机独有的保留 |
| `config.yaml` | **按 key 搬运** | 整份覆盖会把 `model.default` / `provider` / `base_url` / 记忆上限 / 工具集一起退回旧值。先跑 `scripts/compare-config.py` 出三张表，再逐条决定 |
| 被进程打开的 SQLite<br>（`shared-state.db` `kanban.db` `projects.db`） | **跳过，或停机后再补** | 网关持有句柄时 `cp` 覆盖有撕裂风险。`state.db` 本来就不在 overlay 里，会话走导入器 |
| `hooks.*.command` | **逐条 `test -f`** | 备份里存的是绝对路径，常指向 `workspace/temp/…` 这类临时目录；失效的话每个回合都会去跑一个不存在的脚本。改成技能目录内的副本 |

`config.yaml` 的判读经验（一份文件里三类混在一起）：

- **必搬**：`platforms.<平台>.enabled`、`sessions.max_export_messages`（=0 关上限，否则巨会话导出整包失败）、
  `memory.memory_char_limit` / `user_char_limit`、`model.default/provider/base_url/api_mode`、
  `agent.reasoning_effort` / `image_input_mode`、`display.show_reasoning`、`platform_toolsets.*`、
  `delegation.max_concurrent_children`、`terminal.cwd`、`security.fake_ip_ranges`
- **可跳**：`moa.*`（常是 `enabled: false`）、`known_*_toolsets`（内部记账）、`gateway.*` 的大批守护参数
  （新版往往已内建默认值，硬搬可能塞死键）、`fallback_providers: []`
- **本机新增的别删**：新版 schema 多出来的键（`agent.max_turns` / `compression.protect_last_n` /
  `delegation.max_iterations` / `terminal.timeout`…）保留本机值
- 判断某个键还活不活：**去源码里搜**（`grep -rn '<key>' --include='*.py' hermes-agent/`）。源码里有读取处
  才值得搬，否则是死键。

## 4. 执行顺序与暂停点

1. **保全**：本机 `config.yaml` / `.env` / `auth.json` / `state.db` 拷到 `~/restore-safe-<时间>/`
2. `hermes pause` —— 压住 cron/自动派发，验收完再 `hermes resume`
3. **文件层**：把选中的单元拷进 `~/restore-stage/{home-overlay,profiles-overlay}`（删掉不恢复的那几个文件），
   再 `python <HB>/tools/restore_files.py --repo ~/restore-stage --what home,profiles --create --dry-run`，
   看清清单再真跑。这样 clone 保持干净，官方工具的 `pre-restore-backup/` 也照常生效
4. **路径核对**：`check-paths.py --home <HERMES_HOME>`，rc=0 才算过
5. **会话层**：逐档 `restore_sessions.py --source <HB>/sessions/<档> --profile <档> --fast-import`（幂等、可续跑）
6. **路由**：`apply_routing.py`
7. **桌面端**（只在恢复 `desktop-overlay/` 时需要）：`start-finish-restore.ps1` ＋ **用户完全退出一次**
8. 验收 → 分批 `hermes resume`

**执行者本身跑在网关里**：第 7 步用户一退出，当前会话就断。所以收尾器必须是独立进程启动，且第 7 步之前的
每一步都要有自洽的验收输出——不能指望「断线后接着干」。

恢复 `platforms` 之后，**平台连接是全局单例、只挂主档**；其余 profile 的 `config.yaml` 通常没有 `platforms:` 段，
不会各自去连一个微信。别拿「17 个档」推「17 个 bot 同时上线」。

## 5. 回滚

- 文件层：`<HERMES_HOME>/restore-out/pre-restore-backup/`
- 三件套：第 1 步的 `~/restore-safe-<时间>/`
- 会话层：导入是**追加**、不改既有会话；动之前 `cp state.db state.db.bak-<时间>`

## 6. 恢复后必查

- 主 provider 出词（按恢复后的配置真跑一句）
- `sessions.max_export_messages` 是 `0`
- `hermes pairing list` 的 Approved Users 里有你的微信/QQ id（缺了＝消息收得到但被丢，见 §7.3.1）
- `hooks` 里每条命令的文件都存在
- 各档 `hermes -p <档> sessions stats` 与备份的会话数一致
- **盘点剩余缺口**：选择性恢复只搬了一部分，收工前用同一把尺子比一遍——技能按 `SKILL.md` 所在目录名比
  （本机 vs `home-overlay/skills`）、cron 数 `cron/jobs.json` 的 jobs、`HERMES_HOME/scripts/` 在不在、
  `profiles/` 档数、`sessions/main` 的唯一会话 id 数、`workspace/media` 这类不进备份的大件。
  把「已完成 / 还缺什么」做成一张表交回去；只报「恢复完成」而不报缺口，恢复就会停在一半没人知道。
- cron 是暂停态，按「备份同步 → 巡检类 → 交易/实盘类」的顺序分批放行

---

## 7. 恢复后的平台续接与「巨会话打不开」

### 7.1 导入器不写身份字段，必须回填

会话导进来之后，`messages` 行和 `sessions.source` 都在，但 `session_key` / `chat_id` / `chat_type` /
`origin_json` 可能是 **NULL**。没有 `session_key`，平台就认不出「这条微信/QQ 对话属于哪条旧线程」，
续接直接失效。

回填前先 `cp state.db state.db.bak-<时间>`，然后按来源 JSON 的**原值**写回：

```python
# 分片必须先按序拼成完整 JSON —— 单个 .part-0001.jsonl 只是片段，json.load 会直接抛错
fs = sorted(glob.glob(f'{sessions_dir}/{sid}.part-*.jsonl'))
d  = json.loads(b''.join(open(f, 'rb').read() for f in fs).decode('utf-8'))
c.execute('update sessions set session_key=?, chat_id=?, chat_type=?, thread_id=?, origin_json=? where id=?',
          (d.get('session_key'), d.get('chat_id'), d.get('chat_type'),
           d.get('thread_id'), d.get('origin_json'), sid))
```

回填后核：`select id, session_key from sessions where source in ('weixin','qqbot')`，并跑一次
`pragma quick_check`。判据一句话：**导入成功 ≠ 能续接**，续接看 `session_key` 在不在。

### 7.2 有 `session_key` 就不必停网关写 routing

`hermes-agent/hermes_state_gateway.py` 的 `find_latest_gateway_session_for_peer` 会**用行上的 `session_key` 重建缺失的
`session.json` 映射**。所以 `apply_routing.py` 可以整步跳过——而它本来要先停网关（网关按内存快照
`DELETE`+重插 routing 行，手写的行会被抹掉）。少一次停网关，就少一次断线风险。

验证方式：`grep -rn find_latest_gateway_session_for_peer hermes-agent/` 看它的 docstring 还在不在讲这件事；
行为变了就恢复「停网关 → apply_routing」的原顺序。

### 7.3 通道真正激活要重启网关

`.env` 与 `platforms.*` 都是**启动时读一次**，文件改完当前进程不会认。验收只看两处：

- `gateway_state.json` 的 `platforms.weixin / qqbot` 出现且 `state == connected`
- `logs/gateway.log` 里的 `✓ weixin connected` / `QQBot … Ready`

两个容易踩的语义：`WEIXIN_GROUP_POLICY=disabled` 说明**微信群聊根本不是通道**，只有私聊，按这个前提设计不要按「群里也能收」。
QQ 的群策略不在 `.env`，在 `platforms.qqbot.extra.group_policy`。

### 7.3.1 `connected` ≠ 收得到：配对白名单是另一个目录

`dm_policy` 是 `pairing`（weixin / qqbot 的代码默认值）时，放行名单**既不在 `.env` 也不在 `config.yaml`**，
而在 `<HERMES_HOME>/platforms/pairing/<platform>-approved.json`。`restore_files.py --what home,profiles`
按白名单采集，`platforms/` 这种运行期目录常常**整个都进不了恢复范围**。缺了这一层的症状极容易误判：

```
[Weixin] inbound from=<id> …                                  ← 消息其实收到了
WARNING gateway.run: Unauthorized user: <id> on weixin        ← 被策略判未授权，直接丢掉
gateway_state.json → weixin: connected                        ← 与此同时通道显示「已连接」
（不产生任何新会话，桌面端看不到任何变化）
```

所以**判通道通不通看日志里有没有 `Unauthorized user`，不看 `connected`**（启动时那句
`No env user allowlists configured … default to pairing/allowlist policies` 就是同一个提示）。修法：

```bash
mkdir -p "$HERMES_HOME/platforms/pairing"
cp -f <REPO>/home-overlay/platforms/pairing/*.json "$HERMES_HOME/platforms/pairing/"
hermes pairing list      # 期望 Approved Users 里出现对应 openid / QQ id
```

`*-approved.json` 是放行表（`{<id>: {user_name, approved_at}}`），`*-pending.json` 通常是 `{}`，`_rate_limits.json` 可不搬。
**不用重启网关**：`gateway/pairing.py` 的 `_approved_cache` 以**文件签名**（dev+mtime+size）为键，写入后下一条
inbound 就读到新值。验收终点＝平台发一句能得到回复并生成会话，`hermes pairing list` 只是中间证据。

### 7.4 巨会话「能看到尾巴、往上翻加载不出来」

这是恢复巨会话之后最容易被误判成「版本 bug」的现象。**先量三层，再决定动不动版本**：

**第 0 步永远是看真实界面，别先读源码猜。** `computer_use` 的 `capture`（后台投递，不抢焦点、不弹窗）
把窗口截下来，一眼分清三种情况：①根本打不开/一片空 ②打开了但往上翻不动 ③打开了且正常。截图时认准右下角：
**圆形黑方块＝停止按钮（这一轮还在跑），不是加载失败**——配合状态栏 `Running mm:ss` 和会话行的
`last_activity_description` 判断是真在干活还是真卡死。源码路径要读，但只用它来**解释已经看到的现象**，
不能用它替代观察：先读几十分钟代码再回来截图，往往发现界面根本不是自己设想的那个状态。

| 要量的 | 怎么量 | 常见结论 |
|---|---|---|
| 行数与文本量 | `select active, count(*), sum(length(content)) … group by active` | active 只有几百条、`active=0` 的归档上万条 |
| 后端分页 | 直接调 `db.get_messages(limit=120, offset=N*120, latest=True, include_compacted=True, include_ancestors=True)` | 每页 0.0x 秒、无空页 ⇒ 后端清白 |
| resume 闸门 | `db.assert_resume_safe(sid)`；键 `sessions.max_resume_messages`（默认 20000，`0` 关闭） | 没超限就不该报「too long to reload」 |

机制（源码级）：桌面端打开会话走 **WS `session.resume`**，返回的是**显示投影**——
`_live_visible_history` 用 `include_compacted=True`、`include_row_ids=True`、**无 LIMIT**，
上万条归档行一次性推过去（实测 43–73MB）；而「还有更早的、可以继续翻」这个状态
（`recordTranscriptTail` / `possiblyTruncated`）**只在 REST `getSessionMessages` 里记录**。
于是 WS 打开的会话翻页状态为空，`backfillOlderTranscriptPage` 第一行就 `return false`——
往上滚连请求都不发。**判据：`transcript-tail.ts` / `transcript-backfill.ts` 在两个版本之间一行没改，
所以「旧版本能用」不等于「新版本退化」，别拿降级当修法。**

**可逆处置（不动官方代码）＝把 `active=0` 的归档行迁到独立「归档会话」**：`scripts/split-archive-session.py`
（`--session <id>` 可重复、不给 `--apply` 就是 dry-run；归档会话 `session_key` 写 NULL，`messages` 的触发器
不盯 `session_id` 所以 UPDATE 不触发 display 重排）。实测载荷 43.2MB / 73.3MB → 各 **1.3MB**，旧行一条不删。

**但这只是「体量」那一半，不要把它当「UI 修好了」。** 实测同一轮里：拆完、重启桌面端，用户报现象不变。
两条必须分开验收：

| 验什么 | 怎么验 | 过关标志 |
|---|---|---|
| 显示投影变小了 | `db.get_messages_as_conversation(sid, include_ancestors=True, include_row_ids=True, include_compacted=True)` | 条数与 JSON 体积降一个量级 |
| UI 真的能往上翻 | 读 `%HERMES_HOME%\logs\desktop.log` 里 `[hermes:api GET /api/sessions/<id>/messages?...]` 行 | 出现 `offset>0` 的请求才算翻页真的发生了 |

`desktop.log` 是这条线最好用的**被动仪表**：它记桌面端实际发了哪些 REST 请求。实测巨会话打开时
**一次 `/messages` 都没有**（只有 WS `session.resume`），说明数据全从 WS 那份投影来——那么
「翻不动」只可能是渲染层或 tail 状态，跟后端分页、跟版本都无关，别再去降级。

翻旧账的出口（不受 UI 限制）：

```bash
hermes sessions export --format html --session-id <id>_hist <输出文件.html>   # OUTPUT 是位置参数，没有 --output
hermes sessions export --format md   --session-id <id>_hist <目录>            # md/qmd 收的是目录，一会话一文件
```

迁移前备份 `state.db`（脚本 `--apply` 会自动备），回滚＝把 `session_id` 改回去。

探针一律写成独立 `.py` 交给 `run_in_runtime.py` 跑（运行时 venv 才有依赖），只读、不写库。

### 7.5 旧线程彻底不要了：解绑 ＋ 删除，让下一条消息开新会话

用户说「旧的不看了，按现在的项目重新开始」时，比修 UI 便宜得多的路是**解绑**：平台只靠
`sessions.session_key` 找旧会话，把这一列置空，下一条 inbound 就会**自动建一条全新会话**。

```python
c.execute('update sessions set session_key=NULL, archived=1, title=? where id=?', (title, sid))
```

绑定**只在这一列**：`gateway_routing` 里通常没有这几条 key（实测那 6 行全是本地会话），
`channel_directory.json` 只是聊天 id 名册、不含会话映射——别去那两处找，改 `sessions.session_key` 就是全部。

再 `hermes sessions delete <sid> --yes` 逐条删（`split-archive-session.py` 产生的 `<id>_hist` 一起删）。
删前把要留的导出成 HTML（§7.4 的 `hermes sessions export --format html`），并把 `state.db` 备份一份。

三条判据（缺一条就是没做干净）：

```sql
select count(*) from sessions where session_key like 'agent:main:weixin:%' or session_key like 'agent:main:qqbot:%';  -- 0
select count(*) from messages where session_id in (…被删的 id 们…);                                                      -- 0
pragma quick_check;                                                                                                   -- ok
```

**说清边界**：这只是「不再续接旧线程」，**不等于通道已经能通**。通道能不能通要单独验（§7.3 / §7.3.1：
`connected` + 无 `Unauthorized user` + 下一条消息真建出会话）。把「删干净」当成「修好了」会漏掉配对/网关那层。

---

## 8. 只挑一批「会话」恢复（旧机快照 → 新机）

场景：用户只认某几条对话（典型：标题带 `@` 的项目会话），bot / 群聊 / 定时任务的记录一律不要。

### 8.1 先验源覆盖度，再决定用哪个源

备份仓的 `sessions/<档>/` 是**某一轮导出的产物，可能残缺**；本地留着的旧 `state.db` 反而常常更全。
先把「用户点名的那批」在候选源里逐条对上号（数量 + 标题 + 消息数），再谈恢复：

```bash
# 逐会话读头部那行（分片先按 .part-NNNN 归组成一条）
git -C <REPO> ls-tree -r HEAD sessions/main | awk '{print $3}' \
  | python -c "…git cat-file --batch 逐条读、抓 id/title/message_count/source/cwd…"
```

实测坑：镜像导出里 desktop 会话的 `title` **大量为空**，只有少数几条带标题 ⇒ 按标题过滤会漏掉绝大部分；
同机留下的旧库（schema 与目标版本相同）标题齐全、目标会话一条不少。实测那一例：镜像 2,202 个会话里
只命中 4 个目标 `@` 会话，其余 13 个**根本不在**。
⇒ **选源判据＝目标会话按 id 逐个命中 ＋ 标题完整 ＋ 表结构一致**，不是「谁更新」——
旧库可能早一天，但它是原生的；镜像新一天却缺会话。

另：`schema_version` 那个数字**不能当「原生」的判据**——它可能被人为改回旧值（把 32 改回 31 是常见收尾动作），
而表里仍带着更新版本才有的列与守卫触发器。判结构一律比 `pragma table_info` 与 `sqlite_master` 的集合。

### 8.2 挑哪一批：三层过滤

1. **标题约定**：`title LIKE '@%'`（前缀匹配）。用包含匹配会把 `Subagent: … pkg@1.2` 这类误捞进来。
2. **状态位**：排除 `archived=1`（归档＝用户已经不要）；`pinned=1` 随行自动带回，不用单独处理。
3. **子代理闭包**：工具调用里存着 `parent_session_id` 引用，桌面端能点进去；只恢复父会话 ⇒ 这些引用全变死链。
   取传递闭包（含孙代）：
   ```python
   keep = set(parents); frontier = set(parents)
   while frontier:
       q = ",".join("?" * len(frontier))
       kids = {r[0] for r in c.execute(f"select id from sessions where parent_session_id in ({q})", tuple(frontier))}
       frontier = kids - keep; keep |= kids
   ```
   实测一例：14 条父会话带出 **1,506 条**子代理会话（父 1.3 万条消息 vs 闭包 16.5 万条）——
   **先把这个倍数报给用户**再让他决定，别默默搬。

### 8.3 会话里引用的图在另一棵树里

消息正文存的是绝对路径（`%APPDATA%\Hermes\composer-images\image_xxx.png`），文件**不在 `sessions/` 里**，
而在 `desktop-overlay/composer-images/`。只搬会话 ⇒ 历史全成裂图。
做法：**整目录全拷**（几百 MB，几秒；同名不覆盖），不要按引用清单挑——同一批会话用不同正则写法会
算出相差一倍的引用数（实测 126–245 个），按清单挑必漏，而多拷几十张的代价是零。
判据一句话：**文字完整 ≠ 对话完整**，带图的会话要连图一起搬；源里本来就没有的，如实报数。

### 8.4 平台会话能不能续写：先比 session_key 与配对 id

`@weixin` / `@qq` 这类会话恢复后能否继续吃新消息，只看 `sessions.session_key` 里的对端 id 与
`<HERMES_HOME>/platforms/pairing/<平台>-approved.json` 的键是否一致：

```sql
select id, session_key, chat_id from sessions where title in ('@weixin','@qq');
-- agent:main:weixin:dm:<openid> …
```
```bash
cat "$HERMES_HOME/platforms/pairing/weixin-approved.json"   # 键里应含同一个 <openid>
```

一致 ⇒ 恢复后手机发一条就落回旧会话（这正是用户要的「联通后再恢复、恢复后能续写」）；
不一致（换号 / 重新配对）⇒ 老会话永远收不到新消息，**先如实说明再动手**，别承诺「恢复完就能接着聊」。

### 8.5 落库：就地合并，不要换库

**先否掉「离线裁剪副本 → 换 state.db」这条路**（本文件上一版就是这么写的，实测被并行审稿否掉）：

- 换库要停全部持库者。`hermes_state_holders.foreign_state_db_holders(<库>)` 实测至少两个：桌面后端的
  `hermes serve` 与网关——**而执行者自己就跑在 `hermes serve` 的进程树里** ⇒ 停它＝杀掉执行者，
  换库后半程（起服务、验收、回滚）没人做。
- Windows 上 SQLite 不以 `FILE_SHARE_DELETE` 打开，rename/覆盖必 `sharing violation`；即使绕过，
  源码有 `StateDbReplacedError` / `DeletedWalGenerationError` 会拒绝继续写（split-brain）。
- 换库换不来「更纯」：备份库哪怕 `schema_version` 写着旧值，也可能带着更新版本才有的列与守卫触发器
  （历史上有人手工把 32 改回 31）⇒ 判「原生」要看表/列/触发器集合，别信那个数字。

**正确做法＝把源库 ATTACH 进来，只向活库 INSERT 目标会话**（不停服务、不动文件、回滚＝删插入的行）：

```python
con = sqlite3.connect(dst, uri=True, isolation_level=None)          # 活库
con.execute("PRAGMA busy_timeout=60000"); con.execute("PRAGMA foreign_keys=ON")
con.execute("ATTACH DATABASE ? AS bak", (f"file:{src}?mode=ro",))  # 源库只读
```

五条硬约束，缺一条就报错或写坏：

1. **显式列名**：两库列集合可能完全相同、但**物理顺序不同**（新版本把若干列前移）。`INSERT … SELECT *`
   会静默错列，`_reconcile_columns()` 只加列、救不了错位。
2. **`messages` 不带 `id`**：那是 `INTEGER PRIMARY KEY AUTOINCREMENT`，源库已用到几十万，带过来必撞主键。
   让 SQLite 重新发号，其余列（`message_uid` / `display_order` / `display_identity`）原样保留——
   display_order 留 NULL 时插入触发器会按新 id 重写，只影响不可见行，但没必要。
3. **先插 `system_prompts`，再插 `sessions`**：`sessions.system_prompt_hash` 有外键指向
   `system_prompts(hash)`；源库里被引用的 hash 目标库通常一条都没有（实测 81 个全缺）。
4. **父会话先于子会话**：`sessions.parent_session_id` 自引用外键，按「根在前」排序再插。
5. **`sessions.title` 有唯一部分索引**（`WHERE title IS NOT NULL`）：插入前按**本次要插的那些 id**
   查重名——别对整批查，续跑时会把已经插进去的自己当成冲突。

**锁窗口必须切碎。** Hermes 的写连接是 `timeout=1.0`（拿不到锁就失败、应用层重试），一个会话一个事务时
最大的那条有七万多行、锁两分半，直接把正在跑的对话掐断。同一份 35 万行数据的三次对照：

| 做法 | 总耗时 | 最长写锁 |
|---|---|---|
| 抑制 FTS 触发器 + 末尾整表 rebuild | ~3 分钟 | **~2 分钟**（rebuild 单语句全程持锁） |
| 整会话一个事务 | 3 分 14 秒 | **~150 秒** |
| **3000 行/事务 ＋ 断点续跑** | **~3 分钟** | **~3 秒** ✅ |

断点续跑靠一张进度表：`CREATE TABLE zz_merge_progress(session_id TEXT PRIMARY KEY, last_src_id INTEGER)`，
每块插完写回块内最大源 id，重跑时 `WHERE id > last_src_id` 接着来；**收尾 `DROP TABLE`**。
后台跑必须带 `persist_on_release`（普通后台进程随回合结束被杀）。

**验收 SQL 套件**（每次合并后全跑，任一不过就是没成功）：

```sql
select count(*) from sessions;                        -- 目标数 = 恢复数 + 已有数
select count(*) from messages;                        -- 行数（含 active=0 的归档行）
select count(*) from messages where active=1;         -- 可见条数
select count(*) from messages_fts;                    -- 必须等于 messages 行数
select count(*) from messages_fts_trigram;            -- 子集（只索引非 tool、非 cron/subagent）
pragma foreign_key_check;                             -- 必须 0 行
pragma quick_check;                                   -- ok
select count(*) from messages m where not exists(select 1 from sessions s where s.id=m.session_id);  -- 0
select count(*) from sessions s where s.message_count <> (select count(*) from messages m where m.session_id=s.id and m.active=1);  -- 0
```

- **`hermes sessions stats` 的 `Total messages` 是 `count(*)`（含归档行），不是 `message_count`**；
  `sessions.message_count` 只算 `active=1`，两个数能差一倍以上（实测 354,776 行 / 165,199 可见）。
  验收口径写错会把成功判成失败。
- **trigram 搜索的查询词必须 ≥3 个字**（`分镜头` 命中、`分镜` 恒 0）——拿两字词验 trigram 会误判成索引坏了。

### 8.6 平台路由要回指，不只是 `session_key`

活库的 `gateway_routing` 可能已经有一行同 `session_key` 的记录、却指向**一条已不存在的会话**
（平台重连时建过又删掉的），光靠 `session_key` 不会自愈到旧会话。合并后按 `session_key` 把
`entry_json.session_id` 改成恢复出来的那条，并清掉重置痕迹：

```python
d = json.loads(entry_json)
d.update(session_id=new_sid, prev_session_id=None, is_fresh_reset=False,
         reset_had_activity=False, resume_pending=False, resume_reason=None,
         was_auto_reset=False, updated_at=time.time())
```

判据：`json_extract(entry_json,'$.session_id')` 能在 `sessions` 里查到；再让手机发一条，看落点是不是旧会话。

### 8.7 收尾：路径、删除闸与两个上限

- **`cwd` 与 `git_repo_root` 一起改**：旧机的 `D:\workspace` 在新机不存在，只改 `cwd` 会让 git 上下文指向死盘。
- **删会话可能被守卫触发器拦住**：`sessions_runtime_ledger_delete_guard` 对 `session_admissions` /
  `worker_executions` 里有行的会话 `RAISE(ABORT, '… Downgrading is unsupported …')`。要删就先把那两张表里
  该会话的行删掉（先备份库），再 `hermes sessions delete <id> --yes`；删完 `quick_check` 复核。
- **`sessions.max_resume_messages` 默认 20000**（按 `active+compacted` 的可显示行算）：超了就打不开，
  报「too long to reload」。恢复完先量哪些超了（命令行侧用 `SessionDB.assert_resume_safe(sid)` 离线判定），
  再决定要不要 `hermes config set sessions.max_resume_messages 0`（改配置 ⇒ 需授权）。

源库全程只读、活库先按 `<库>.bak-pre-merge-<时间>` 留一致性快照（用 `sqlite3.Connection.backup()`，
不要 `cp`——WAL 里的提交会丢）。失败回滚＝删掉插入的那批 session id。


