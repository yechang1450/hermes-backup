---
name: hermes-backup
description: Local git-mirror backup/restore of Hermes state (no cloud).
version: 1.1.9
author: yechang1450, Hermes Agent
license: UNLICENSED
platforms: [windows]
metadata:
  hermes:
    tags: [backup, restore, github, cloud, sync]
    related_skills: []
---

> **现行模式（2026-10-31 起）**：纯本机线下备份 —— 镜像仓在 `~/hermes-backup`（本机 git 仓），
> `sync.py` 默认只 commit 到本地、**不推送**（要推才加 `--push`）；GitHub 上只保留本技能源码仓，
> 不再保留任何数据仓。

# hermes-backup — Hermes 数据备份 / 恢复

> **维护笔记（发布前必读）**：①官方策展器会持续回写本技能文件（回写带 CRLF）；发布时把技能目录拷入暂存后先 `sed -i 's/\r$//'` 归一（git 的 `text=auto` 在提交侧也会归一 blob），再用「全新克隆 ↔ 暂存」逐字节比对核实——**判 CRLF 用 python 字节计数，不要用 MSYS grep（管道层会造假 CR）**。②公开仓默认分支为 **main**（`git push origin HEAD`）。③一致性四处：技能目录 ↔ 暂存 `temp/hcb-publish` ↔ 公开仓 ↔ 数据镜像 tools/。④改会话存储格式或恢复工具时，同步更新**镜像根 README** 的格式描述（该文件不被 sync 自动生成；手改后随下轮同步提交）。⑤**owner 会直接改公开仓**（如政策提交：删 LICENSE、`license: MIT → UNLICENSED`、README 改 Copyright 段）——推送前必 `git fetch` 并 `git log HEAD..origin/main` 看有没有远端新提交；有就 `git reset --hard origin/main` 再把本地的文件改动重放上去重新提交，**绝不 force / 绝不覆盖他的提交**。本仓的常态口径＝**无 LICENSE 文件**（保留所有权利）、frontmatter `license: UNLICENSED`；别再写回 MIT。⑥**可能有并行 agent 也在维护本技能**（2026-10-24/25 实见两笔既非 owner 也非本对话的提交：`restore_sessions --verify`、推送预算 900→1500s／外层 3300→3900s）——每次动手前 `git log --oneline -3` 看一眼，推前必 fetch，**只做增量、绝不回滚别人的提交**；改同一文件前先重读当前内容（技能目录可能已被对方改过）。⑦**版本号当最后一步改**：`SKILL.md` 的 `version:` 要在**提交前最后一次**写，并让提交信息里的版本号与它一致；提交后回读一次 `git show origin/main:SKILL.md | grep '^version'` 核对。多写者并行时很容易出现「0.4.22 的提交信息里装着 0.4.23 的内容」这种考古灾难——本次就因对方已把版本提到 0.4.23、我的 `sed 0.4.21→0.4.22` 静默不匹配而撞上。⑧**公开仓零本机痕迹**：脚本/测试里不得出现 `C:/Users/<名>` 这类绝对路径（既泄露用户名，又让测试换机即败）——一律按 `Path(__file__).resolve().parent` 拼；发布前用 `grep -rn "C:/Users/" scripts/` 扫一遍。⑨**文档里不写死可增长的数量**（几条坑 / 几套测试 / 几个场景 / 几个脚本）：每加一条就要回来改 SKILL、references、README 三处，漏改了也没人报错（实见同一份文档里「62 条」「8 套」「10 个场景」「9 套」四处同时过期）。要么写「以 `ls scripts/test-*.py | wc -l` 为准」，要么只给末行判据；能写死的只有能对回 `sync.py` 同一行的**代码常量**（分片阈值 / 超时 / 体积闸）。⑩**别把技能目录整份拷进暂存仓**——owner 与并行 agent 会直接改仓里的文件（实测：README 里新加的许可与权利声明被整份覆盖时静默回滚，还把他早删掉的 `LICENSE` 带了回去）；推前按 `git status` 只复制**你实际改过的那几个文件**，提交前 `git diff` 扫一眼有没有删掉别人的段落。

把本机 Hermes 的全部状态（配置/凭据/记忆/技能/会话/桌面端数据）**线性同步**到一个
**本机 git 镜像仓**（默认 `~/hermes-backup`；2026-10-31 起纯线下，不联网、不推 GitHub），并支持在任何新机上
从该仓**一键恢复到重装前状态**。本技能含两套流程：**日常同步**（源机）与**新机恢复**（目标机）。

架构一句话：镜像工作区 `~/hermes-backup` 采集本机数据 → commit（**默认不 push**，`--push` 才推）；恢复方向相反。

## 单写者纪律（同一条线只许一个进程写镜像）

镜像仓、`state.db`、技能目录这三样**都不是多写者安全的**。换机/重装后的「跑通」任务尤其容易撞车：

- **动手前先查占用**：读 `<HERMES_HOME>/state/hermes-backup-sync.lock`（按 pid 判活），再用 psutil 扫有没有进程的
  `cwd` 落在镜像或技能目录里。有活写者就**等它收工**——不杀、不删锁；Windows 下目录被当 cwd 时连**改名都会失败**（WinError 32）。
- **`--allow-shrink` 是逃生口，不是常规参数**：语义＝「我确认用本机现状覆盖备份」。误用一次，工作树里**未跟踪**的会话文件
  会被 prune 掉，且**没有任何副本可恢复**（git 里没有、活库里没有）。真要放行，先打可回取点：
  `git branch -f pre-<机器>-takeover HEAD`（本地仓没有远端也能打）。
- **恢复也必须单写者**：两条线同时对同一活库跑 `restore_sessions` / 合并脚本，会留下**重复消息**与**空会话壳**；
  判据与处置见「Verification」节。看到活库 `state.db-wal` 的 mtime 一直在动＝还有人在写，此时只读不写。

## When to Use

- 用户说「备份一下 / 同步一下 / 推到 GitHub / 把 Hermes 存一下」→ 执行 **流程 A（日常同步）**
  （当前 `local.json` 的 `mode=local-only`：**默认只提交到本机镜像，不联网**；用户要推 GitHub 时必须显式 `--push`，
  否则本轮只落本地，事后才发现没推）
- 用户在新机或重装后说「恢复我的 Hermes 备份 / 从 GitHub 恢复之前的状态」→ 严格执行 **流程 B（新机恢复，见 templates/RESTORE.md）**
- Don't use for：非 Hermes 数据；公开仓库（**本仓必须永远保持私有**，内含凭据与私人对话）。

## Prerequisites

- 本机一块可写的盘（镜像仓默认 `~/hermes-backup`）。首次使用：`python <SKILL>/scripts/sync.py --owner <你的GitHub用户名> --create-remote`（自动建私有仓、配 remote；已有仓直接识别）。owner/remote 也会从镜像仓 git remote 或 `gh` 登录名自动解析。
- 本机 Hermes 安装完整：`HERMES_HOME`（默认 `%LOCALAPPDATA%\hermes`）内有 `hermes-agent/` 源码与 `bin/`、`tools/python-*/`。
- 恢复端只需要：**本机镜像目录**（`~/hermes-backup`）＋ 一个 git。纯本机模式下**不需要任何 GitHub 凭据**；
  只有把镜像搬到另一台机器时才需要凭据。**恢复全程不依赖 `gh`**：`restore_files.py` /
  `restore_sessions.py` / `apply_routing.py` 里 `gh` 出现 0 次——别把「本机没装 gh」当成恢复的前置阻塞项。
- **`gh` 只在日常同步（`sync.py`）里必需**：解析属主、私有性检查（fail-closed，没有 gh 或查询失败
  ⇒ **拒绝推送**）、`--create-remote` 建仓、`gh repo edit --visibility` 改可见性。确需在无法验证的环境里推，
  显式加 `--allow-unverified-privacy`。
- 脚本纯标准库；除会话导入外都可用任意 python 直跑。

## Quick Reference

| 操作 | 命令（`<SKILL>` = 本技能目录，`<REPO>` = 备份仓 clone 目录） |
|---|---|
| 首次初始化（建私有仓） | `python <SKILL>/scripts/sync.py --owner <gh-user> --create-remote` |
| 日常同步 | `python <SKILL>/scripts/sync.py` |
| 文件层恢复 | `python <REPO>/tools/restore_files.py --repo <REPO> --home <HERMES_HOME>` |
| 会话层恢复 | `python <REPO>/tools/run_in_runtime.py <REPO>/tools/restore_sessions.py --all`（加 `--fast-import`，大型历史实测 2–4 倍速，见 references/session-storage.md） |
| 拼包（给桌面端导入用） | `python <REPO>/tools/concat_sessions.py main` |
| 全量自检（一条命令） | `python <SKILL>/scripts/selfcheck.py --repo <REPO>`（默认：编译＋全部 `test-*.py`＋文档引用＋公开面卫生＋**自动同步调度**；加 `--repo` 再加**退役档完整性**与分片体检，加 `--audit` 再跑会话层覆盖率审计。**项数以它自己打印的合计为准**） |
| 覆盖率审计（镜像 vs 活库） | `python <SKILL>/scripts/audit-coverage.py --repo <REPO> --home <HERMES_HOME> --profile main`（PASS 打印 `AUDIT-PASS`，硬伤退出码 1；加 `--with-files` 连文件层一起审） |
| 数据仓换代（体积到阈值） | `bash <SKILL>/scripts/rotate-generation.sh --from <旧代> --to <新代> --dry-run` 先看计划，再去掉 `--dry-run` 真跑（见 references/generations.md） |
| 路由回写（微信/QQ 续接） | `python <REPO>/tools/apply_routing.py` |
| 桌面端收尾器 | `pwsh -NoProfile -File <REPO>/tools/finish_restore.ps1 -Repo <REPO>` |

