# -*- coding: utf-8 -*-
"""把 sessions/<档>/ 的每会话文件（.jsonl / .part-NNNN.jsonl / 旧 .jsonl.gz）拼成若干可导入整包。

恢复流程（新机）示例：
    python tools/concat_sessions.py main
    python tools/concat_sessions.py main --batch-mb 200 --out D:\\restore-sessions
产出 <out>/main-part1.jsonl、main-part2.jsonl …；在桌面端「会话页 -> 导入」逐个导入。

默认每包 <= 250MB（原始大小，可调）；拼包产物在仓库根 restore-out/（已 gitignore）。
"""
import argparse
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESS = ROOT / "sessions"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", help="main 或档名（sessions/ 下的目录名）")
    ap.add_argument("--batch-mb", type=int, default=250)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    src = SESS / args.profile
    if not src.is_dir():
        print(f"未找到 {src}")
        return 1
    out = Path(args.out) if args.out else (ROOT / "restore-out")
    out.mkdir(parents=True, exist_ok=True)
    batch_limit = args.batch_mb * 1024 * 1024

    groups: dict[str, list[Path]] = {}
    for p in src.iterdir():
        if not p.is_file():
            continue
        n = p.name
        if n.endswith(".jsonl.gz"):
            groups.setdefault(n[:-len(".jsonl.gz")], []).append(p)
        elif ".part-" in n and n.endswith(".jsonl"):
            groups.setdefault(n.split(".part-", 1)[0], []).append(p)
        elif n.endswith(".jsonl"):
            groups.setdefault(n[:-len(".jsonl")], []).append(p)
    files = sorted(groups.items())
    part, written, total = 1, 0, 0
    fh = None
    try:
        for key, parts in files:
            data = b""
            for q in sorted(parts, key=lambda p: p.name):
                if q.name.endswith(".gz"):
                    with gzip.open(q, "rb") as gf:
                        data = gf.read()
                else:
                    data += q.read_bytes()
            if fh and written + len(data) > batch_limit:
                fh.close()
                print(f"  -> {out / f'{args.profile}-part{part}.jsonl'} ({written/1048576:.1f}MB)")
                part += 1
                written = 0
                fh = None
            if fh is None:
                fh = open(out / f"{args.profile}-part{part}.jsonl", "wb")
            fh.write(data)
            if not data.endswith(b"\n"):
                fh.write(b"\n")
            written += len(data)
            total += len(data)
        if fh:
            print(f"  -> {out / f'{args.profile}-part{part}.jsonl'} ({written/1048576:.1f}MB)")
    finally:
        if fh:
            fh.close()
    print(f"共 {len(files)} 个会话 -> {part} 个导入包（{total/1048576:.1f}MB），在 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
