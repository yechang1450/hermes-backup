# 自查手册：四路审计（文档 / 可跑性 / 代码 / 公开面）

改过存储格式、切片阈值、采集白名单、换代脚本之后；发布新版本之前；以及定期——跑这四路。
每路都要给出**可核验的输出**（命令 + 读数），不许「看着没问题」。

## 路线 A：文档 ↔ 代码漂移

文档里的每个数字都必须能对回代码常量；对不上就是缺陷（读的人会照着做错事）。

**先立一条通则：可增长的数量（几条坑 / 几套测试 / 几个场景 / 几个脚本）一律不写死在文档里。**
写死就等于给自己排一张必然过期的卡——每加一条、每加一套，就要回来改 SKILL、改 references、改 README 三处，
而漏改了也无人报错。要么写「以 `ls scripts/test-*.py | wc -l` 为准」，要么只说「跑通即看末行判据」。
只有**代码常量**（分片阈值、超时秒数、体积闸）才是该写死的数——那也是因为能对回 `sync.py` 里的同一行。

| 查什么 | 怎么查 | 判据 |
|---|---|---|
| 阶段预算/超时 | `grep -n "TIMEOUT\|timeout=" scripts/sync.py scripts/hb-autosync.py` | 与 `references/auto-sync.md`、SKILL.md 里写的秒数一致 |
| 分片阈值 | `grep -n "PART_MAX" scripts/sync.py` | 文档里的 12MB 与常量一致；历史值只允许出现在「阈值演进」叙述里 |
| 换代阈值 | `grep -n "12,000,000\|12GB" references/generations.md` | 全文只剩 12GB 一处现值，旧值仅在「为什么上调」段落 |
| 场景数 / 套数 | `ls scripts/test-*.py | wc -l`、`grep -c '== S' scripts/test-hb-sentinel.py` | **文档不得写死数量**（只写「以 ls 为准」或末行判据）；若已写死，拿实测值去改掉它 |
| 命令模板 | 逐条把文档里的命令在克隆里试跑（`--help` 级别） | 路径真实存在、参数被接受 |
| 版本号 | `grep -rn "0\.4\." SKILL.md references/ | head` | 现值只在 frontmatter 与提交信息；其余只作历史叙述 |

**手维护件最容易过期**：镜像根 `README.md` 与 `tools/README.md` **不由 sync 生成**（sync_tools 只复制 `TOOL_FILES` 白名单里的脚本 + SKILL.md + RESTORE.md，且**不 prune**），改格式/加了脚本必须手改——实见两处同时过期（根 README 仍写 >90MB 切片、tools/README 仍写「8 个脚本」）。

## 路线 B：契约可跑性（一律在全新克隆里做）

```bash
git clone --depth 1 <技能仓> <temp>/repo
cd <temp>/repo && for f in scripts/*.py; do python "$f" --help; done   # 逐个看 rc 与用法
python scripts/test-split.py; python scripts/test-layout.py; …          # 全部 test-*.py，判据是各自末行「N 项，失败 0」
# 更省事：`python scripts/selfcheck.py` 一条命令跑完编译+全部测试+文档引用+公开面卫生
bash -n scripts/*.sh
```

- **错误路径**：给不存在的 `--profile` / `--source` / `--repo`，要求**可读错误**而不是堆栈崩溃。
- **幂等**：能重复跑的（不碰真数据的测试、恢复脚本对同一沙箱）跑第二遍，仍绿/仍 0 导入。
- **禁止**：对生产镜像写、碰活的 `HERMES_HOME`、push。只读克隆 + 沙箱 `--home`。

## 路线 C：代码级审查

只看能不能构造出**静默失败**，不看风格：

1. 异常被吞掉后仍返回成功（返回值 / rc / 日志说「完成」）；
2. 半截写入、非原子替换（先写临时文件再 `os.replace`）；
3. 锁的获取与释放成对、崩溃后残留、pid 复用误判；
4. 路径拼接能否越界（`..` / 绝对路径注入）、shell 命令拼接的引号；
5. 多分片排序与拼接、缺片检测、`message_uid` 唯一性；
6. 超时/中断后的状态：**能不能把半完成的轮次记成成功**。

## 路线 D：公开面卫生（这个仓是公开的）

```bash
grep -rn 'C:/Users/' scripts/ templates/ references/ SKILL.md   # 必须为空
grep -rniE 'sk-[a-z0-9]{10,}|ghp_|AKIA|BEGIN (RSA|OPENSSH)' .    # 必须为空
git check-ignore -v local.json                                    # 机器本地配置不进仓
```

- 脚本/测试里**禁止**本机绝对路径：既泄露用户名，又让测试换机即败——按 `Path(__file__).resolve().parent` 拼。
- 盘符示例（`~/hermes-backup`）可以保留（是约定俗成的示例），但不得带真实用户名。

## 判读与修法

- **审阅者的结论必须回原文核对**：先看文档有没有定义它误用的那个术语。实案：有审阅者报「SKILL.md 让人跑 `<REPO>/tools/…` 但仓里没有 `tools/`」——表格抬头已定义 `<REPO>`＝**备份仓 clone 目录**（那里确实有 `tools/`），技能仓只有 `scripts/`。正确处置＝补一句「两个 `tools/` 别搞混」，**不是**去改（本来正确的）命令。
- 只修**已验证且可复现**的项；误报记进台账、不改代码去迎合。
- 修完立刻用同一条命令复验，并把「修了什么、判据是什么」写进状态页。

## 收尾

1. 全部回归（或 `selfcheck.py` 一条命令）+ 一次全新克隆复跑；
2. 版本号**最后**改，frontmatter 与提交信息一致；
3. 推后 `git ls-remote origin refs/heads/main` 与本地 HEAD 比对；
4. 镜像侧手维护件（README 等）的改动留给下一轮同步提交，不要手推。
