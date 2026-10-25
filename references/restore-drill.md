# 恢复侧验收演练（drill）— 配方与判读

> 何时必须跑：改动恢复链任何一环（restore_files / restore_sessions / concat_sessions / apply_routing / 存储格式 / run_in_runtime）之后；重大收口或用户要求「证明真能恢复」时。
> 验收标准：**至少两遍独立全量演练、两遍都 0 差异**。单遍不足以背书——第二遍要换仓状态、换工具来源，排除「凑巧跑通」。

## 两遍的独立性设计

| | 第 1 遍 | 第 2 遍 |
|---|---|---|
| 仓状态 | 当时的镜像快照 | 最新的仓（含最新一轮同步的数据＋刷新后的 tools/） |
| 工具来源 | 技能目录 `scripts/` | **克隆仓自带的 `tools/`**（真实新机流程，严格照 `templates/RESTORE.md` 步骤） |
| 附加 | 基本流程 | ＋拼包通道（桌面「导入」的数据通道）实测 |

## 步骤（全部在 temp 沙箱，不碰真机）

1. `git clone ssh://git@ssh.github.com:443/<owner>/hermes-backup.git <BASE>/repo` —— 新机视角必须走**网络克隆**；优先官方备用端口 443（22 端口在代理/fake-ip 窗口易被掐断，clone 卡死先换端口验证）。
2. 文件层：`python <TOOLS>/restore_files.py --repo <BASE>/repo --home <BASE>/home --no-backup`
3. 会话层：`python <TOOLS>/run_in_runtime.py <TOOLS>/restore_sessions.py --all --repo <BASE>/repo --home <BASE>/home`
   完成标准：`imported=N skipped=0 failed=0`，N 应等于镜像侧会话组数。
4. 路由：`python <TOOLS>/apply_routing.py --home <BASE>/home --repo <BASE>/repo`（期望 `applied: new=K`）。
5. 官方读回：`run_in_runtime` 跑读回脚本（逐档 sessions/messages 计数 ＋ 关键件在位 ＋ 置顶清单）。
6. **逐会话对账（核心验收）**：镜像 `sessions/<档>/` 的每个会话（`.part-*` 按名序拼接）`json.loads` 后数 `messages` 长度 ⇔ 沙箱 DB `SELECT COUNT(*) FROM messages GROUP BY session_id` —— 要求**逐会话相等、差异 0 项**，不是只比总数。现成实现：`python <SKILL>/scripts/verify_drill.py <BASE>`（输出逐档行＋差异清单，末行 `VERIFY-DONE`）。
7. 拼包通道（第 2 遍加跑）：`python <TOOLS>/concat_sessions.py main --batch-mb 250 --out <BASE>/packs` → **行数（行=会话）**必须等于克隆内该档会话组数。
8. 三向差集（推荐）：真机 vs 快照——真机∩快照应精确一致；真机−快照＝同步后新建（预期存在）；快照−真机＝曾删除（快照保留历史）。报告时先讲这层边界，避免把预期差异当故障。
9. 清理：删沙箱（一次演练 6–8GB 级）；保留演练脚本＋对账输出＋回执于 temp。

## 冷启动冒烟（新机视角，15 分钟级；改文档或恢复链后先跑这个）

比「两遍全量演练」便宜一个数量级，但它证明的是最关键的一件事：**一台没有本机状态的机器，能不能只靠 GitHub 上的仓恢复**。

1. 全新克隆（不要复用现有镜像）：`GIT_SSH_COMMAND="ssh -o ServerAliveInterval=20 -o ServerAliveCountMax=30" git clone --depth 1 <url> <BASE>/data`
2. 用**克隆自带的**工具恢复一个中等档（不要用技能目录的 `scripts/`——那会掩盖「仓内副本缺失／文档路径写错」这类缺陷）：
   `python <BASE>/data/tools/run_in_runtime.py <BASE>/data/tools/restore_sessions.py --source <BASE>/data/sessions --profile <档> --home <BASE>/home --fast-import --verify`
3. **独立核对**：别只信脚本自报，直接读库（`sqlite3 <home>/profiles/<档>/state.db`：`sessions`/`messages` 计数、`PRAGMA integrity_check`、`foreign_key_check`），再与镜像逐会话比。
4. 幂等复跑一次：期望 `imported=0 skipped=N failed=0`。

**实测基准（3.3GB 树）**：`--depth 1` 克隆 **393s / 落地 4.5GB**（工作树 3.3 + `.git` 1.2）；恢复一个 136 会话 / 27,249 条的档 **29s**，`verify_failed=0`、`integrity_check=ok`、幂等复跑 `imported=0/skipped=136`。

