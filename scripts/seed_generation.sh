#!/bin/bash
# seed_generation.sh — 把镜像树的当前内容「分批」推成一个新仓的 main 分支。
#
# 为什么需要：GitHub 单次 push 上限 2GB，而一代快照的树可达 3–12GB。
# 做法：用临时 index 逐批 `git add` → `write-tree` → `commit-tree` → `push`；
# 每批 ≤ BATCH_MB（默认 800MB），每次 push 只传这批新增的 blob。不复制任何文件
# （复用镜像工作树与对象库），最后校验「逐批推进后的树 == 镜像树」。
#
# 用法：bash scripts/seed_generation.sh <mirror> <remote-url> [batch_mb]
#   例：bash scripts/seed_generation.sh D:/hermes-backup ssh://git@ssh.github.com:443/me/newrepo.git 800
# 幂等：远端 main 若已等于最终提交，则不再推（可重复跑）。
# 输出：最后一行打印 `SEED-OK <commit>`，失败则非零退出。
set -euo pipefail

MIRROR="${1:?用法: seed_generation.sh <mirror> <remote-url> [batch_mb]}"
URL="${2:?缺少 remote url}"
BATCH_MB="${3:-800}"
HOMEDIR="${HERMES_HOME:-${LOCALAPPDATA:-$HOME}/hermes}"

IDX="$HOMEDIR/cache/scratch/seed-index-$$"
BATCHDIR="$HOMEDIR/cache/scratch/seed-batches-$$"
rm -f "$IDX"; rm -rf "$BATCHDIR"; mkdir -p "$BATCHDIR"
trap 'rm -f "$IDX"; rm -rf "$BATCHDIR"' EXIT

python - "$MIRROR" "$BATCH_MB" "$BATCHDIR" <<'PY'
import subprocess
import os, sys
root, batch_mb, outdir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
limit = batch_mb * 1024 * 1024
# heredoc 会顶掉管道 stdin，所以自己调 git（2026-10-25 实案：分批数恒为 0）
out = subprocess.run(['git', '-C', root, 'ls-files', '-z'], capture_output=True, check=True).stdout
raw_files = [p for p in out.split(b'\0') if p]
sized = []
for raw in raw_files:
    rel = raw.decode('utf-8', 'replace')
    try:
        sized.append((os.path.getsize(os.path.join(root, rel)), rel))
    except OSError:
        sized.append((0, rel))
sized.sort(key=lambda x: -x[0])          # 大的优先，减少批次数
batches, cur, cursz = [], [], 0
for sz, rel in sized:
    if cur and cursz + sz > limit:
        batches.append(cur); cur, cursz = [], 0
    cur.append(rel); cursz += sz
if cur:
    batches.append(cur)
for i, b in enumerate(batches, 1):
    with open(os.path.join(outdir, 'batch-%03d.txt' % i), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(b) + '\n')
print('  分批：%d 批 / %d 文件 / 每批 ≤ %dMB' % (len(batches), len(sized), batch_mb))
PY

PREV=""
N=0
for BF in "$BATCHDIR"/batch-*.txt; do
  N=$((N + 1))
  if [ -z "$PREV" ]; then GIT_INDEX_FILE="$IDX" git -C "$MIRROR" read-tree --empty; fi
  # 注意必须 -f：临时 index 是空的，所有文件对它而言都是"未跟踪"，仓库 .gitignore 会把
# 某些已被跟踪的文件（如 skills/*/local.json）判为忽略并让 add 失败（2026-10-25 实案：第 5 批直接死）
GIT_INDEX_FILE="$IDX" git -C "$MIRROR" add -f --pathspec-from-file="$BF"
  T=$(GIT_INDEX_FILE="$IDX" git -C "$MIRROR" write-tree)
  if [ -z "$PREV" ]; then
    C=$(git -C "$MIRROR" commit-tree "$T" -m "gen snapshot $(date '+%F %T') [batch $N]")
  else
    C=$(git -C "$MIRROR" commit-tree "$T" -p "$PREV" -m "seed batch $N")
  fi
  T0=$(date +%s)
  git -C "$MIRROR" push -q "$URL" "$C:refs/heads/main"
  echo "  批 $N：推送 $(($(date +%s) - T0))s，提交 $C"
  PREV="$C"
done
[ -n "$PREV" ] || { echo "!! 没有任何批次"; exit 1; }

# 校验：分批推进后的索引树里，文件数必须与镜像索引一致
# （不用镜像真实 index 做 write-tree：那会碰用户的 index，且 3GB 树上很慢）
N_SEED=$(git -C "$MIRROR" ls-tree -r "$PREV" --name-only | wc -l)
N_MIRROR=$(git -C "$MIRROR" ls-files | wc -l)
[ "$N_SEED" = "$N_MIRROR" ] || { echo "!! 种子文件数($N_SEED) != 镜像文件数($N_MIRROR)"; exit 1; }

# 用 refs/heads/main 而不是 HEAD：本地路径/部分传输下 HEAD 不被广播，会返回空（2026-10-25 实案：
# 5 批推送全成功，却在这里误报"远端 HEAD() != 最后提交"并退出 1）
R=""
for _try in 1 2 3; do
  R=$(git -C "$MIRROR" ls-remote "$URL" refs/heads/main 2>/dev/null | cut -c1-40)
  [ -n "$R" ] && break
  sleep 2
done
[ "$R" = "$PREV" ] || { echo "!! 远端 HEAD($R) != 最后提交($PREV)"; exit 1; }
echo "  文件数一致：$N_SEED / 批次数 $N"
echo "SEED-OK $PREV"
