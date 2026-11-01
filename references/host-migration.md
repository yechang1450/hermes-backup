# 主机路径迁移：适配与「是否真完成」的六路审计

适用：宿主把工作区 / 镜像换盘、换目录或换用户名之后。目标是两句话——**链路指到新址**、**数据一件没少**。

## A. 差异怎么读（先学会判读，否则会把正常淘汰当丢失）

新旧镜像逐文件对比后，差异只可能落在四类，只有最后一类要查：

| 差异 | 判据 | 结论 |
|---|---|---|
| `*/Local Storage/leveldb/*.ldb|*.log` | 桌面前端一直在轮转 | 正常，跟随新树 |
| `home-overlay/cron/output/<job>/<时间>.md` | cron 输出有保留期 | 正常淘汰 |
| `<sid>.jsonl` 变成 `<sid>.part-000N.jsonl` | `ls sessions/<档> \| grep <sid>` 能看到分片 | **不是丢失**，是超 12MB 自动分片 |
| 会话/文件在活库仍存在、镜像里没有 | 逐条回活库核 | 真漏备，必须查 |

判据一句话：镜像按设计**跟随活库当前状态**（删了会 prune），所以「少了」本身不是问题，**「活库还有、镜像没有」**才是。

## B. 六路审计（每路都要有可复现的命令与判据）

1. **数据完整性**：两边各出清单再比集合
   ```bash
   git -C <新镜像> ls-tree -r --name-only HEAD | sort > new.txt
   git -C <旧镜像> ls-tree -r --name-only HEAD | sort > old.txt
   comm -23 old.txt new.txt | head        # 旧有新无 = 待判读
   comm -13 old.txt new.txt | wc -l       # 新增件
   ```
   同时核 `git rev-parse --short HEAD` 与 `git ls-remote origin refs/heads/master` 是否一致。
2. **引用完整性**：活引用必须为零（`.bak-*` 历史备份不算）
   ```bash
   grep -n 'D:\\\\\|D:/' <HERMES_HOME>/config.yaml <HERMES_HOME>/cron/jobs.json
   grep -rln 'D:/\|D:\\\\' <HERMES_HOME>/scripts/ <技能>/scripts/
   ```
3. **跨盘残留**（决定「旧盘能不能拆」）：仓里 `.git` 是**文件**的＝worktree/子模块指针，可能仍指向旧盘
   ```bash
   for g in <工作区>/build/*/.git; do [ -f "$g" ] && echo "$g -> $(head -1 "$g")"; done
   grep -rl 'D:\\\\\|D:/' <工作区>/build/*/.git/config
   ```
4. **运行配置**：`local.json` 的 `mirror` / `repo`、镜像的 `git remote -v`（应为官方备用端口 `ssh://git@ssh.github.com:443/…`）、`<HERMES_HOME>/scripts/*.py` 必须是**转调壳**而不是副本（副本会分叉）。
5. **旧副本停写**（双写是本类迁移最大的隐患）：旧镜像 `HEAD` 与 `git reflog -1 --date=iso` 的时间戳**静止**，而新镜像同期推进了轮次 ⇒ 停写成立。只看一次 HEAD 不算证据。
6. **恢复演练**（唯一能证明「迁移后还能恢复」的一步）：全新 `--depth 1` 克隆 → 只恢复**一个小档**（不要 main）→ 用 `sqlite3` 直读沙箱库核 `sessions` / `messages` 行数与 `message_uid` 空值。**非 main 档的库落在 `<沙箱>/profiles/<档>/state.db`**，`<沙箱>/state.db` 只是空壳（没有 `sessions` 表，开它必报 `no such table`，会被误判成恢复失败）。命令形状：`run_in_runtime.py <绝对路径>/restore_sessions.py --repo <克隆> --profile <档> --home <沙箱>`——`--create` 只有 `restore_files.py` 有（给 `restore_sessions.py` 会 rc=2），`run_in_runtime.py` 的脚本参数必须是**绝对路径**（相对路径按 cwd 解析，会 `FileNotFoundError`）。适合派子代理跑，但跑完要自己再开一次库核那四个数——子代理的自报是自述不是证据。

