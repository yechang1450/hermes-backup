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

