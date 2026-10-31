"""check-parts.py — 校验镜像里会话分片的不变式（纯标准库，只读）。

判据（固定偏移切片）：
  P1 片号从 0001 连续、无缺号
  P2 除最后一片外，每片字节数必须**正好**等于 PART_MAX（固定边界）
  P3 最后一片 ≤ PART_MAX 且 > 0
  P4 同一会话不允许 `<sid>.jsonl` 与 `<sid>.part-*.jsonl` 同时存在（形态混用）

为什么需要：改切片代码后，镜像里的旧文件**不会自动变形**——写入侧对内容相同的文件会跳过，
所以「代码回退 ≠ 数据归位」。P2 是外部可观测的形态指纹：若末片远小于 PART_MAX、倒数第二片
也不满，说明这批文件还是别的切法（例如试过的「末片上限」方案），必须跑一轮真同步让它归位。

用法：
  python check-parts.py --repo ~/hermes-backup
  python check-parts.py --repo ~/hermes-backup --profiles main,proj-83 --json
退出码：0 全通过 / 1 有违例 / 2 参数错误
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_PART_MAX = 12 * 1024 * 1024
PART_RE = re.compile(r"^(?P<sid>.+)\.part-(?P<num>\d{4})\.jsonl$")


def part_max_from_sync(repo: Path) -> int:
    """读切片阈值：**只解析源码文本，不 import sync**（import 会拉起重型依赖与副作用）。

    优先用镜像 tools/ 的运行副本（恢复端），否则用技能自带的 scripts/sync.py。
    """
    pattern = re.compile(r"^PART_MAX\s*=\s*([0-9*\s]+?)\s*(?:#.*)?$", re.M)
    for p in (repo / "tools" / "sync.py", Path(__file__).resolve().parent / "sync.py"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = pattern.search(text)
        if not m:
            continue
        value = 1
        try:
            for tok in m.group(1).split("*"):
                value *= int(tok.strip())
        except ValueError:
            continue
        if value > 0:
            return value
    return DEFAULT_PART_MAX


def check_profile(prof_dir: Path, part_max: int) -> tuple[list[dict], int]:
    groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    plain: set[str] = set()
    for f in prof_dir.glob("*.jsonl"):
        m = PART_RE.match(f.name)
        if m:
            groups[m.group("sid")].append((int(m.group("num")), f))
        else:
            plain.add(f.name[: -len(".jsonl")])

    problems: list[dict] = []
    for sid, parts in sorted(groups.items()):
        parts.sort(key=lambda x: x[0])
        nums = [n for n, _ in parts]
        sizes = [p.stat().st_size for _, p in parts]
        if nums != list(range(1, len(nums) + 1)):
            problems.append({"profile": prof_dir.name, "session": sid, "rule": "P1",
                             "detail": f"片号不连续：{nums[:6]}{'…' if len(nums) > 6 else ''}"})
        bad_mid = [(i + 1, s) for i, s in enumerate(sizes[:-1]) if s != part_max]
        if bad_mid:
            problems.append({"profile": prof_dir.name, "session": sid, "rule": "P2",
                             "detail": "非固定边界：%s（末三片 %s）"
                                       % (bad_mid[:3], [round(s / 1048576, 2) for s in sizes[-3:]])})
        if sizes[-1] <= 0 or sizes[-1] > part_max:
            problems.append({"profile": prof_dir.name, "session": sid, "rule": "P3",
                             "detail": f"末片 {sizes[-1]} 字节超出 (0, {part_max}]"})
        if sid in plain:
            problems.append({"profile": prof_dir.name, "session": sid, "rule": "P4",
                             "detail": "单体文件与分片同时存在"})
    return problems, len(groups)


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--repo", required=True, help="镜像仓根目录（含 sessions/）")
    ap.add_argument("--profiles", default="", help="只看这些档，逗号分隔（默认全部）")
    ap.add_argument("--part-max", type=int, default=0, help="分片阈值字节（默认读 sync.PART_MAX）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    sessions = Path(args.repo) / "sessions"
    if not sessions.is_dir():
        print(f"!! 找不到 {sessions}", file=sys.stderr)
        return 2
    part_max = args.part_max or part_max_from_sync(Path(args.repo))
    wanted = {s.strip() for s in args.profiles.split(",") if s.strip()}

    all_problems: list[dict] = []
    total_groups = 0
    for prof in sorted(sessions.iterdir()):
        if not prof.is_dir() or (wanted and prof.name not in wanted):
            continue
        probs, n = check_profile(prof, part_max)
        all_problems += probs
        total_groups += n

    if args.json:
        print(json.dumps({"part_max": part_max, "sessions_with_parts": total_groups,
                          "violations": all_problems}, ensure_ascii=False, indent=2))
    else:
        print(f"分片阈值 {part_max} 字节（{part_max / 1048576:.1f}MB）；带分片会话 {total_groups} 个")
        if total_groups == 0:
            print("提示：0 个带分片会话——全是单体文件时属正常；若本该有巨会话，先确认采集是否漏档")
        if not all_problems:
            print("PARTS-OK 全部通过（P1 编号连续 / P2 固定边界 / P3 末片合规 / P4 无形态混用）")
        else:
            for p in all_problems[:20]:
                print(f"  [{p['rule']}] {p['profile']}/{p['session']}  {p['detail']}")
            print(f"PARTS-FAIL 违例 {len(all_problems)} 项")
    return 0 if not all_problems else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