**为什么必须做、且值得派子代理**：本机是「热」环境——镜像、技能目录、`tools/` 副本全在，`<REPO>/tools/…` 与 `<SKILL>/scripts/…` 混着敲也不报错，所以**文档里的路径错误在本机永远暴露不出来**。冷启动克隆里只有仓内那一份文件，照文档跑一次就当场炸（实案：文档让人跑 `<REPO>/tools/`，技能仓却只有 `scripts/`——本机从未触发，克隆后一跑即 `No such file`）。派给子代理还能拿到第三方视角的读数，避免自己验自己。

## 低成本冒烟（官方版本变动 / 改恢复工具后先跑这个，1–2 分钟）

全量演练要 30–90 分钟、几 GB，日常不值得；但它掩盖的风险（**官方导入 API / schema 变了**）用一个 5 分钟冒烟就能拿到证据。不碰真机，全在 `temp` 下的假 home 里跑。

**例 A：小档整档恢复**
```bash
python <REPO>/tools/run_in_runtime.py <REPO>/tools/restore_sessions.py \
  --repo <REPO> --home <BASE>/home --profile <小档> --fast-import
```
判据：`imported=N failed=0`，且沙箱 `profiles/<档>/state.db` 的 `sessions`/`messages` 计数 **== 镜像侧该档计数**（不拿活库当基准：活库比快照新，差数＝上轮后增量，属正常）。

**例 B：一个被切片的巨会话**（验证 `.part-NNNN` 拼接＋导入）
```bash
mkdir -p <BASE>/src/<profile> && cp <REPO>/sessions/<profile>/<sid>.part-*.jsonl <BASE>/src/<profile>/
python <REPO>/tools/run_in_runtime.py <REPO>/tools/restore_sessions.py \
  --home <BASE>/home2 --profile <profile> --source <BASE>/src --fast-import
```
判据：恢复条数 == 分片按名序拼接后 `json.loads()['messages']` 的长度；摘要行应含 `触发器恢复 10 个 / uid 空值 0/N`（`uid` 空值必须为 0）。

**坑与基准**
- `--source` 指的是**包含 `<profile>/` 的父目录**，不是档目录本体——传档目录会报 `源目录不存在: …/src/main/main`（工具自己再拼一层档名）。
- 沙箱 home 不用预建完整骨架；`--home <空目录>` 即可，恢复器自己建库。
- 量级参考：575 条消息 ≈ 0.1s；21,600 条巨会话（两片）≈ 17s（FTS 重建占 14s）；**外层看到的墙钟还要加 `run_in_runtime` 启动 15–20s**，别把启动开销记成恢复变慢。
- 跑完删沙箱（一个巨会话冒烟约 200MB）；回执里写清「例 A／例 B 各多少条、与镜像差多少」。

## 判读坑（每条都实际踩过）

- **空会话假阳性**：0 消息的会话不会出现在 messages 分组里——判「未导入」必须同时查 `sessions` 表（id 在会话表里即已导入，消息数按 0 比对）。只看 messages 分组会把它误报为缺件。
- **分片不是独立会话**：`.part-NNNN.jsonl` 按 `.part-` 前缀分组、按名序拼接后解析；计数口径是「会话组数」，不是文件数。
- **导出文件是单行巨 JSON**：行数≠消息数；对账用 `json.loads` 后的 `messages` 长度，别用 `wc -l`。
- **导入中间态**：会话按档导入、逐档落库——验收只认全部结束后的读数，中途查库必然「少」。
- **验收以「最后同步时刻」为界**：快照之后的新会话不在备份里（下轮同步带上）；向用户解释时把三向差集讲成「预期边界」而不是故障。
- **时间锚**：长演练前后各 `date` 一次；报告里「最新/刚才」按实测时间重算，别沿用会话内的旧时间感。

## 基准与产物

- 同一恢复流程独立两遍全绿（最近一次）：715 会话/169,955 消息 与 752 会话/174,834 消息，**均 0 差异**；拼包 4 包 / 261 行＝会话数。数量级对不上先怀疑仓状态不同步，不要怀疑对账方法。
- 演练脚本与对账输出：`temp\hb-drill-20261018\`（drill.sh / drill2.sh / verify.py / verify2.py 及各输出）。temp 会被周期清理——复跑按本配方重建即可（对账器逻辑即第 6 步，约 40 行 python）。
