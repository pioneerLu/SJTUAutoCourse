from modules.project_info import DEVELOPER, PROJECT_NAME


def print_banner():
    print(f"\n{PROJECT_NAME}")
    print("课程学习与章节测试")
    if DEVELOPER:
        print(f"by {DEVELOPER}")
    print()
