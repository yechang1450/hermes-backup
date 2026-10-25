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

# ── 第一遍：只算每批的树（不提交），用于"续跑/已就位"判定 ──
TREES=()
N=0
GIT_INDEX_FILE="$IDX" git -C "$MIRROR" read-tree --empty
for BF in "$BATCHDIR"/batch-*.txt; do
  N=$((N + 1))
  GIT_INDEX_FILE="$IDX" git -C "$MIRROR" add -f --pathspec-from-file="$BF"
  TREES+=("$(GIT_INDEX_FILE="$IDX" git -C "$MIRROR" write-tree)")
done
[ "$N" -gt 0 ] || { echo "!! 没有任何批次"; exit 1; }
FINAL_TREE="${TREES[$((N - 1))]}"

R_SHA=$(git -C "$MIRROR" ls-remote "$URL" refs/heads/main 2>/dev/null | cut -f1)
R_TREE=""
if [ -n "$R_SHA" ]; then
  R_TREE=$(git -C "$MIRROR" rev-parse "$R_SHA^{tree}" 2>/dev/null || true)
fi
if [ -n "$R_TREE" ] && [ "$R_TREE" = "$FINAL_TREE" ]; then
  echo "  远端已是完整树（无需推送）"
  echo "  文件数一致：$(git -C "$MIRROR" ls-tree -r "$R_SHA" --name-only | wc -l) / 批次数 $N"
  echo "SEED-OK $R_SHA"
  exit 0
fi
# 远端若是"别的形状"（既不是某批的树、也不是完整树）⇒ 拒绝覆盖，避免把完整备份回退成半棵树
if [ -n "$R_TREE" ]; then
  MATCH=0
  for T in "${TREES[@]}"; do [ "$T" = "$R_TREE" ] && MATCH=1; done
  if [ "$MATCH" = 0 ]; then
    echo "!! 远端 main 的树既不是任何一批、也不是完整树（$R_SHA）——拒绝覆盖，请人工核对"
    exit 1
  fi
fi

# ── 第二遍：从"远端已有的那批"之后继续推 ──
PREV="${R_SHA:-}"
SRC_DATE="$(git -C "$MIRROR" log -1 --format=%cI 2>/dev/null || true)"
[ -n "$SRC_DATE" ] || SRC_DATE="2000-01-01T00:00:00+00:00"
export GIT_AUTHOR_DATE="$SRC_DATE" GIT_COMMITTER_DATE="$SRC_DATE"

i=0
for BF in "$BATCHDIR"/batch-*.txt; do
  T="${TREES[$i]}"
  i=$((i + 1))
  if [ -n "$R_TREE" ] && [ "$R_TREE" = "$T" ]; then
    echo "  批 $i：远端已是同一棵树（续跑跳过）"
    continue
  fi
  if [ -z "$PREV" ]; then
    C=$(git -C "$MIRROR" commit-tree "$T" -m "gen snapshot [batch $i]")
  else
    C=$(git -C "$MIRROR" commit-tree "$T" -p "$PREV" -m "seed batch $i")
  fi
  T0=$(date +%s)
  if ! git -C "$MIRROR" push -q "$URL" "$C:refs/heads/main" 2>"$BATCHDIR/push-$i.err"; then
    echo "!! 批 $i 推送失败：$(head -2 "$BATCHDIR/push-$i.err")"
    exit 1
  fi
  echo "  批 $i：推送 $(($(date +%s) - T0))s，提交 $C"
  PREV="$C"
done
[ -n "$PREV" ] || { echo "!! 没有任何提交"; exit 1; }
COMMIT="$PREV"

N_SEED=$(git -C "$MIRROR" ls-tree -r "$COMMIT" --name-only | wc -l)
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
[ "$R" = "$COMMIT" ] || { echo "!! 远端 main($R) != 最后提交($COMMIT)"; exit 1; }
echo "  文件数一致：$N_SEED / 批次数 $N"
echo "SEED-OK $COMMIT"
