# -*- coding: utf-8 -*-
"""在 Hermes 官方运行时里执行指定脚本（与 `hermes` 启动器完全同源）。

命令由官方函数 `hermes_cli/_launchers.py::runtime_command()` 构建：
  <store python> -I -c "<官方 bootstrap>" <脚本> <脚本参数...>
- store python 由官方 `resolve_store_python()` 解析（<HERMES_HOME>/tools/python-*/）。
- 环境整理（清 PYTHONHOME/PYTHONPATH/VIRTUAL_ENV、注入 hermes-agent 路径、
  钉定 HERMES_HOME）全部由官方 bootstrap 完成——本脚本不再自实现。
- HERMES_HOME 若已设置则原样保留（可指向沙箱做隔离测试）。

用法:
    python run_in_runtime.py <目标脚本.py> [脚本参数...]
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_RUN_CODE = "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"


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
    sys.path.insert(0, str(root))
    from hermes_cli._launchers import runtime_command  # noqa: E402

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("用法: python run_in_runtime.py <脚本路径> [脚本参数…]\n"
              "  在 Hermes 官方运行时里执行指定脚本（缺 ruamel 等运行期依赖时用这个入口）。")
        return 0
    script = str(Path(sys.argv[1]).resolve())
    rest = sys.argv[2:]
    cmd = runtime_command(root, args=[script, *rest], code=_RUN_CODE, home=home)
    print(f"[run_in_runtime] python={cmd[0]}")
    print(f"[run_in_runtime] script={script} args={rest}")
    return subprocess.call(cmd, cwd=os.getcwd())


if __name__ == "__main__":
    sys.exit(main())
