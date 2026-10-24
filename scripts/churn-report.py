"""量一轮提交"实际重写了多少内容"——按目录汇总新增 blob 字节数。

## 为什么需要
备份仓的容量增长由"每轮重写量"决定，不是由"数据总量"决定。2026-10-24 实案：
一轮重写 827–967MB（会话头部字段变化导致全部分片重写），仓体积 8 小时涨 1.26GB；
修好尾部稳定化后应降到只含真实新增内容的量级。

## 用法
    python churn-report.py                       # 镜像仓 HEAD~1..HEAD
    python churn-report.py --range HEAD~3 HEAD
    python churn-report.py --repo D:/hermes-backup --top 10
    python churn-report.py --json                # 机器可读

判据：`>5MB 文件` 里如果出现同一个会话的多个 `.part-`，说明该会话仍被整份重写。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

DEFAULT_REPO = "D:/hermes-backup"


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True).stdout


def _raw_pairs(repo: str, a: str, b: str) -> list[tuple[str, str]]:
    """返回 [(new_blob_sha, path)]；用 `-z` 避免带空格路径被引号/空格切断。"""
    out = subprocess.run(["git", "-C", repo, "diff-tree", "-r", "--no-commit-id", "--raw", "-z", a, b],
                         capture_output=True, text=True).stdout
    tokens = out.split("\0")
    pairs: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(tokens):
        meta = tokens[i].lstrip(":")
        path = tokens[i + 1]
        fields = meta.split()
        if len(fields) >= 4:
            pairs.append((fields[3], path))     # 新对象的 sha（删除时为全 0）
        i += 2
    return pairs


def report(repo: str, a: str, b: str, top: int) -> dict:
    rows = _raw_pairs(repo, a, b)
    shas = sorted({s for s, _ in rows if s.strip("0").strip()})
    size: dict[str, int] = {}
    if shas:
        out = subprocess.run(["git", "-C", repo, "cat-file", "--batch-check=%(objectname) %(objectsize)"],
                             input="\n".join(shas), capture_output=True, text=True).stdout
        for ln in out.splitlines():
            sha, n = ln.split()
            size[sha] = int(n)
    agg: Counter = Counter()
    cnt: Counter = Counter()
    for sha, path in rows:
        key = "/".join(path.split("/")[:2])
        agg[key] += size.get(sha, 0)
        cnt[key] += 1
    big = sorted(((size.get(s, 0), p) for s, p in rows if size.get(s, 0) > 5 * 1048576), reverse=True)
    return {
        "range": f"{a}..{b}",
        "files": len(rows),
        "total_mb": round(sum(agg.values()) / 1048576, 1),
        "by_dir": [{"dir": k, "mb": round(v / 1048576, 1), "files": cnt[k]} for k, v in agg.most_common(top)],
        "over_5mb": {"count": len(big), "mb": round(sum(x[0] for x in big) / 1048576, 1),
                     "top": [{"mb": round(s / 1048576, 1), "path": p} for s, p in big[:5]]},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="量一轮提交的重写量（按目录汇总新增 blob 字节）")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"镜像仓路径（默认 {DEFAULT_REPO}）")
    ap.add_argument("--range", nargs=2, metavar=("FROM", "TO"), default=["HEAD~1", "HEAD"])
    ap.add_argument("--top", type=int, default=8, help="按目录列出前 N 项")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    a, b = args.range
    rep = report(args.repo, a, b, args.top)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"{rep['range']}  {rep['files']} 个文件，新增内容 {rep['total_mb']} MB")
    for item in rep["by_dir"]:
        print(f"  {item['dir']:<36} {item['mb']:8.1f} MB  ({item['files']} 文件)")
    over = rep["over_5mb"]
    print(f"  >5MB 文件：{over['count']} 个，合计 {over['mb']} MB")
    for item in over["top"]:
        print(f"    {item['mb']:7.1f} MB  {item['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
