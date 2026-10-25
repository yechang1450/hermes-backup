#!/bin/bash
# rotate-generation.sh — 数据仓换代：当前镜像树 → 孤儿快照单提交 → 新代私有仓；旧代冻结为只读档案。
#
# 为什么用孤儿快照：GitHub 单次 push 有 2GB 上限，而"带着旧历史整体搬"会超；
# 单提交快照把新仓历史清零，体积=当前树，推送一次即可（实测 2.4GB 树 / 262 秒）。
#
# 用法：
#   bash scripts/rotate-generation.sh --from hermes-backup --to hermes-backup-2027q1
#   bash scripts/rotate-generation.sh --from A --to B --dry-run        # 只看计划，不动任何东西
#   可选：--mirror D:/hermes-backup  --owner <gh-user>  --skip-archive  --keep-mirror-branch
#
# 幂等/可续跑：新仓若已存在快照提交（远端 HEAD 与本轮生成的 tree 一致），会跳过推送直接继续。
# 安全：全程持 sync 锁（sync.py 会主动让路）；工作树只允许 meta/、logs/ 的轮后改动。
set -euo pipefail

MIRROR="D:/hermes-backup"
OWNER=""
FROM=""
TO=""
DRY=0
SKIP_ARCHIVE=0
KEEP_MIRROR=0
BATCH_MB=800        # 分批推送的每批上限（MB）；set -u 下必须先给默认值

while [ $# -gt 0 ]; do
  case "$1" in
    --mirror) MIRROR="$2"; shift 2;;
    --owner) OWNER="$2"; shift 2;;
    --from) FROM="$2"; shift 2;;
    --to) TO="$2"; shift 2;;
    --batch-mb) BATCH_MB="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    --skip-archive) SKIP_ARCHIVE=1; shift;;
    --keep-mirror-branch) KEEP_MIRROR=1; shift;;
    -h|--help) sed -n '2,28p' "$0"; exit 0;;
    *) echo "未知参数：$1"; exit 2;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
HOMEDIR="${HERMES_HOME:-$LOCALAPPDATA/hermes}"
LOCK="$HOMEDIR/state/hermes-backup-sync.lock"
[ -n "$FROM" ] && [ -n "$TO" ] || { echo "必须给 --from 与 --to"; exit 2; }
[ "$FROM" != "$TO" ] || { echo "--from 与 --to 不能相同"; exit 2; }
if [ -z "$OWNER" ]; then OWNER=$(gh api user --jq .login 2>/dev/null || true); fi
[ -n "$OWNER" ] || { echo "无法确定 GitHub 属主（用 --owner 指定）"; exit 2; }

echo "===== $(date '+%F %T') 换代计划 ====="
echo "  镜像      : $MIRROR"
echo "  属主      : $OWNER"
echo "  旧代(冻结): $OWNER/$FROM"
echo "  新代(接手): $OWNER/$TO"
echo "  归档旧仓  : $([ $SKIP_ARCHIVE = 1 ] && echo 否 || echo 是)"