### 性能归因：先排除并发，再怀疑磁盘

换址后「一轮变慢」最常见的不是磁盘慢，而是**测的时候旁边有重活**。三条判据：

1. 挑干净窗口重测（无同步、无自检、无克隆），那才是基线；同一天的干净轮 vs 污染轮可以差 5 倍（实测 485–496s ↔ 1605–2697s）。
2. 小文件吞吐比单文件顺序写更贴近真实负载：`3000 × 4KB` 写入，新旧盘通常打平（实测 2,227 vs 1,954 文件/秒）——本流程写的是几万个小文件，不是几个大文件。
3. 只有「干净轮仍显著更慢」才归因磁盘，且只能拿**同口径的干净轮**对照；不要用被污染的样本去改超时预算，更不要把错误归因写进 CHANGELOG 或技能。

## C. 轻探针（别在几十万文件的树上用 `du`）

`du -sh` 在迁移后的树上会撞工具 420s 超时，而且超时不留部分结果。用 git plumbing，秒级：

```bash
git ls-files | wc -l                                   # 文件数
git ls-tree -r -l HEAD | awk '{s+=$4} END {printf "%.2fGB", s/1073741824}'   # 树实量
git count-objects -vH | grep -E '^count|^size-pack'     # 对象数 / 本地包体积
gh api repos/<owner>/<repo> --jq .size                  # 远端体积（KB）
```

体积判读：`.size ≈ 树` 时说明历史开销已被压缩抵平——**换代不会缩小任何东西**，此路判据见 `generations.md`。

## D. 收尾判据（三条同时成立才算迁移完成）

1. 一轮真同步：`mirror=<新路径>` + `committed=True`；纯本机模式下 `pushed=None` 是正常值，只有 `--push` 的轮次才要求 `pushed=True` 且 `ls-remote` == 本地 HEAD。
2. 一轮真恢复：小档会话数/消息数与镜像一致、`message_uid` 无空值。
3. 旧副本 HEAD 与 reflog 时间戳在观察窗内不动（新镜像已推进 ≥1 轮）。

缺第 3 条＝可能双写；缺第 2 条＝只证明了「能同步」，没证明「能恢复」。

## E. 点火前三项体检（目录都在 ≠ 链路通）

镜像、技能、脚本三样都在，也可能照样跑不起来。按顺序核这三件，每件都有过实案：

1. **`local.json` 指的镜像必须存在、且是 git 仓**。它可能还写着上一轮「换代」中间态的名字（`~/hermes-backup-<型号>` 之类），而那个目录要么不存在，要么只是一个**没有 `.git` 的半成品拷贝**（体积约为真镜像的 1/7）。判据：
   ```bash
   python -c "import json,pathlib;d=json.load(open(r'<技能>/local.json'));print(d['mirror'], pathlib.Path(d['mirror'],'.git').exists(), d['repo'])"
   ```
   半成品目录不要就地「补 git init」——直接指回真正在用的那一代（镜像已有 origin 时仓名以 origin 为准）。
2. **仓名必须在 GitHub 上真实存在**：`gh repo list <owner> --json name,visibility --jq '.[]|select(.name|test("hermes-backup"))'`。`repo` 写着上次尝试留下的名字时，`--create-remote` 会去建一个**新的空仓**，等于把历史切断。
3. **cron 任务「在册」≠「在跑」**：`jobs.json` 里两条任务都在、`script` 也在，但 `enabled: false` 时 ticker 根本不点火（脚本、镜像、脚本副本全绿，就是没人调度）。判据不是 `cron list` 的 Last Run，而是**该 job_id 在 `executions.db` 的最后一行时间 vs 现在**：
   ```bash
   sqlite3 "<HERMES_HOME>/cron/executions.db" \
     "select job_id,status,started_at from executions where job_id='<id>' order by rowid desc limit 3"
   ```
   最后一条是几分钟前＝在跳；几小时／几天前＝已停。修完先手工点一轮，再用**新一轮** `executions.db` 行作证（脚本能跑、`cron run` 成功都不等于 ticker 在工作）。

