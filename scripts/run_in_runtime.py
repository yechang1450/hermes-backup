# -*- coding: utf-8 -*-
"""在 Hermes 运行时里执行指定脚本（含全部依赖与 HERMES_HOME 语义）。

与官方启动器同构（hermes_cli/_launchers.py::runtime_command 的配方）：
  <store python> -I -c "pop 环境; sys.path+=hermes-agent; import hermes_bootstrap;
                        默认 HERMES_HOME; runpy.run_path(目标脚本)"
- store python 从 <HERMES_HOME>/tools/python-*/python.exe 取（与 hermes 启动器同源）。
- 环境变量 HERMES_HOME 若已设置则原样保留（可指向沙箱做隔离测试）。

用法:
    python run_in_runtime.py <目标脚本.py> [脚本参数...]
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from pathlib import Path


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "hermes"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    home = hermes_home()
    root = home / "hermes-agent"
    if not (root / "hermes_state.py").is_file():
        print(f"找不到 hermes-agent 源码目录: {root}")
        return 2
    pythons = glob.glob(str(home / "tools" / "python-*" / "python.exe"))
    if not pythons:
        print(f"找不到 store python（{home}/tools/python-*/python.exe）")
        return 2

    def _ver(p: str) -> tuple:
        m = re.search(r"python-(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    py = sorted(pythons, key=_ver)[-1]  # 按版本号取最新（字典序会误判 3.9 > 3.14）

    script = str(Path(sys.argv[1]).resolve())
    rest = sys.argv[2:]
    bootstrap = (
        "import os, sys, runpy; "
        "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
        "os.environ.pop('VIRTUAL_ENV', None); "
        f"sys.path.insert(0, {str(root)!r}); "
        "import hermes_bootstrap; "
        "os.environ.get('HERMES_HOME') or os.environ.__setitem__('HERMES_HOME', "
        "str(__import__('hermes_constants').get_default_hermes_root())); "
        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    cmd = [py, "-I", "-u", "-c", bootstrap, script, *rest]
    print(f"[run_in_runtime] python={py}")
    print(f"[run_in_runtime] script={script} args={rest}")
    return subprocess.call(cmd, cwd=os.getcwd())


if __name__ == "__main__":
    sys.exit(main())