> **两个 `tools/` 别搞混**：`<REPO>/tools/` 只在**备份仓 clone 目录**里（每轮同步时由本技能 `scripts/` 复制过去，`~/hermes-backup/tools/` 就是它）；**技能仓本身只有 `scripts/`，没有 `tools/`**。所以恢复类命令要在备份仓目录下跑，同步/审计/换代类命令用 `<SKILL>/scripts/`。

> **自检只能跑技能目录那份**：`<REPO>/tools/selfcheck.py` 是**镜像里的旧副本**，它按「脚本所在目录的父目录」找 `scripts/test-*.py`，在镜像里只找到 `tools/` ⇒ 直接报 `scripts/ 下没有 test-*.py`、`0 套不能算通过`，还会连带把文档引用判成上百处悬空（引用的是技能目录才有的文件）。这是**位置错，不是回归**：判据＝输出里出现「scripts/ 下没有 test-*.py」时先看命令写的是 `<SKILL>/scripts/selfcheck.py` 还是 `<REPO>/tools/selfcheck.py`，换前者重跑；不要据此去补测试或改文档。

> **补推要写对分支名**：数据镜像与公开技能仓都以 **`main`** 为默认分支。手工补推一律 `git push origin HEAD`（或显式 `HEAD:main`）；写成 `HEAD:master` 会在远端凭空多出一条同 SHA 的 `master`（`remote: Create a pull request for 'master'`）——默认分支没变、同步不受影响，但下次有人按 `ls-remote --heads` 对数会看不懂。发现已建出来时，确认它与 `main` 同 SHA 后删除即可（不是改写历史）。

> **解释器与远端**：命令里的 `python` 要用 PATH 上的 Hermes python；**不要手拼 `tools/python-3.14.7+…` 的目录名**——真实目录名含被掩码的星号段，bash/subagent 里会被当字面量或通配，实测直接 `exit 127`。`sync.py` 会把 `origin` 改写成它解析出的 `REMOTE_URL`；若本机 HTTPS 到 GitHub 被 reset 而 SSH 可用，显式加 `--remote ssh://git@ssh.github.com:443/<owner>/<repo>.git`（预检也用同一 URL），否则会在 git pull/push 段失败。

> 机器本地配置（镜像目录、同步默认仓名）放技能目录 `local.json` 的 `mirror` / `repo` 两项（不进任何仓库）；镜像目录默认自动探测，`--mirror` 可指定；镜像已有 origin 时仓名以 origin 为准（换代脚本会同时改写 origin 与 `local.json`，代码里不写死代名）。

## 主机路径迁移（换盘 / 换目录 / 换用户名）

宿主把工作区或镜像换根之后，**地址不会自动跟上**：代码默认值、`local.json`、cron 的 workdir、运行目录壳、文档示例、记忆各存一份。按顺序改完再点火：

