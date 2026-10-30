#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语义对比两份 Hermes config.yaml：忽略注释与排版，只比真实键值。

用法:
    python compare-config.py <备份的 config.yaml> <本机的 config.yaml>

依赖: ruamel.yaml（Hermes 自带运行时里有；缺就 pip install ruamel.yaml）

输出三段:
    A. 只在备份里有   -> 本机被重置 / 新版删掉的设置，候选「搬回来」
    B. 两边都有值不同 -> 逐条判断以谁为准
    C. 只在本机有     -> 新版 schema 新增，保留本机

判读原则见 references/selective-restore.md。只读，不写任何文件。
"""
import json
import sys
from pathlib import Path

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("需要 ruamel.yaml: pip install ruamel.yaml")

# 版本自增 / 内部记账类键：差异无意义，默认不打印
NOISE = (
    "known_plugin_toolsets",
    "known_builtin_toolsets",
    "_config_version",
    "update.installs",
)


def flat(obj, prefix=""):
    """把嵌套 dict 摊平成 'a.b.c' -> 叶子值。列表整体当一个值比。"""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out.update(flat(v, key))
            else:
                out[key] = v
    else:
        out[prefix] = obj
    return out


def brief(value, limit=90):
    s = json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def load(path):
    yaml = YAML(typ="safe")
    return flat(yaml.load(Path(path).read_text(encoding="utf-8")) or {})


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    backup, live = load(sys.argv[1]), load(sys.argv[2])
    kb, kl = set(backup), set(live)

    only_b = sorted(k for k in kb - kl if not k.startswith(NOISE))
    only_l = sorted(k for k in kl - kb if not k.startswith(NOISE))
    differs = sorted(k for k in kb & kl if backup[k] != live[k] and not k.startswith(NOISE))

    print(f"备份 {len(kb)} 个叶子键 / 本机 {len(kl)} 个叶子键")
    print(f"仅备份有 {len(only_b)} · 仅本机有 {len(only_l)} · 值不同 {len(differs)}\n")

    print("=" * 72)
    print("A. 只在备份里有（本机被重置掉的设置，候选搬回来）")
    print("=" * 72)
    for k in only_b:
        print(f"  {k} = {brief(backup[k])}")

    print("\n" + "=" * 72)
    print("B. 两边都有但值不同（逐条定以谁为准）")
    print("=" * 72)
    for k in differs:
        print(f"  {k}\n      备份: {brief(backup[k])}\n      本机: {brief(live[k])}")

    print("\n" + "=" * 72)
    print("C. 只在本机有（新版新增，保留本机）")
    print("=" * 72)
    for k in only_l:
        print(f"  {k} = {brief(live[k], 60)}")


if __name__ == "__main__":
    main()
