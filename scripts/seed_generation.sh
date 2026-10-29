#!/bin/bash
# seed_generation.sh — 把镜像「HEAD 的那棵树」分批推成一个新仓的 main 分支。
#
# 为什么需要：GitHub 单次 push 上限 2GB，而一代快照的树可达 3–12GB。
# 做法：用临时 index 逐批组装树（**取自 git 索引/HEAD，不读工作树**）→ write-tree →
#       commit-tree → push；每批 ≤ BATCH_MB（默认 800MB），每次 push 只传这批新增的 blob。
#
# 为什么读 HEAD 而不是读文件：桌面端 leveldb 一直在增删，按 `ls-files` 名单 `git add` 会撞
# `fatal: pathspec … did not match any files`，**整批 rc=128**（2026-10-26 双跑实测：两遍都挂）。
# 从 HEAD 组装则完全无竞态，且最终树必然 == `HEAD^{tree}`（可直接对哈希）。
#
# 用法：bash scripts/seed_generation.sh <mirror> <remote-url> [batch_mb]
#   例：bash scripts/seed_generation.sh ~/hermes-backup ssh://git@ssh.github.com:443/me/newrepo.git 800
# 幂等：远端 main 若已等于最终树，则不再推（可重复跑，第二遍应为 no-op）。
# 输出：最后一行打印 `SEED-OK <commit>`，失败则非零退出。
set -euo pipefail

MIRROR="${1:?用法: seed_generation.sh <mirror> <remote-url> [batch_mb]}"
URL="${2:?缺少 remote url}"
BATCH_MB="${3:-800}"
HOMEDIR="${HERMES_HOME:-${LOCALAPPDATA:-$HOME}/hermes}"

# 钉住快照点：整轮只用开始时的那个 commit。否则同步轮在跑的时候 HEAD 会前进，
# 结尾的「种子文件数 vs HEAD 文件数」必然不等（2026-10-26 实测：16207 vs 16262 直接失败）。
BASE="$(git -C "$MIRROR" rev-parse HEAD)"
echo "  快照点：$BASE（$(git -C "$MIRROR" ls-tree -r "$BASE" --name-only | wc -l) 文件）"

IDX="$HOMEDIR/cache/scratch/seed-index-$$"
BATCHDIR="$HOMEDIR/cache/scratch/seed-batches-$$"
rm -f "$IDX"; rm -rf "$BATCHDIR"; mkdir -p "$BATCHDIR"
trap 'rm -f "$IDX"; rm -rf "$BATCHDIR"' EXIT

# ── 分批（按 HEAD 里的 blob 大小，大的优先，减少批次数）──
python - "$MIRROR" "$BASE" "$BATCH_MB" "$BATCHDIR" <<'PY'
import os
import subprocess
import sys

root, base, batch_mb, outdir = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
limit = batch_mb * 1024 * 1024
out = subprocess.run(['git', '-C', root, 'ls-tree', '-r', '-l', '-z', base],
                     capture_output=True, check=True).stdout.decode('utf-8', 'replace')
entries = []
for rec in out.split('\0'):
    if '\t' not in rec:
        continue
    meta, path = rec.split('\t', 1)
    parts = meta.split()            # 100644 blob <sha> <size>
    try:
        size = int(parts[3])
    except (IndexError, ValueError):
        size = 0
    entries.append((size, path))
entries.sort(key=lambda x: -x[0])
batches, cur, cursz = [], [], 0
for size, path in entries:
    if cur and cursz + size > limit:
        batches.append(cur); cur, cursz = [], 0
    cur.append(path); cursz += size
if cur:
    batches.append(cur)
if not batches:
    print('!! HEAD 里没有文件')
    sys.exit(1)
for i, b in enumerate(batches, 1):
    with open(os.path.join(outdir, 'batch-%03d.txt' % i), 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(b) + '\n')
print('  分批：%d 批 / %d 文件 / 每批 ≤ %dMB' % (len(batches), len(entries), batch_mb))
PY