点火前的活库侧预检（避免撞保护闸白跑一轮）：活库会话数 vs `sessions/main/*.jsonl` 数——活库 < 备份 50% 会触发 shrink guard `exit 6`；新机 `profiles/` 目录**存在但为空**属正常，退役档靠 `local.json` 的 `preserve_sessions` 兜住，不要因此加 `--allow-shrink`。

## F. 扫「还有谁写死旧盘」：逐仓 `git grep`，别用 `rg` 扫整棵树

换地址后最耗时的一步不是搬数据，是找出还有谁写死旧路径。两种做法实测差两个数量级：

| 做法 | 实测 |
|---|---|
| `rg '<旧盘>' <整个工作区>` | 在刚迁移完、文件缓存全冷的盘上**跑不完**：撞 300s 工具超时（连执行内核一起被杀），或让子代理卡在 442s 只发出 1 个调用 |
| 每仓一次 `git grep -nI -e ...` | 18 个仓 **74 秒**跑完，最慢单仓 0.4 秒 |

要点：①只扫**已跟踪文件**（`git grep` 天生如此，未跟踪的本来不算账）；②**单仓 60 秒超时**，超时记「未扫完」继续下一个，不重试；③**边跑边 append 写盘**，别攒到最后一次性写（被打断就全丢）；④报告把「代码/脚本」与「文档」分开——真会出事的是 `.py/.ps1/.rs/.gd/.sh/.cfg`，`.md` 里多半是历史记录，不必改。

跨盘复制过来的 Rust `target/` 会带着旧盘的绝对路径，`cargo test` 直接 rc=101（`couldn't find file …/target/debug/build/…/private.rs`）——**清掉重建**（`cargo clean`），别改 `.cargo/config` 或环境变量去迁就它。同理，判断「迁移后某仓是否干净」不能看 `target/` 在不在，要看重建后测试是否全绿。

## G. 全量改名（技能 / 项目目录 / 远端仓）

适用：主人说「以后叫 X」——把 `hermes-cloud-backup` 这类旧名从整条链上换掉。**改名是迁移的子集**，漏一处就出现「技能加载不到 / cron 跑旧路径 / 推错仓」。

### G1. 改名范围（一次列全，别边改边找）

| # | 位置 | 改什么 |
|---|---|---|
| 1 | 技能目录名 | `skills/<旧名>` → `skills/<新名>` |
| 2 | `SKILL.md` frontmatter | `name:` 与 `description:`（描述要同时改成新架构，否则「本地备份」还写着「via a private GitHub repo」） |
| 3 | 技能内全部文本 | `.py/.sh/.md/.json/.ps1` 里的旧名与旧路径；改后 `grep -r <旧名>` 应**只剩 `.git/` 内部**（reflog、config） |
| 4 | 运行目录副本 | `<HERMES_HOME>/scripts/*.py` 里的 `SYNC_SCRIPT = HOME/"skills"/"<名>"/...`——这是 cron 实际执行的那份，漏改＝cron 每晚报 `FileNotFoundError` |
| 5 | 项目目录 | `<工作区>/build/<旧名>` → `<新名>`；改完把 `git remote set-url` 指到远端新名 |
| 6 | 镜像里的副本 | `<镜像>/tools/`（sync 每轮覆盖，改不改都会被下一轮刷成新值，但手改期间别怕） |
| 7 | 本地配置与远端 | `local.json` 的 `repo`、仓的 `origin` URL、GitHub 仓名 |

### G2. Windows 下目录被占用就改不了名

