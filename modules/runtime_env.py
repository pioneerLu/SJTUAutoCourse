"""源码启动时定位项目虚拟环境，不依赖第三方包。"""
import os
from pathlib import Path
import sys


def ensure_windows():
    if os.name != 'nt':
        raise SystemExit('AutoCourse 当前仅支持 Windows。')


def project_python(root, fallback=None):
    ensure_windows()
    fallback = fallback or sys.executable
    if getattr(sys, 'frozen', False):
        return fallback
    directory = Path(root) / '.venv'
    executable = directory / 'Scripts/python.exe'
    return str(executable) if executable.is_file() else fallback


def using_project_python(executable):
    # 不解析符号链接：venv 的 Python 可以与系统 Python 指向同一个二进制。
    normalize = lambda path: os.path.normcase(os.path.abspath(path))
    return normalize(executable) == normalize(sys.executable)
