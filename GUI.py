"""AutoCourse GUI launcher."""
import os
from pathlib import Path
import sys

from modules.runtime_env import ensure_windows, project_python, using_project_python


def main(project_root=None):
    ensure_windows()
    from modules.gui_app import main as gui_main
    return gui_main(project_root or Path(__file__).resolve().parent)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    executable = project_python(root)
    if not using_project_python(executable):
        os.execv(executable, [executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    main(root)