1. `local.json` 的 `mirror` 写**真路径**——这是唯一该出现用户名的地方，它不进任何仓库。
2. 代码默认值用 `Path.home() / "hermes-backup"`，**绝不写死 `C:/Users/<名>`**：写死了既泄用户名，又让公开仓的隐私扫描（`privacy-scan.py` 第三道）必红。
3. 文档与示例一律 `~/hermes-backup`、`~/workspace`（Windows 即 `C:\Users\<你>\…`）。
4. 新镜像若是「从旧盘拷过来的中间态」，先 `git fetch` 并对齐远端（`git reset --hard origin/main`），**别在落后的工作树上直接跑轮**。
5. **点火一轮真同步验收**：日志头 `解析：mirror=<新路径>`、末行 `committed=True`（纯本机模式下 `pushed=None` 是**正常值**）、镜像 HEAD 比上一轮前进；只有显式 `--push` 的轮次才要求 `pushed=True` 且 `git ls-remote` 与本地 `HEAD` 一致。
6. 记忆里写**一条全局映射**（旧路径 → 新路径）就够，比逐条改上百处便宜且不会漏。
7. **重装/换机后重建两个 cron 任务，备份线才会真的跑。** 镜像仓、脚本、`local.json` 都在 **不等于**自动同步在跑：
   实测新机上 `cron/jobs.json` 里 0 条，镜像仓最后一轮停在数小时前，而 `HERMES_HOME\scripts\` 里的
   `hb-sentinel.py` / `hb-autosync.py` 与技能副本 sha256 完全一致——**脚本没问题，是没人调度它**。
   按「自动同步」节的两条 `hermes cron create` 重建，然后用 `cron/executions.db` 里**该 job_id 的行**
   证明它真的在跳。三种证据别混：`cron list` 的 Last Run 只是上次遗留值、`cron run` 只证明脚本能跑、
   只有 executions.db 里带新时间戳的 `completed` 才证明 ticker 在工作。判 `Next run` 是否过期前先取一次
   当前时间——`cron run` 打印的那个 Next run 是本次手动触发的下一个槽位，不是 ticker 卡住的证据。

三条必踩的：

- **`~` 不是 Python 的语法**：把 `D:/x` 批量替换成 `~/x` 会让 `Path("~/x")` 永远不存在（被当相对路径），必须改回 `Path.home() / "x"`；替换后逐个 `py_compile`，并在解释器里 `print(m.MIRROR, m.MIRROR.exists())` 验一次。
- **报「换盘后变慢」前先确认那一轮旁边有没有别的重活**：同一份数据、同一套代码，干净窗口实测 **485–496s**（旧址 282–325s，+75%）；而 1605–2697s 那两轮都撞着 3.4GB 的 `git reset --hard`、自检与克隆——**慢在争抢，不在磁盘**（同目录 3,000 个小文件写入：新盘 2,227 文件/秒 vs 旧盘 1,954，小文件反而更快；只有单文件顺序写才是 104 vs 343MB/s）。口径：测基线挑干净窗口；超时兜底按「干净轮最大值 ×2」留余量，**别拿被污染的样本改参数、也别把归因写进文档**。
- **迁移脚本会留一批 `.bak-<日期>` 副本**：`~/.hermes` 下可能有上百个，会被镜像进备份仓（恢复时原样回到新机）；落进技能目录的那批还会经 `tools/` 发布到公开仓——发布前 `find <技能目录> -name '*.bak-*'` 清掉自己那份，别人的（`cron/`、`profiles/`）只报不动。
- **镜像树是 `sync.py` 每轮的产物，不是源——批量改路径时必须把它排除在外。**
  `home-overlay/` / `profiles-overlay/` / `sessions/` / `desktop-overlay/` 每轮由采集整份覆盖，手改只会制造
  「源与镜像不一致」，下一轮被覆盖回去（期间还可能被当成真实差异提交进历史）。**尤其别在镜像里做字节级
  字符串替换**：它还躺着 SQLite（`kanban/boards/*/kanban.db`、`cron/*.db`），按字节替换会改坏二进制页。
  实测一次迁移里误改镜像 140 个文件，其中一块板库命中 4,064 处；靠 `.bak-<日期>` 全量还原（448 个）
  ＋逐库 `PRAGMA quick_check`（12 个库全 `ok`）才确认没搞坏。判据一句话：**这个目录会不会被某个进程
  整份重新生成？会 ⇒ 只读，想让它变就重跑一轮 sync。** 例外是镜像根 `README` 这类「不被 sync 生成」的文件
  （见维护笔记④）。
  同理：用户自己粘贴/上传的原始输入存档（`attachments/`、`composer-pastes/`）是取证材料，不是配置。

迁移**是否真的完成**按 `references/host-migration.md` 的六路审计验，不要凭「目录都在」下结论。
**技能 / 项目目录改名**（如 `hermes-cloud-backup` → `hermes-backup` 这类全量更名）按同文件 §G 的流程走——
目录被进程当成 cwd 时 Windows 会拒绝改名，必须等空闲窗口再原子改，不能边跑边改。

## 文件构成

- `scripts/sync.py` — 日常同步（采集 → commit → push；本机镜像工作区）
- `scripts/restore_files.py` — 文件层恢复（home/profiles 覆盖，旧件自动备份）
- `scripts/restore_sessions.py` — 会话层恢复（走官方 SessionDB 导入管线）
- `scripts/run_in_runtime.py` — 在 Hermes 官方运行时里执行脚本（会话导入需要）
- `scripts/apply_routing.py` — 平台路由回写（微信/QQ 旧线程续接）
- `scripts/concat_sessions.py` — 会话拼包（桌面端「导入」按钮用）
- `scripts/finish_restore.ps1` — 桌面端收尾器（等待退出 → 替换数据 → 重启网关/桌面端）
- `scripts/hb-sentinel.py` — 自动同步哨兵（①启动 / ②静默≥5分钟 / ④长静默≥1h 恢复触发；安装=复制到 `HERMES_HOME\scripts\` 后由官方 cron 每 2 分钟调用）
- `scripts/hb-autosync.py` — 周期同步（③每 2 小时；安装方式同上）；与哨兵共用 `state/hb-sync-child.log`（子进程输出流式落盘）与 `state/hb-sync-health.json`（连续失败计数 + needs-attention 标记）
- `scripts/export_all_profiles.py` — 单进程批量会话导出（sync 提速件；直调官方导出函数，与 CLI 输出逐字节一致，失败自动回退逐档 CLI）
- `scripts/verify_drill.py` — 演练对账器（镜像会话文件 vs 沙箱 DB 逐会话比对；`restore-drill.md` 第 6 步的现成实现，参数＝沙箱根目录）
- `scripts/test-hb-sentinel.py` — 哨兵分支回归（（场景块 S1/S1b/S2–S9/S5b/S10/S11…，**别在文档里写死数量**）——①网关重启 / ②静默 5 分钟 / ④长静默恢复 / 新鲜度闸 / 锁跳过（活 pid vs 死 pid）/ 失败上报 / 冷启动两态 / 首轮失败重试；假 HERMES_HOME＋桩 sync.py，不碰真数据）。**别在文档里写死场景数**——加一个分支就得改三处，直接说「跑通即看末行『总判定：PASS』」
- `scripts/test-hb-health.py` — 健康记账回归（sync.py 单一来源 + 包装"未跑完"补偿去重）
- `scripts/test-audit-coverage.py` — 覆盖率审计的**负向对照**（8 场景合成夹具：全等 / 缺会话 / 多会话 / 镜像超前 / 镜像落后 / 轮内新建豁免 / 孤立组 / 坏 JSON；证明审计"该红就红"，不碰真库）
- `scripts/selfcheck.py` — **一条命令全量自检**：编译 + 全部 `test-*.py` + 文档引用完整性 + **公开面卫生**（`privacy-scan`）＋**自动同步调度**（`cron/jobs.json` 两个任务在册 + 哨兵最近 15 分钟内有 completed 记录——换机后 cron 为空、静默标记把自动轮压死，这两种故障 `cron list` 都显示正常）＋（`--repo` 时）**退役档完整性**（`local.json` 的 `preserve_baseline` 基线条数，任何一档变少即判红——2026-10-31 实案：1,339 个退役档会话被外部清理删掉）＋（`--repo` 时）分片体检＋（`--audit` 时）会话层覆盖率审计；末行 `SELFCHECK-PASS/FAIL`，退出码 0/2。**只读**：不写镜像、不碰活库。有同步轮在跑时会先告警（自检抢磁盘会把那一轮拖成超时）
- `scripts/privacy-scan.py` — **推前三道自检**（凭据文件 / 真实私人信息 / 本机绝对路径；`--repo <仓> --history` 连全历史扫）；命中退出码 2
- `scripts/test-split.py` — 分片回归（合成 2MB/100MB/45MB 三条会话、阈值取 `sync.PART_MAX`）：断言每片≤阈值、片数＝ceil(字节/阈值)、按片名拼接后**小会话逐字节相同**、**>5MB 的会话语义等价**（`json.loads` 相同——它们会被尾部稳定化重排成员顺序，不能再用字节相等断言）
- `scripts/test-layout.py` — 尾部稳定化的回归（6 项：语义等价 / 幂等 / 字段落尾 / 追加后 messages 前缀不变 / 40MB 性能 / 前半段＝KEEP_KEYS 且 messages 收尾）
- `scripts/churn-report.py` — **单轮重写量**量测器（按目录汇总新增 blob 字节、列 >5MB 改动）：判断「体积增长有没有真被压住」只认它，不看 `--stat` 的文件数/行数；`git --raw` 解析用 `-z`，带空格路径不会错位
- `scripts/session_layout.py` — 会话 JSON 的尾部稳定化（零解析扫描；**只留身份字段＋`messages` 在前，其余成员全部移到对象末尾**）
- `scripts/test-reap-exporters.py` — 孤儿导出回收回归（3 场景：孤儿被杀 / 父进程健在者不动 / 命令行只提到文件名者不杀）
- `scripts/test-shrink-guard.py` — **新机/未恢复保护回归**（备份大活库小 ⇒ 拒绝 / 正常增量 ⇒ 放行 / 小档不误伤 / 分片只按会话 id 计一次 / 活库读不到不误拦 / **工作树被误 prune 后仍按 git HEAD 计数** / **退役档 `preserve_sessions` 进不进分母的对照 + 不被列入可删档**。项数以它自己打印的合计为准，别在文档里写死）
- `scripts/compare-config.py` — 配置逐键比对（备份的 `config.yaml` vs 本机的，只读；选择性恢复时先看出差异再决定合并哪些键）
- `scripts/test-zero-session.py` — 零产出保护回归（5 项：0 会话 + 有历史 ⇒ 判失败并保留旧文件 / 空档不误判 / 有产出不干预 / prune 后旧文件仍在）
- `scripts/test-prune-protect.py` — 覆盖三类收敛保护：整档删除授权、`snapshot_sqlite` 回退不得递归、**`prune_unvisited(keep_paths=)` 的保留子树**、**`collect_profiles` 活库零档时整层不清**（项数以它自己打印的合计为准）
- `scripts/test-kanban-snapshot.py` — SQLite 在线快照回归（3 项：WAL 下快照可正常打开 / 表结构与行数一致 / 普通拷贝的反例）
- `scripts/test-kanban-scope.py` — kanban 采集范围回归（只备 boards 板库 / 不备工作区 db / 顶层 kanban.db / **prune 之后板库仍在**；走快照 API 由 `test-kanban-snapshot.py` 覆盖。**项数以末行合计为准，别写死**）
- `scripts/check-paths.py` — 恢复后扫「旧机器绝对路径」残留（默认只报告，`--fix` 才改写、先落 `.bak-paths-<时间>`；只动白名单文本件，不碰 `.db`/日志/会话）
- `scripts/start-finish-restore.ps1` — 桌面端收尾器的壳（内联拼 `Invoke-CimMethod` 经 bash→pwsh 三层转义必 ParserError，故用壳；0=已拉起 / 2=找不到收尾器）
- `scripts/test-commit-guards.py` — 提交期两道闸的回归（批量删除闸 4 项＋整档删除授权 3 项；用假 git，不碰真仓）
- `scripts/check-parts.py` — 镜像分片形态体检（P1 编号连续 / P2 除末片外每片正好 PART_MAX / P3 末片合规 / P4 无形态混用；`PARTS-FAIL` 退出码 1）。改切片代码后用它验收「数据是否真的归位」
- `scripts/audit-coverage.py` — 覆盖率审计：①会话层（镜像会话文件 vs 活库 state.db 逐会话条数）②文件层（`--with-files`：HERMES_HOME 顶层条目 vs 采集白名单，列出未分类/漏备项）；判据＝硬伤退出码 1
- `scripts/rotate-generation.sh` — 数据仓换代（**分批种子**推到新代私有仓 → 旧仓归档；`--dry-run` 只打印计划；幂等可续跑）。锁**判活**用 psutil（MSYS 的 `kill -0` 看不到原生 Windows 进程，会把活锁当死锁）；工作树有改动时不硬退——桌面端 leveldb 一直在写，永远等不到“干净”，真跑先提交收口（并尝试推送一次）再换代
- `scripts/seed_generation.sh` — 换代种子：整轮**钉住开始时的 `BASE` commit**（分批／组树／计数全读 `BASE`，不碰工作树），每批 `read-tree "$BASE"` + `update-index --force-remove -z --stdin` 反向删掉本批之外的路径，第 i 棵树＝前 i 批的**并集**（累积），分批 push（每批 ≤800MB，绕开单次 2GB 上限），末树必须等于 `BASE^{tree}`；可单独对本地裸库自测（第二遍应为 **no-op**）；校验只认 `refs/heads/main`。历史老写法（按 `ls-files` 名单 `git add`）**已被真实环境否掉**，见 pitfalls ④⑤⑥
- `references/backup-scope.md` — **备份范围唯一权威表**（进/不进、原因、怎么改、改完跑什么）；其他文档只许指向它
- `references/operations.md` — **日常运维一页纸**（每天 30 秒看什么 / 每周跑什么 / 什么信号该动手 / 告警怎么接到人）
- `CHANGELOG.md` — 版本演进与「踩过的坑怎么修的」
- `references/pitfalls.md` — **实战坑全文**（按 6 组主题分类；条数随修随增，别在文档里写死数字）；排查时先查这份，SKILL 正文只留最常咬人的几条
- `references/generations.md` — 数据仓「换代」归档预案（体积到阈值时换新代仓、旧仓冻结为只读档案；不重写历史）
- `templates/RESTORE.md` — 新机恢复手册（编号步骤 + 验收清单）
- `references/session-storage.md` — 会话存储格式与历史体积机制（v0.3 原样文本格式、实测方法、GitHub 链路限制）
- `scripts/split-archive-session.py` — 巨会话瘦身：把 `active=0` 归档行迁到独立「归档会话」（`--apply` 才写库；幂等、可回滚；用法见 `references/selective-restore.md` §7.4）
- `references/auto-sync.md` — 自动同步（watchdog 四机制）判定、静默信号源与实施顺序
- `references/restore-drill.md` — 恢复侧验收演练配方（两遍独立演练标准、逐会话对账口径、判读坑）
- `references/audit-playbook.md` — **自查手册**：四路审计（文档↔代码漂移 / 全新克隆下逐个脚本可跑性 / 代码级静默失败审查 / 公开面卫生）+ 误报判读与收尾
- `references/host-migration.md` — **主机换盘/换目录后的六路审计**（数据完整性 / 引用完整性 / 跨盘残留 / 运行配置 / 旧副本停写 / 恢复演练）＋轻探针命令＋「哪些差异是正常淘汰」判读表
- `references/selective-restore.md` — **选择性恢复配方**（先 clone → 盘点出真数 → 给可选菜单 → 配置文件逐键合并 → 执行顺序与回滚）；同一台机器重装后的恢复走这条，不要整机覆盖。**§8＝只挑一批会话恢复**（按标题前缀/归档位/子代理闭包挑，先验源覆盖度，连 composer-images 一起搬，比 session_key 与配对 id 再承诺续写）；**§8.5 起＝就地合并（ATTACH 源库 → 向活库 INSERT），不换 state.db**——换库要停全部持库者，而执行者自己就在其中

## 本机部署的三条现实约束（2026-11-01 实测，改判据前先读）

1. **镜像是浅克隆**：`.git/shallow` 指向某一轮的提交，`git rev-list --count HEAD` 只有 30 轮左右，
   更早的快照**本机取不回**（`git show` 只能回到浅点之后）。判「能不能取回某天状态」看两条：
   `git rev-parse --is-shallow-repository` 与 `git rev-list --count HEAD`。要长期留存历史，
   定期把整仓打成 bundle 放到**另一块物理盘**：`git -C ~/hermes-backup bundle create <外盘>/hb-<日期>.bundle --all`。
2. **单盘单副本**：纯本机模式下镜像与活库在同一块盘（`C:`），同盘＝同一故障域；磁盘坏了源和备份一起没。
   这不是代码问题，是部署问题——要么外接一块盘做 bundle，要么接受「只防误删、不防盘损」。
3. **统一内存机器（AMD APU）的内存挤压**：核显与系统共享内存，桌面合成器（dwm）在 UMA 下会短暂吃掉几十 GB，
   可用内存可能瞬间见底。这时 `sync.py` 的内存闸会让本轮 **exit 2（aborted=low_memory）**，
   账本里记 `mem_avail_gb`/`mem_total_gb`；**这不是故障**，等内存回落下一轮自然恢复。
   判据：`hb-sync-runs.jsonl` 该行 `aborted` 含 `low_memory` 且 `mem_avail_gb` 很低。

## 流程 A：日常同步（源机）

> **换机 / 重装后先恢复，再同步。** `sync.py` 在采集之前有一道 **shrink guard**：活库会话数 < 备份的 50%
> （且备份里已有 20 个以上会话）⇒ **直接拒绝，镜像一个字节都不动**（exit 6，`report.aborted=shrink_guard`）。
> 依据是一次实案：新机空库（87 个会话）触发了一轮同步，把镜像里 3,603 个会话中的 2,311 个文件、
> 8,284 个 profile 文件和 1,569 个 home 文件当「残留」清掉——只因 `gh` 不可用拒绝推送才没提交。
> 确要用本机现状覆盖备份：加 `--allow-shrink`；想「旧机归档 + 新机继续」两代都留：走 `references/generations.md` 的换代。

**退役 profile（不再恢复的 bot 档）走 `preserve_sessions`，不要用 `--allow-shrink`。** 只在 default 一档的新机上，备份里那十几个 bot 档会同时踩两道闸：`_count_live_sessions` 给出 0（`profiles/<name>/` 整个不存在）⇒ shrink guard 判「将被清空」拒绝；`_deletable_profiles` 又因目录不存在把它们全判「可整档删除」⇒ 就算放行也会把整档 rmtree。在 `local.json` 里列出这些档即可：

```json
{ "mirror": "…", "repo": "…", "preserve_sessions": ["ceo", "proj-80", "…"] }
```

被列出的档：不计入 shrink 分母、不参与 `_deletable_profiles`、`prune_stale` 原样保留（它们的历史仍按「镜像独有、可随时 `git show` 取回」）。**`main` 不要写进去** —— 活库自己那档必须照常比对，否则这道闸就白设了。判据只在 `local.json`，别在代码里写死档名。

同一份名单还护着 `profiles-overlay/` 的配置树：活库一个档都没有时那一层本来就整层不清理（零值短路），但**活库还有别的档在、只有这几个退役档缺席**时，`collect_profiles` 会走 `prune_unvisited(keep_paths=…)` 把这些子树的 config/SOUL/skills 原样留下。两种形态都有回归（`test-prune-protect.py` 的零档与「有活档」两组）。

**新机 / 重装后第一次同步：先过这三道**（第四道是文末自检第 7 条的 `max_export_messages`）。缺任何一项都会以非零退出停下——那是保护，不是故障：

| 检查 | 判据命令 | 不满足时的表现 |
|---|---|---|
| `profiles/` 目录存在 | `ls "$HERMES_HOME/profiles"` | `FileNotFoundError …\hermes\profiles` → 整轮 `exit 2`。只有 default 一档的新机必现（备份里没有这个空目录），`mkdir -p` 即可 |
| `gh` 可执行（**仅 `--push` 轮适用**） | `gh auth status` | 私有性无法验证 → 拒绝推送 → `exit 4`。纯本机轮不解析远端、不调 gh，这一项不适用。`sync.py` 的 `_gh_exe()` **先看 PATH、再查官方安装位置**（`%ProgramFiles%\GitHub CLI\gh.exe` 等）——从备份恢复只拷了 exe、没走安装器时 PATH 是空的，这一步兜住；两处都找不到才判「gh 不可用」 |
| shrink guard 是否该放行 | 活库会话数 vs `sessions/*/` 文件数 | 活库 < 备份 50% → `exit 6`，镜像零改动 |

退出码对照：`2` 导出/采集失败（含 `low_memory` 跳轮）· `3` git 环节失败 · `4` 私有性无法验证拒推（仅 `--push`）·
`5` 零产出（该档保留旧文件）· `6` shrink guard 主动拒绝 · **`7` 批量删除闸**（拟删 >30% 已跟踪文件，按**失败**记账、不写静默标记）。

**新机恢复后第一次同步前，再做一次「会话 ID 对账」——只看条数会漏。** `restore_sessions` 的 `imported/skipped` 只统计这一轮处理过的会话，不等于镜像里的全部 ID 都进了活库；实测出现过镜像 `sessions/main` 有 2,247 个 ID、活库只有 2,205 个，随后一轮同步把镜像工作树里缺的 43 个会话文件 prune 掉。开跑前必须：①`git -C <镜像> ls-tree -r --name-only <commit> sessions/<档>` 取镜像 ID，`state.db` 取 `select id from sessions`，**差集必须为 0**；②差集非空时按 `git show <commit>:<路径>` 把缺的 `.jsonl` 与 `.part-*` 分片提取到一个目录，作为 `restore_sessions.py --source` 再导入，**不要先同步**；③`shrink_guard` 只拦「活库 < 备份 50%」，1–2% 的缺口它能放行，所以恢复完成前把两个自动同步 cron 保持 paused，补完再恢复。

**「新机接管备份」这一步，`shrink_guard` 保护不全 overlay 两层。**（`profiles-overlay/` 已有零值短路：活库 0 档 ⇒ 整层保留并打印 `profiles: 活库 0 档 —— 整层保留现有 N 个文件不清理`，**看到这行是正常**；`home-overlay/` 与 `desktop-overlay/` 仍然没有，按下面的流程走） 那道闸只比会话数；`home-overlay/` 与 `desktop-overlay/` 的 prune 没有闸——**本机没有的文件一律当残留删掉，然后正常 commit + push**。在半恢复的新机上直接跑一轮，等于把备份 HEAD 里的看板库、技能 references、平台账号文件、cron 输出一次摘掉（实测一轮 1555 条删除，其中 16 个是有内容的技能文档）。开跑前按这个顺序：

1. `git -C <镜像> status --porcelain | grep '^ D'` 列出待删清单，按顶层目录分组看（量级通常上千条，先分类再决定，别逐条看）。
2. **先打一个永久可回取点**：`git branch -f pre-<机器>-takeover HEAD && git push origin pre-<机器>-takeover`。prune 只动工作树与新一轮提交，历史与新分支都在；取回单条用 `git show pre-<机器>-takeover:<home-overlay 相对路径>`。
3. **把「有价值且本机缺」的文件从 HEAD 取回本机**：`git show HEAD:<路径>` 写到 `HERMES_HOME` 对应位置——采集看到文件在，就不会删它。值得捞回的是技能 `references/`、`scripts/`、平台账号文件；纯噪音（`cron/`、`.curator_backups/`、`pending/`、`*.bak-*`、旧日志）不必捞。
4. 之后才跑同步。验收**多一条**：抽查取回的文件仍在（`test -f`），且 `sessions/<档>` 文件数没腰斩。

**`local.json` 里的 mirror/repo 不能直接信**：它可能是上一次没做完的换代留下的（镜像指向一个根本不是 git 仓的目录、仓名在远端不存在）。判据三条：该目录存在且 `git -C <mirror> rev-parse` 成功、`git remote -v` 有 origin、`gh repo view <owner>/<repo>` 能查到。三条有任一不成立，就以**现存的真仓**为准改回 `local.json`，别顺着它去 `--create-remote` 新建一个空仓。

**`--allow-shrink` 之前先证可回滚**：`git ls-tree -r HEAD --name-only sessions/<档> | wc -l` 数出该档在 HEAD 里的文件数，并确认 `HEAD` 就是上一轮提交（纯本机模式没有 origin，别去找 `origin/main`）；建议先打一个可回取分支
`git branch -f pre-<机器>-takeover HEAD`。prune 只动工作树，历史仍可 `git show <sha>:sessions/<档>/<sid>.jsonl` 单条取回——这是「用本机现状覆盖镜像」可接受的前提，也是给用户解释「删了什么、怎么找回」的依据。

**发现工作树已经被误 prune（还没提交）时，整体回滚只要一条命令**：`git -C <镜像> checkout -- .` —— 已跟踪文件全部还原（实测 1.5 万个文件 4 秒，`D` 9839→0、`M` 168→0），**未跟踪的新文件原样保留**，而那正是本轮刚导出的增量。判据：`git status --porcelain` 的分组从 `D/M/??` 变成只剩 `??`。**别用 `git reset --hard`**（连未跟踪增量一起丢）、也别 `git clean`。回滚后先 `find <层> -type f | wc -l` 复核对得上 HEAD，再决定要不要重跑。这条路径救过一次 8,284 个 profile 文件的清空——prune 在采集阶段就跑完了，而提交在它之后，**中间这段窗口是可逆的**。

**内存两道闸（别拿「以前没炸过」赌）**：①导出走分块渲染（`CHUNK=4`），实测 1793MB 会话峰值只吃 0.2GB；
②`sync.py` 在批量导出前查可用内存——**<3GB 跳过本轮（exit 2，下轮再试）**、**<8GB 放弃单进程批量、退逐档 CLI**。
统一内存机器（核显与 CPU 共享同一池）尤其要守：图形进程吃掉的那份同时也是系统内存，爆掉会连桌面端一起带走。
同一时间不要叠自检 `--audit`、全量克隆、`git reset --hard` 这类重活。

1. 在 `terminal` 里跑 `python <SKILL>/scripts/sync.py`（后台跑也可以；其日志在 `<镜像>/logs/`）。
   **完成标准分两种模式**：纯本机模式下末尾是 `sync 完成：… committed=True pushed=None`，且镜像 `git log -1`
   比上一轮前进、`git status --porcelain` 只剩未跟踪增量；只有显式加了 `--push` 才要求 `pushed=True` +
   `git ls-remote` 与本地 HEAD 一致。别拿 `pushed=None` 判失败——那正是本机模式的正常值。
2. 铁规：**永不 force-push**；推送前脚本自检仓库私有性（gh 可用时；非私有会尝试改回、失败即中止推送）。
3. 节流建议：每日 ≤1–2 次或按需。**v0.3 存储格式已落地（0.3.0）**：会话原样存文本、**>12MB 自动分片**（0.4.26），每轮入历史的是「真实新增 ＋ 变动分片」而不是整份文件。**但别把它读成「体积无顾虑」**：容量仍随内容增长与末片重写上升，必须按 `references/generations.md` 的「容量巡检」定期量 `churn-report.py` 与远端 `.size`，到**该文件「触发条件」那四条**任一成立就换代（判据只在那里，别在别处另写数字）——2026-10-24 就是因为把这句当成「已解决」而漏看了 150MB/小时的暴涨。
**另外记住：`churn-report.py` 量的是「新增 blob 字节」，不是仓体积增长**（git 会对整份重写的文件做 delta）；判容量一律看 `gh api … --jq .size`。

## 自动同步（可选；走官方 cron 的纯脚本任务）

把同步挂成 Hermes 官方 cron 的 watchdog 型任务（`--no-agent`：不过模型、不花 token，只投递脚本输出）：

1. 启动器：本技能随附 `scripts/hb-sentinel.py`（哨兵）与 `scripts/hb-autosync.py`（周期）两枚参考实现——复制到 `HERMES_HOME\scripts\`（cron 脚本**只允许**放在该目录内；`.py` 由官方托管 Python 执行）。脚本自带 git PATH 摆正、防撞锁与 20 分钟新鲜度闸；**成功＝空输出（静默）**，失败输出错误摘要（被自动投递）。**技能目录是源、`HERMES_HOME\scripts\` 是运行副本：改过这两枚（或 `export_all_profiles.py`）就必须重新复制一次，并逐个 `sha256sum` 比对两侧**——实测换机后运行副本比技能旧了一整版（缺分块渲染、超时 3900 vs 5400），而 cron 与手工轮跑的都是那份旧的。
2. 建两个任务（哨兵 2 分钟＋周期 2 小时；完整命令见 `references/auto-sync.md`「安装」节）：`hermes cron create "2m" --name "hermes-backup 哨兵" --script hb-sentinel.py --no-agent --deliver local` 与 `hermes cron create "every 2h" --name "hermes-backup 周期同步" --script hb-autosync.py --no-agent --deliver local`。建好后用 `hermes cron run <任务id>` 当场触发核对——注意 **`cron run` 会跟随脚本跑完（同步一轮通常 5–10 分钟（大重排轮可达 20 分钟））**：请放宽超时或用 background，勿中途掐掉（掐掉会留孤儿锁＋未推提交；v3.2 起由下一轮自动接管并补推，零丢失）。
   **从备份恢复回来的任务多半是停用态，而 `cron list` 会骗你**：`cron/jobs.json` 里 `enabled: false` 的两条，
   `hermes cron list` 仍可能显示 `[active]`（显示层读的是另一份字段）；官方开启动作是
   **`hermes cron resume <id>`**——`hermes cron edit` 根本没有 `--enable` 这个开关。三类证据分开取：
   `jobs.json` 的 `enabled` 变 True（策略层）、`cron list` 的 Next run 有值（调度层）、
   `cron run` 返回 succeeded（脚本层）；**真正在跳只看 `executions.db` 里该 job_id 带新时间戳的行**，
   其中 `source=builtin` 是 ticker 自己触发、`source=direct` 是你手动跑的那次——拿 direct 行证明「自动同步在跑」
   等于自己骗自己。
3. 官方语义：**错过自动补跑**（`cron.catch_up_missed` 默认开；多次错过合并补跑一次）⇒ 等效「每次开机/网关重启后补一轮 ＋ 运行中周期刷新」；调度器随网关运行（`hermes cron status` 看宿主与心跳）；脚本单轮超时可调（`HERMES_CRON_SCRIPT_TIMEOUT` 或 `cron.script_timeout_seconds`），按同步 4–6 分钟一轮留足余量。
4. **并发前提**：多触发源（手动＋自动）并存前，先给 `sync.py` 补**单实例锁**——两轮同步相撞会互踩镜像仓。（**已落地**：v0.3 起 `sync.py` 自带防撞锁，后来者自动让路。）
5. **目标形态（四机制）**：①网关/Hermes 启动后 ②全部静默 ≥N 分钟 ③每 2 小时 ④长静默（≥1h）结束即触发——①②④ 由一个哨兵轮询脚本统一实现（Hermes 无启动/会话原生钩子，条件触发只能轮询；静默判据＝`gateway_state.json`＋`processes.json`＋`session_turn_leases`＋`async_delegations`＋`messages.max(timestamp)` 复合）。
6. **实施顺序**：v0.3 存储格式＋防撞锁（**均已落地并实测**）→ 安装哨兵与两任务（命令见 `references/auto-sync.md`「安装」节）。判定表、信号源与坑 → 同文档。

## 流程 B：新机恢复（目标机）

**严格按 `templates/RESTORE.md` 的编号步骤执行**。要点顺序（此顺序有依赖，勿打乱）：

1. clone 备份仓到本机（`git clone git@github.com:<owner>/hermes-backup.git ~/hermes-backup`）。
2. 文件层覆盖（含覆盖前自动备份旧件到 `restore-out/pre-restore-backup/`）。
3. 会话层导入（走官方 `SessionDB.import_sessions` 管线；本工具进程内放宽官方导入尺寸上限，以恢复含超大档在内的全部历史；已存在的会话自动跳过，可重复跑）。
4. 路由回写（微信/QQ 线程 → 会话映射）。
5. 桌面端收尾器（**唯一需要用户配合的一步：完全退出桌面端一次**；收尾器自动完成剩余拷贝、重启网关、重新拉起桌面端）。
6. 重启后按 RESTORE.md 的验收清单逐项核对。


### 已决事项（2026-10-25，勿再当"待拍板"重提）
- **镜像独有的孤儿会话不导回活库**：活库那边删掉是既成状态，备份的职责是"能随时取回"，不是替主人改会话列表。
  取回路径＝`references/../scripts/restore_sessions.py --source <文件>`（`RESTORE.md` 第 8 节），需要哪条取哪条。
  **这条必须在执行前闸住，光写在文档里拦不住人**：`restore_sessions.py` 的默认行为就是「遍历镜像、把活库没有的 id 全部导入」，
  所以 `--all` 会把用户刚删的每一会话原样灌回（实测一轮 `imported=2216`，与刚被删的 2,215 个一一对上）。四条执行纪律：
  ① `--all` 只给换机/灾难恢复用；日常恢复一律 `--source <只放目标 jsonl 的目录>`；
  ② 跑之前先算「镜像 id 集合 − 活库 id 集合」，把差额贴给用户确认——那些不是「缺失待补」，而很可能就是「他刚删的」；
  ③真删的会话必须连带清镜像（删 jsonl 并 commit），否则镜像永远是恢复源，删多少次都能捞回来；顺序是
  「等该会话的活跃租约结束（正在回答时删除会被 `exclude_active_write_guards` 拒掉）→ 删 → 跑一轮 sync
  让 `prune_stale` 剪掉 `sessions/<档>/<sid>.part-*.jsonl` 并推送」，只删库不跑那一轮，下一次恢复照样复活。
  **恢复源不止镜像一处**：`restore_sessions.py --source <目录>` 指到哪，哪就是恢复源——别的会话做恢复演练
  留下的 `temp/*/restore-src-*`、`migration-staging-*`、`hcb-*/src-main` 这类目录里同样躺着完整的 jsonl，
  删完活库和镜像仍会被它们灌回（实测：删掉的会话「又回来了」，`started_at` 与原值逐位相同＝整条还原）。
  判据：把活库 id 集合与**磁盘上所有** `<id>.jsonl`、`<id>.part-*.jsonl` 的文件名取交集，**交集为 0 才算删干净**；
  逐份副本清还是留着由主人定（那些目录属于别的对话时按红线只报不动）。会话 id 三种形态都要认：
  `YYYYMMDD_HHMMSS_hex`、`cron_<jobid>_<ts>`、`local-<64位hex>`（`source=local`）——按 id 写正则时漏掉第三种，
  这些会话会在清点里凭空消失，删也删不到。
  ④ 归档（`archived=1`）不受影响——官方 import 见 id 已存在就跳过，所以「想让会话消失」的首选是归档不是硬删。
  删除/归档/租约侧的做法见另一个技能 `hermes-mechanics` 的会话库参考（L 篇）与其中的 `session-ctl.py`。
- **网关计划任务不加"进程死了自动拉"**：官方任务无重启策略是设计；实测加 `TimeTrigger` 每 10 分钟拉一次会空烧
  （冷启动 300–900s，10 小时 19 次，并把活网关的事件循环饿死过一次）。判活靠 `hermes gateway status` 巡检。
- **`leveldb` / `.curator_backups` / `cron/output` 保留在备份范围**：合计约 10–15MB/轮（leveldb 4–5、curator 4–8、cron 1–2），属噪音级；
  为省这点字节牺牲"删掉的还能找回来"的完整性不划算（判据是内容有没有用，不是体积）。

## 流程 B′：选择性恢复（用户要挑 / 环境还能跑）

用户说「先别全恢复」「让我选」「只把聊天记录拿回来」，或**备份来自同一台机器的较早时刻**（不是全新机）时
走这条。配方与对照表 → [`references/selective-restore.md`](references/selective-restore.md)；整机恢复仍按
`templates/RESTORE.md`。

四条铁律：

1. **先 clone 到本地，再谈方案。** 没看到实体文件就给「计划」是空谈——体积、档数、会话数、配置差异全在仓里，
   `git clone --depth 1` 之后一条 `du`/`ls` 就有真数。用 API 列目录时注意 `git/trees?recursive=1` 的
   `entries` **包含目录**：`17940 entries` 可能只对应 `14846` 个文件，别把目录数当文件数报出去。

   **「恢复那个备份」= 恢复那个 commit，不是恢复工作树。** 镜像目录是活的：本机后续每轮同步都会往里写
   新的**未跟踪**文件（`git status --short` 几百条、`git ls-files --error-unmatch <path>` 报 UNTRACKED），
   直接拿工作树当数据源会把「不在那个快照里」的会话混进来，而用户要的往往正是「某时刻那一版」。
   判定用户指哪个快照看 commit 信息里的 `sync <日期> host=<机器>`；取数一律走 git 对象层：
   `git ls-tree -r HEAD sessions/<档>` 清单、`git archive HEAD sessions/<档>` 导出、单条用
   `git show HEAD:sessions/<档>/<文件>`——然后把导出目录当 `--source` 喂给 `restore_sessions.py`。
2. **给可选清单，不给执行流水账。** 每项带 ID、体积、会话数、一句话说明，外加 2–3 个打包套餐
   （精简 / 标准 / 全量）＋推荐项，让用户点 ID。九步命令表是执行阶段的产物，不是决策界面。
3. **配置文件逐键合并，绝不整份覆盖。** `config.yaml` / `.env` / `auth.json` 各有各的坑，见配方里的对照表。
   判据一句话：**恢复完 Hermes 必须还能出词、还能登录。**
4. **用户没说「可以恢复」之前一行都不写。** 讨论阶段只做只读检查（`--dry-run`、`git status`、语义 diff），
   把待拍板项列成问题交回去；每次「确认」都要落到具体动作上，别自己替他决定。

## Pitfalls

**实战坑全文在 [`references/pitfalls.md`](references/pitfalls.md)**（按主题分组：会话与分片 / 导出与采集 / git 与推送 / 恢复与迁移 / 哨兵与自愈 / 运行环境与并发 / 其它；**条数随修随增，文档里不写死数字**）。排查先看那一份，这里只留最常咬人的几条：

- **会话是「一行=一个会话」**：头部统计字段每轮都变 ⇒ 必须靠 `session_layout` 的**尾部稳定化**（反向白名单）＋**固定偏移**分片（12MB，边界不得随总长浮动），否则每轮重写整批分片。
- **「备份被清空」比「0 产出」更隐蔽**：新机/重装后若先跑同步（而不是先恢复），活库只有几十个会话，
  采集会把备份里成百上千个会话文件、整个 `profiles-overlay/` 当残留删掉，然后**正常** commit+push——
  下一轮起备份就只剩新机那点东西。零会话闸只看「0 个」，拦不住「有一点但极少」。现在由
  `shrink_guard()` 在采集前拦（活库 < 备份 50% 且备份 ≥20 个会话 ⇒ exit 6、镜像不动）；
  两个 cron 包装器把 exit 6 当**主动拒绝**而不是故障——提示一次、写标记、24h 内静默且不记失败计数。
- **「0 产出」必须当失败处理**：`rc=0` 且 0 个会话 **≠ 成功**——只要该档历史上已有文件，就判失败、旧文件全部保留、本轮不许 prune 它（`guard_zero_sessions()`，回归 `test-zero-session.py` 5 项），整轮记 **`exit 5`** 而不是 0（其它档照常提交推送，健康计数照记）。实案：一轮 `export main: sessions=0 files=0`（内存不足）把 `sessions/main` 的 1,470 个会话文件整目录删掉并推送，整轮还报 exit 0。`prune_stale(..., allow_delete=…)` 只删「已确认活库里不存在」的档。
- **导出内存：整档渲染会 MemoryError，必须分块**：`render_sessions_export(全部会话)` 会把整档拼成一个字符串（main 约 1.6GB）；改成按**同一份 `sessions` 列表**切块（`CHUNK=4`）逐块写，输出与整档渲染逐字节等价（sha256 实测一致）。**不要**改成「按 id 逐个 `export_session()`」——`export_all()` 会对 `tool_names` 等字段做后处理（哈希），两者 sha256 不同。
- **导出超时按「有没有进展」判，不按固定墙钟**：机器被别的活占满时同一轮导出能从 210s 涨到 1200s+（固定 20 分钟预算会掐死正在干活的导出，回退逐档 CLI 反而更慢）。改成每 30s 看**进程 IO**（不能看输出目录——整档渲染完才落盘，目录一直是空的），涨就续期，连续 1200s 不涨才杀整树，硬上限 3600s。
- **私有性检查 fail-closed**：属主解析不到 / `gh` 不可用 / 查询失败 ⇒ **拒绝推送**（显式 `--allow-unverified-privacy` 才放行）；这个仓里是凭据与私人对话。
- **`push` 报 `ERROR: Repository not found` 而 `ssh -T git@github.com` 认证正常 ⇒ 远端仓已被删或改名**，不是凭据/网络问题。判据：同 URL 的 `git ls-remote` 也报 not found，而 `Hi <user>!` 正常返回，两者并存即可定案。纯本机模式下这条 remote 本来就不用，`git remote remove origin` 即可收口；不要据此怀疑 token 失效、更不要重配凭据。
- **恢复脚本必须经 `run_in_runtime.py`**：直接 `python restore_sessions.py` 会因缺运行期依赖报 `ModuleNotFoundError: ruamel`。
- **恢复时「0 个会话」= 失败**（rc=2）：指错 `--source` 不该安静成功。
- **导入成功 ≠ 能续接**：导入器不写 `session_key`/`chat_id`/`chat_type`/`origin_json`，平台因此认不出旧线程；
  按来源 JSON 原值回填即可（分片要先拼成完整 JSON）。有 `session_key` 时网关会自己重建 routing，
  `apply_routing.py` 可跳过、不必为此停网关。详见 `references/selective-restore.md` §7。
- **通道 `connected` ≠ 收得到消息**：`dm_policy=pairing` 的放行名单是**独立文件**
  `<HERMES_HOME>/platforms/pairing/<platform>-approved.json`，不随 `--what home,profiles` 一起恢复；
  缺了这一目录，消息照样收进来、却被静默丢弃（`WARNING gateway.run: Unauthorized user: <id> on <platform>`），
  而 `gateway_state.json` 仍报 `connected`，也不会建任何会话。判「通道通不通」看有没有 `Unauthorized user` 行，
  不看 `connected`；修法 ＝ 把备份 `home-overlay/platforms/pairing/*.json` 拷进去（**无需重启网关**，
  缓存按文件签名失效），再用 `hermes pairing list` 核对 Approved Users。详见 §7.3.1。
- **巨会话「能看到尾巴、往上翻不出来」先量再判版本**：量 active/归档行数、后端分页耗时、
  `sessions.max_resume_messages` 闸门；后端清白就不要降级——根因常在桌面端显示投影（无 LIMIT 全量）+ 翻页状态只在 REST 路径记录。
  可逆处置＝把归档行迁到独立归档会话（`scripts/split-archive-session.py`）——**只降体量，不等于 UI 修好**：
  拆完要看 `logs/desktop.log` 里有没有 `offset>0` 的 `/messages` 请求才算翻页真的发生，实测拆完重启后用户仍报原样。同上 §7.4。
- **批量导出缺 `export_all_profiles.py` 会静默变慢 3–5 倍**；`HERMES_HOME/scripts/sync.py` 若存在只能是**转调壳**，不能是副本。
- **镜像里出现 gitlink（`mode 160000`）＝那个目录只备了一个指针，没备文件。** 被备份的目录自身若是 git 仓（技能目录就是），整目录拷进镜像时 git 会把它记成子模块指针；换机恢复时该路径是**空的**，等于没备份，而 `git status` 只显示一行 ` M`，看不出问题。`copy_tree` 一律跳过嵌套 `.git`；体检：`git -C <镜像> ls-files -s | awk '$1=="160000"'` 必须为空，非空就 `git rm --cached <路径>` 后按文件重新入库。
- **`cron` 的 `completed` 只证明包装器退出 0，不证明它做了活。** 新鲜度闸（距上次成功 <20 分钟）与 shrink-guard 静默标记都会让它**静默 return 0、零输出**——cron 表里一切正常，而备份已经几小时没动。判「同步真的在跑」只认 `state/hb-sync-runs.jsonl` 出现新行 + 镜像 HEAD 前进；并且**任何一轮成功都要清掉 `state/hb-sync-guard-blocked.json`**，否则手动修好了也会被 24 小时静默期压住。
- **哨兵看锁的活性**（读 pid），死锁不该永久挡住补跑；`overdue` 另有 `overdue-no-state` 覆盖「从未成功过」。
- **判容量看 `gh api … --jq .size`**，`churn-report.py` 量的是「新增 blob 字节」，不是仓体积增长。
- **换代**：判据是「常规轮 push >10 分钟 / 克隆 >15 分钟 / `.size` >8GB / 历史占比 >20%」，**不是「体积到 12GB」**；孤儿快照**分批推送**（单次 push 上限 2GB）、旧仓 `archived=true`。换算不了：`.size` ≈ 树（历史只多 1.6%），换代不会重置树的增长。
- **`restore_files.py` 只能按层选**（`--what home,profiles`），没有逐单元开关：要按单元恢复就先做「筛选过的暂存仓」——拷 `home-overlay/` ＋ `profiles-overlay/` 到 `~/restore-stage/`，删掉不恢复的那几个文件，`--repo` 指它。这样 clone 保持干净，`pre-restore-backup/` 照常生效。
- **被网关打开的 SQLite 不能直接 cp 覆盖**（`shared-state.db` / `kanban.db` / `projects.db`）：进程持有句柄时覆盖会撕裂。跳过，或停网关后再补——`state.db` 本来就不在 overlay 里，会话一律走导入器。
- **补历史会话不要动 `state.db` 文件本身**：`hermes serve`（桌面后端）与网关都持有它，而 agent 自己就跑在 `hermes serve` 的进程树里 ⇒ 「换库」等于杀掉执行者，后面没人收尾；Windows 上还会先撞共享冲突，就算绕过也会被 `StateDbReplacedError` / `DeletedWalGenerationError` 拒写。正确做法＝ATTACH 源库、**向活库 INSERT** 目标会话，按 3000 行/事务提交（写连接 `timeout=1.0`，锁超过 1 秒就会掐断正在跑的对话）。完整约束与验收 SQL → `references/selective-restore.md` §8.5。
- **`auth.json` 整份覆盖＝丢当前登录**：它是凭据池（`providers` / `credential_pool`），本机那份常比备份新得多（实测 11.5KB vs 940B）。恢复前先比 `providers` 的键，缺就跳过。
- **`hooks.*.command` 指向的脚本可能已经不存在**（备份里存的是绝对路径，常指向 `workspace/temp/…` 这类临时目录）→ 每个用户回合都去跑一个不存在的命令。恢复前逐条 `test -f`，路径失效就改成技能目录里的副本。
- **别用 mtime 判断「最后改动」**：`git clone` 把所有文件 mtime 刷成 checkout 时间，清单里会显示「全部同一分钟」。新鲜度只认 commit 时间。

## 更新 / 重启后自检（约 5 分钟；官方版本变了必跑）

顺序固定，全部看完再下结论：

1. `hermes --version` 确认新版本与 commit；`tail logs/update.log` 看更新是否真的收尾（是否重启过网关）。
2. 网关：`gateway_state.json` 的 mtime 新鲜度 + `pid` + `platforms.*.state`（`weixin`/`qqbot` 应为 connected）+ 进程表里有且只有一个 `hermes_cli.main gatew*`。**`gateway_state.json` 只在 default home 一份**（多档共用一个 multiplexer）。
3. **`ESTOP` 文件在不在**（`%LOCALAPPDATA%/hermes/ESTOP`）：`hermes pause` 跨重启保留，会让 cron 静默停摆而网关看起来一切正常；在就 `hermes resume`。
4. cron 是否还在跳：读 `cron/executions.db` 最近几行（哨兵应每 2–3 分钟一条 `completed`），不要只信 `cron list` 的 Last Run。
5. 同步线：锁文件在不在 + `state/hb-sync-runs.jsonl` 最后一行（`committed` 为 true 即算跑过；纯本机模式 `pushed` 是 null）
   + 镜像 HEAD 是否前进；健康文件 `consecutive_failures`。只有 `--push` 轮才核 `git ls-remote`。**更新后跑满一整天再判断稳定性**——更新期间那几轮会因资源争用变慢（实测 export 1181s / git 1367s），不是机制坏了。
6. 环境漂移：`hermes update` 会换 tools python（实测 3.14.7→3.14.9）并换出旧包——`python -c "import psutil"` 之类可选依赖要复验；`HERMES_HOME/scripts/` 里的运行副本与技能副本用 sha256 比对（更新不应覆盖，但要证）。**同时复核网关启动器**（`gateway-service/Hermes_Gateway.vbs`/`.cmd`）：它们内嵌安装时的解释器绝对路径，换 python 后路径失效 → 计划任务照报 `Last Result 0` 但网关根本没起来（`hermes gateway status` 报 `No gateway process detected`）；修法是 `hermes gateway install --start-on-login --no-start-now` 重生成启动器再 `schtasks /Run`，详见 `windows-autostart-ops`。
7. 配置仍在：`hermes config get sessions.max_export_messages` 应为 `0`（否则巨会话导出会整包失败且 rc=0 无产物）。**新机从备份恢复后这个键常回默认 `20000`**——不是「更新弄丢了」，而是恢复只覆盖被备份过的键；见到 `SessionExportTooLargeError … more than 20,000 exportable messages` 就是它。
8. **恢复链路复验（最要紧）**：跑 `references/restore-drill.md` 的「低成本冒烟」两例（小档整档 + 一个巨会话），确认官方导入 API / schema 变动没有破坏恢复。

## Verification

- **回归总闸：改过存储格式 / 采集白名单 / 审计器 / kanban / 采集前闸门任一处，就把 `scripts/test-*.py` 全跑一遍再收工**（以 `ls scripts/test-*.py | wc -l` 为准；纯标准库、各自不碰真库）：
  现有 `test-layout.py` · `test-split.py`(阈值直读 `sync.PART_MAX`) · `test-audit-coverage.py` · `test-hb-sentinel.py` · `test-hb-health.py` · `test-kanban-snapshot.py` · `test-kanban-scope.py` · `test-reap-exporters.py` · `test-prune-protect.py` · `test-zero-session.py` · `test-shrink-guard.py` · `test-commit-guards.py`。一条命令跑全：`scripts/selfcheck.py`。
  校验判据是**每套自己打印的「N 项，失败 0」**，不是 rc=0（脚本可能在断言前就因语法/路径错退出）。改阈值类常量时，测试要读运行时值（`sync.PART_MAX`）而不是抄一个字面量——两处各写一个数就会「测试绿、生产红」的反向事故。
- **`--collect-only` 不是快速探针**：它只跳过 git 段，**导出照跑**（本机实测约 10 分钟）——想验证采集正确性（白名单、快照、prune）值得跑，想「看一眼状态」就别用它。跑完镜像里是未提交改动，别当成异常；下一轮会连带提交。
- 同步后：纯本机模式看 `meta/sync-state.json` 的 `finished_at` 是否刷新 + 镜像 HEAD 是否前进（`pushed` 为 `null`
  是正常值）；只有 `--push` 的轮次才看 `pushed` 为 true 且 `git ls-remote origin` 与本地提交一致。
- **恢复完整性要看三个数，不只看会话数**：`sessions` 条数、`messages` 条数、有消息的会话数。后两者对不上时按两条判：
  ①**重复消息**＝`select session_id,message_uid,count(*) c from messages where message_uid is not null group by 1,2 having c>1`
  （>0 说明同一会话被导入了不止一次——并发恢复、或合并脚本重跑）；
  ②**空会话壳**＝`select count(*) from sessions s where not exists(select 1 from messages m where m.session_id=s.id)`
  （会话行在、消息没进来，常见于合并脚本先建壳后填内容、中途被打断）。
  **先报数不动手**：确认没有别的进程还在写活库（看 `state.db-wal` 的 mtime），且 `count(*)` 连续 ~15 分钟不变，再修；否则修完又被覆盖。
- **空会话壳先回源核对，别急着补导**：拿镜像里该会话的 JSONL 数一遍 `len(json.loads(line).get("messages") or [])`，
  **镜像也是 0** ⇒ 它本来就是空壳（`source=subagent` / `gui` 的会话常有），不需要任何回填；只有「镜像有内容、活库为空」
  才值得补，补的时候仍走 `--source <只放这些文件的目录>`，绝不 `--all`。
- **重复消息的修法**（同 `(session_id, message_uid)` 多份 ⇒ 并发导入/合并脚本重跑）：**按最老 id 保留一份**，分块删，
  全程低内存——①先快照（`sqlite3` 的 `backup()`，页级拷贝、内存恒定，别 `cp` WAL 库）；②每事务 2,000 行：
  `delete from messages where id in (select id from messages m where m.session_id=? and m.message_uid is not null
   and exists(select 1 from messages m2 where m2.session_id=m.session_id and m2.message_uid=m.message_uid and m2.id<m.id) limit 2000)`；
  ③`message_uid` 为 NULL 的行一律不动（它们无法判重）；④`messages_fts*` 的 delete 触发器在位时会自己跟随，
  删完跑 `pragma quick_check` + `insert into messages_fts(messages_fts) values('integrity-check')`。
  先在合成小库上把这段 SQL 跑一遍断言「每组只留最早一行」，再碰真库。
- **别在别人还在写的时候去重**：合并/导入进程常以每秒一条的速度长跑，期间重复行会**继续增长**；盯着
  `select count(*) from messages` 直到静默，或把「等静默→去重→校验」做成一个可中断的等待器（带 3 小时兜底），
  而不是占着回合空转。
- **「让路」的一轮也是 exit 0，它一个字节都没同步。** 防撞锁命中时 `sync.py` 打一行
  `已有另一轮同步在进行（锁年龄 Ns）——本轮退出，让路` 然后 **rc=0** 退出。只看退出码会把
  「什么都没干」记成「同步成功」。判据是**日志首行 + 证据**：先读那行日志，再看 `meta/sync-state.json`
  的 `finished_at` 有没有刷新、`committed` 是否为 true（纯本机模式下 `pushed` 为 null，不是故障）、镜像 HEAD 是否前进；只有 `--push` 轮次才核 `git ls-remote`。
  同理，`state/hb-sync-runs.jsonl` 里 `committed`/`pushed` 为 `null` 的行是**让路/空转轮**，不是失败——
  别把它们统计进失败率。碰上别人正在跑时**不要杀它、不要删锁**，等它收工再按上面三条验收；
  同一台机上常有另一个会话在做同一件事（换机后的「跑通」任务尤其容易撞）。
- **stdout 末尾没有「sync 完成」不等于失败**：输出被 `| tail` 或包装器截断时，最后一行可能停在「git 推送完成」。以 `state/hb-sync-runs.jsonl` 的 `finished_at` / `pushed` 与 `git rev-parse HEAD == git ls-remote origin refs/heads/main` 为准；不要因为日志尾缺失就重跑一轮。
- **验收必须在与 cron 相同的环境里跑，不许给自己加戏。** 用 `GIT_CONFIG_*` / `http.curloptResolve`
  把 `github.com` 钉到某个 IP、或临时挂代理之后跑通的同步，**证明不了 cron 能跑**——cron 的包装器
  不带你这些变量。先裸跑一次（`git ls-remote origin refs/heads/main` + `ssh -T git@github.com` 各一条就够判
  网络与凭据），失败就把原文错误贴出来分类（认证 / DNS / 443 被拦），别自己发明绕路。
- **每轮结构化记录**：`$HERMES_HOME/state/hb-sync-runs.jsonl` 一行一轮（时长／分段耗时 collect·export·git／退出码／committed·pushed／字节数）——追轮次历史、算成功率与分段成本先读它；连续失败计数在 `state/hb-sync-health.json`，≥3 轮失败写 `state/hb-sync-needs-attention` 标记（不推送、不打扰，成功一轮自动清）。
- 恢复后：置顶对话在；微信/QQ 发消息能续接旧线程；群聊记录在；各档 `hermes -p <档> sessions stats` 数量与备份一致；主档出词正常（provider 可用）。
- 全量验收入口：`templates/RESTORE.md` 第 6 节清单。
- **全量审计（大改后或定期）**：四路核对（维护笔记③）＋版本字符串全扫（如 `grep -rn '0\.3\.' <技能目录>`，捕捉状态行/示例漂移）＋点火一轮真同步核**镜像 HEAD 前进**（纯本机模式不看 `ls-remote`）；线上设置与隐私面 → `github-security-audit` 技能。四路自查的完整清单（含每条的 grep 与判据）→ `references/audit-playbook.md`。
- **完整性＝两条腿，缺一条都不算数**：①**演练**（镜像 → 沙箱，证明"能恢复"）②**覆盖率审计**（活库 → 镜像，证明"备的是全部"）。审计默认只审会话层；**要连文件层（`--with-files`）一起跑**才算覆盖配置/记忆/技能/平台数据那一半。判据一句话：缺会话／镜像条数超前／会话文件解析失败＝**硬伤**（退出码 1）；**多会话＝警告**（0.4.48 起）、**落后＝正常增量**、孤立消息组＝活库自身残留，均不计硬伤。
- **审计的两类"非硬伤"要会读，别急着当漏备**：①**文件层轮内新建豁免**（0.4.13）——白名单目录/文件在镜像缺失，但源内容 mtime 晚于本轮 `started_at` 时，判「轮内新建、下一轮会采」，输出在 `· 轮内新建…` 一行、不计硬伤（实案：`desktop/interrupted_turns.json` 由桌面端在轮后写入，被误判为漏备）；②**镜像独有会话**（审计报「多会话」）——通常是活库被外部事件换过（state.db 回滚/还原、手工清理），不是漏备也不是采集错：下一轮会从最新树 prune，**git 历史仍保留**；要留档先另存文件（含分片），要导回用 `git show <sha>:sessions/main/<sid>.jsonl` 取出后走 `--source` 导入。看到「多会话」先问「活库最近是否被回滚过」，别从活库删东西。
- **「镜像多会话」只警告，不判硬伤**（0.4.48）：子代理临时会话（`Subagent: …`）、人工删除、库回滚都会让镜像比活库多；
  这类是「备份比活库全」，不是备份缺陷——原先判硬伤会导致每次跑都 AUDIT-FAIL，把真信号淹没（2026-10-26 实测 5 个 `Subagent:` 会话）。
  现在 `AUDIT-PASS` 只由三种硬伤决定：镜像缺会话 / 镜像条数多于活库（错位或回滚）/ 会话文件解析失败；多会话单列「警告」并附例子。
- **`AUDIT-SKIPPED` 是口径问题，不是备份缺陷。** 单档（只有 default）的机器上，镜像里仍躺着十几个退役档的会话目录，
  而活库没有这些档 ⇒ 审计会把它们整批判成「活库缺失被跳过」，末尾不输出 `AUDIT-PASS` 而输出 `AUDIT-SKIPPED`
  并附带档名列表。那是**扫描范围没限定**，不是“备份缺了 16 个档”：带 `--profile main` 重跑即 PASS。
  反向判据：真缺陷的形态是 `main` 这一档里缺会话／条数错位／解析失败，与“哪个档根本不存在”无关。
- **审计 PASS 必须与负向对照成对出现**：改了审计器或采集白名单后跑 `scripts/test-audit-coverage.py`（8 场景合成夹具，断言"该红的会红"）。只有 PASS 没有负向对照＝**没有证据**（审计可能根本没在看）；断言要落在输出字段（`缺 N`／`新建豁免 N`／`超前 N`），只断言退出码会把"因别的原因失败"误当通过。
- **验收以「最后同步时刻」为界**：快照之后的新会话/改动不在备份里（属预期，下轮同步带上）。对账做三向差集——真机∩快照（应精确一致）、真机−快照（＝同步后新建）、快照−真机（＝曾删除；快照保留历史）；给用户解释时先讲这层边界，避免把预期差异当故障。
- **时间锚点**：报「损失窗口/新鲜度」前先实测当前时间（`date`）并按当下重算——隔夜或隔数轮后旧读数的时间锚（如「今晚」）即失效，禁止沿用旧时点表述。
- **沙箱演示/验收**：在 `temp` 下克隆＋恢复（`--home` 指向沙箱），全程不动真机；大件会话（>6MB）可先改名加 `.hold` 后缀暂缓，中小件先跑完出对账，再恢复原名单独补入（总量不变）。
- **恢复侧验收三步**：①**冷启动冒烟**（全新 `--depth 1` 克隆 ＋ 仓内自带工具恢复一档 ＋ 直读 sqlite 独立核对，约 15 分钟——专抓「本机热环境永远暴露不了」的文档路径错，适合派子代理做）②③两遍独立全量演练、均 0 差异（第二遍必须换仓状态＋换工具来源，不可省）。配方、对账口径与判读坑 → `references/restore-drill.md`。