# ── 用「HEAD 的条目」组装某一批的树（不碰工作树）──
build_tree() {   # $1 = "要保留的路径" 文件（累积到当前批为止）；stdout = tree sha
  # 先整棵读进来，再把本批之外的路径删掉。
  # 不能用 `update-index --index-info` 直接塞条目：它对**未被跟踪**的路径照样套 .gitignore，
  # 会打印 "Ignoring path …" 并跳过（实测整树变空 4b825dc…）。read-tree 走的是对象库，无此问题。
  GIT_INDEX_FILE="$IDX" git -C "$MIRROR" read-tree "$BASE"
  python - "$MIRROR" "$BASE" "$1" <<'PY' | GIT_INDEX_FILE="$IDX" git -C "$MIRROR" update-index --force-remove -z --stdin
import subprocess
import sys

root, base, bf = sys.argv[1], sys.argv[2], sys.argv[3]
keep = set(open(bf, encoding='utf-8').read().splitlines())
out = subprocess.run(['git', '-C', root, 'ls-tree', '-r', '-z', '--name-only', base],
                     capture_output=True, check=True).stdout.decode('utf-8', 'replace')
drop = [p for p in out.split('\0') if p and p not in keep]
sys.stdout.write('\0'.join(drop) + ('\0' if drop else ''))
PY
  GIT_INDEX_FILE="$IDX" git -C "$MIRROR" write-tree
}

# ── 第一遍：只算每批的树（不提交），用于「续跑/已就位」判定 ──
TREES=()
N=0
KEEP="$BATCHDIR/keep.txt"
: > "$KEEP"
for BF in "$BATCHDIR"/batch-*.txt; do
  N=$((N + 1))
  cat "$BF" >> "$KEEP"          # 累积：第 i 棵树 = 前 i 批的并集（推送时每批只新增自己那些 blob）
  TREES+=("$(build_tree "$KEEP")")
done
[ "$N" -gt 0 ] || { echo "!! 没有任何批次"; exit 1; }
FINAL_TREE="${TREES[$((N - 1))]}"
HEAD_TREE="$(git -C "$MIRROR" rev-parse "$BASE^{tree}")"
[ "$FINAL_TREE" = "$HEAD_TREE" ] || {
  echo "!! 组装出的树($FINAL_TREE) != 快照点的树($HEAD_TREE)——拒绝继续（会推出残树）"; exit 1; }

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
# 远端若是「别的形状」（既不是某批的树、也不是完整树）⇒ 拒绝覆盖，避免把完整备份回退成半棵树
if [ -n "$R_TREE" ]; then
  MATCH=0
  for T in "${TREES[@]}"; do [ "$T" = "$R_TREE" ] && MATCH=1; done
  if [ "$MATCH" = 0 ]; then
    echo "!! 远端 main 的树既不是任何一批、也不是完整树（$R_SHA）——拒绝覆盖，请人工核对"
    exit 1
  fi
fi

# ── 第二遍：从「远端已有的那批」之后继续推 ──
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
N_MIRROR=$(git -C "$MIRROR" ls-tree -r "$BASE" --name-only | wc -l)
[ "$N_SEED" = "$N_MIRROR" ] || { echo "!! 种子文件数($N_SEED) != 镜像 HEAD 文件数($N_MIRROR)"; exit 1; }
[ "$(git -C "$MIRROR" rev-parse "$COMMIT^{tree}")" = "$HEAD_TREE" ] || {
  echo "!! 最终提交的树 != 快照点的树"; exit 1; }

# 用 refs/heads/main 而不是 HEAD：本地路径/部分传输下 HEAD 不被广播，会返回空（2026-10-25 实案：
# 5 批推送全成功，却在这里误报「远端 HEAD() != 最后提交」并退出 1）
R=""
for _try in 1 2 3; do
  R=$(git -C "$MIRROR" ls-remote "$URL" refs/heads/main 2>/dev/null | cut -c1-40)
  [ -n "$R" ] && break
  sleep 2
done
[ "$R" = "$COMMIT" ] || { echo "!! 远端 main($R) != 最后提交($COMMIT)"; exit 1; }
echo "  文件数一致：$N_SEED / 批次数 $N"
echo "SEED-OK $COMMIT"
