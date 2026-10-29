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
   同时核 `git rev-parse --short HEAD` 与 `git ls-remote origin refs/heads/main` 是否一致。
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

1. 一轮真同步：`mirror=<新路径>` + `committed=True pushed=True` + `ls-remote` == 本地 HEAD。
2. 一轮真恢复：小档会话数/消息数与镜像一致、`message_uid` 无空值。
3. 旧副本 HEAD 与 reflog 时间戳在观察窗内不动（新镜像已推进 ≥1 轮）。

缺第 3 条＝可能双写；缺第 2 条＝只证明了「能同步」，没证明「能恢复」。