# ---------- [0] 前置检查 ----------
DIRT=$(git -C "$MIRROR" status --porcelain | grep -vE '^( M|M |A |\?\?) (meta/|logs/)' || true)
[ -z "$DIRT" ] || { echo "!! 工作树有意外改动："; echo "$DIRT" | head -8; exit 1; }
[ ! -e "$LOCK" ] || { echo "!! 已有同步在跑（锁存在 $LOCK）"; exit 1; }
L=$(git -C "$MIRROR" rev-parse HEAD)
# 用 refs/heads/main（HEAD 在部分传输/本地路径下可能不被广播，会误判"本地 != 远端"）
R=$(git -C "$MIRROR" ls-remote origin refs/heads/main 2>/dev/null | cut -c1-40)
[ "$L" = "$R" ] || { echo "!! 本地($L) != 远端($R)，先让下一轮同步收口"; exit 1; }
FILES=$(git -C "$MIRROR" ls-files | wc -l)
SESS=$(git -C "$MIRROR" ls-files 'sessions/*' | wc -l)
TREE_MB=$(git -C "$MIRROR" ls-files -z | python -c "
import os, sys
total = 0
root = sys.argv[1]
for raw in sys.stdin.buffer.read().split(b'\0'):
    if not raw: continue
    try: total += os.path.getsize(os.path.join(root, raw.decode('utf-8', 'replace')))
    except OSError: pass
print(round(total / 1048576))
" "$MIRROR" 2>/dev/null || echo "?")
echo "  前置 OK：local=remote=$L | 文件 $FILES / 会话件 $SESS / 树约 ${TREE_MB}MB"

if [ $DRY = 1 ]; then
  echo "== dry-run：不动任何东西。将要执行 =="
  echo "  1) git gc"
  echo "  2) 分批造提交（seed_generation.sh；每批 ≤ ${BATCH_MB}MB，dry-run 不生成）"
  echo "  3) gh repo create $OWNER/$TO --private（不存在时）"
  echo "  4) 分批判 push → $TO:main（每批一推，避开单次 2GB 上限）"
  echo "  5) 浅克隆抽查（文件数 / 会话可解析）"
  echo "  6) 切换：branch refs/$FROM-archive（--keep-mirror-branch 可跳过）/ update main / origin=$TO / push / 写 local.json 的 repo"
  echo "  7) $([ $SKIP_ARCHIVE = 1 ] && echo '跳过归档' || echo "gh api -X PATCH repos/$OWNER/$FROM -f archived=true")"
  echo "ROTATE-DRYRUN-OK"
  exit 0
fi

# ---------- 持锁 ----------
echo "{\"pid\": $$, \"started\": \"$(date '+%Y-%m-%dT%H:%M:%S%z')\"}" > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

echo "[1] 压缩本地对象（gc）"
git -C "$MIRROR" gc --quiet || echo "  gc 告警（继续）"
git -C "$MIRROR" count-objects -vH | grep -E '^(count|size-pack):' || true

URL="ssh://git@ssh.github.com:443/$OWNER/$TO.git"
echo "[2] 建新代私有仓 $OWNER/$TO"
gh repo create "$OWNER/$TO" --private 2>&1 || echo "  （已存在或创建告警，继续）"

# ---------- [3] 分批推送（seed_generation.sh） ----------
# 不再"单提交整树一次推"：GitHub 单次 push 上限 2GB，树到 3–12GB 时必然撞限。
# 分批逻辑在 seed_generation.sh（可单独跑、可对本地裸库测），每批 ≤ BATCH_MB，推完校验树一致。
echo "[3] 分批推送（每批 ≤ ${BATCH_MB}MB）"
SEED_OUT=$(bash "$HERE/seed_generation.sh" "$MIRROR" "$URL" "${BATCH_MB:-800}")
echo "$SEED_OUT" | grep -v '^SEED-OK' || true
C=$(echo "$SEED_OUT" | sed -n 's/^SEED-OK //p' | tail -1)
[ -n "$C" ] || { echo "!! 未拿到种子提交（见上面输出）"; exit 1; }
echo "  ✓ 远端 HEAD=$C"

echo "[5] 浅克隆抽查"
TMP="$HOMEDIR/cache/scratch/rotate-smoke-$(date +%s)"
rm -rf "$TMP"; git clone --quiet --depth 1 "$URL" "$TMP"
N_CLONE=$(git -C "$TMP" ls-files | wc -l)
echo "  索引文件：镜像 $FILES / 克隆 $N_CLONE"
[ "$N_CLONE" = "$FILES" ] || { echo "!! 文件数不一致，停止（新仓保留，可重跑本脚本续接）"; exit 1; }
SAMPLE=$(git -C "$TMP" ls-files 'sessions/main/*.jsonl' | grep -v '\.part-' | head -1)
if [ -n "$SAMPLE" ]; then
  python - "$TMP/$SAMPLE" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1], encoding='utf-8').read())
print('  抽查会话:', sys.argv[1].split('/')[-1], '| messages:', len(d.get('messages') or []))
PY
fi
rm -rf "$TMP"

echo "[6] 切换镜像到新代"
if [ "$KEEP_MIRROR" = 1 ]; then
  echo "  跳过本地 $FROM-archive 分支（--keep-mirror-branch）"
else
  git -C "$MIRROR" branch -f "$FROM-archive" main
fi
git -C "$MIRROR" update-ref refs/heads/main "$C"
git -C "$MIRROR" remote set-url origin "$URL"
echo "  origin -> $URL"
git -C "$MIRROR" push -q -u origin main && echo "  ✓ 切换后推送正常"
python - "$HERE/local.json" "$TO" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]); repo = sys.argv[2]
cfg = {}
if p.is_file():
    try: cfg = json.loads(p.read_text(encoding='utf-8'))
    except Exception: cfg = {}
cfg['repo'] = repo
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('  local.json repo ->', repo)
PY

if [ $SKIP_ARCHIVE = 1 ]; then
  echo "[7] 跳过归档旧仓（--skip-archive）"
else
  echo "[7] 归档旧代（只读，仍可克隆）"
  gh api -X PATCH "repos/$OWNER/$FROM" -f archived=true --jq '.archived'
fi

echo "===== $(date '+%F %T') 换代完成 ====="
echo "ROTATE-DONE"