改名报 `PermissionError: [WinError 32] 另一个程序正在使用此文件` ＝ 有进程把它当 **cwd**（正在跑的 sync、cron 包装器、子代理都会）。**不要循环硬撞**，用「等空闲窗口再原子改名」：

```python
import os, time, psutil
OLD, NEW = r"<旧目录>", r"<新目录>"
def holders():
    out = []
    for p in psutil.process_iter(["pid", "cmdline", "cwd"]):
        cw, cl = str(p.info["cwd"] or ""), " ".join(p.info["cmdline"] or [])
        if OLD in cw or OLD in cl:
            out.append(p.info["pid"])
    return out
while time.time() < deadline:
    if not holders():
        try:
            os.rename(OLD, NEW); break
        except OSError:
            pass
    time.sleep(2)
```

判据：改名成功那一刻打时间戳，**立刻**跑字符串替换（旧路径引用从下一秒起就是死链）；改完 `skill_view(name="<新名>")` 必须能加载、`scripts/*.py` 全部 `py_compile` 通过。

### G3. 删远端仓之前先落本地归档（顺序不能反）

删 GitHub 数据仓前，确认**本地已经有它的全部对象**：

```bash
git clone --mirror <远端URL> <本地>/gen1.git      # 大仓几分钟到十几分钟，后台跑
git -C <本地>/gen1.git rev-list --count --all      # 必须 >0
for r in main master <其它分支>; do
  R=$(git ls-remote origin refs/heads/$r | cut -f1)
  git -C <镜像> cat-file -e "$R" && echo "$r 本地已有" || echo "$r **缺**"
done
```

三个 ref 全在本地才能 `DELETE /repos/<owner>/<repo>`；任缺一条就先把缺的拉下来。删完把镜像的 `origin` 移除（否则下一轮 sync 会把它写回去，指向一个 404 仓库）——这是**设计后果**，不是故障。

转纯本机后有三条不变量，破了任何一条都可能把私人数据推到公开面：

1. `sync.py` **默认只 commit**，`--push` 才推送；判据句随之改成 `pushed=None` 是正常值（见 §G5）。
2. `local.json` 的 `repo` 置空、`mirror` 指本机仓——**数据镜像绝不能复用技能源码仓的名字**（那个仓是 public，内含会话与凭据）。
3. 每轮 sync 会把 `--remote` 解析出的 URL 写回 `origin`；没有 `--remote` 时应保持**无 origin**。看到 origin 又冒出来，先看是谁在带 `--remote` 跑，而不是删了就算修好。

### G4. 「数据丢了」之前，先把 temp 里的旧副本当成候选源

迁移/清理现场常在 `temp` 留三样东西，任一样都可能是唯一的副本：**迁移暂存目录**（旧镜像工作树的整体拷贝，可能带 `.git`）、**旧机 `state.db` 快照**、**导出 tar/jsonl**。判「丢了」之前做一次四方差集（按**会话 id 集合**，不看体积/文件数）：

```python
staging_ids − mirror_ids   # 只此一份：立即 restore_sessions --source 捞回
old_db_ids  − live_ids     # 旧机整库比活库全？
```

实案：迁移暂存里躺着 1 个活库和镜像都没有的会话（171 条消息），靠这条差集找回；旧机 10-29 全库 2,119 个会话是活库的**真子集**（差集 0），证明「恢复没漏」——这个结论用集合验，不要用体积或条数猜。

### G5. 改了默认行为，文档里所有「判据句」要跟着改

把「默认推送」改成「默认只本地提交」之后，`完成标准：pushed=True`、`同步后 pushed 为 true`、`收尾判据 committed=True pushed=True` 这几句全部变成**假判据**（会让人把正常轮判成失败，或反向放过）。改代码的同一次把这几处一起改掉：纯本机模式的正常末行是 `committed=True pushed=None`，判据换成「镜像 HEAD 前进 + `git status` 只剩未跟踪增量」。
