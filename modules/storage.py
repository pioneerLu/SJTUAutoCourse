"""本地文件存储：严格读取、容错读取和同目录原子替换。"""
import json
import os
from pathlib import Path
import tempfile


def read_json(path):
    """业务数据使用严格读取；损坏时交给调用方决定如何恢复。"""
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_json_safely(path, default):
    """显示元数据和查找历史时容错，并检查顶层数据类型。"""
    try:
        value = read_json(path)
        return value if isinstance(value, type(default)) else default
    except (ValueError, OSError):
        return default


def write_text_atomic(path, text):
    """写完并关闭临时文件后替换目标；失败时保留旧文件并清理临时文件。

    不创建父目录，由业务层控制记录存放位置。临时文件在 POSIX 上
    使用 0600 权限，配置和登录状态中的敏感信息也适用。
    """
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}-', suffix='.tmp', dir=path.parent)
    try:
        if os.name != 'nt':
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as file:
            fd = None  # 文件对象取得所有权，避免异常清理时重复关闭描述符。
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def write_json(path, data):
    # 先序列化：不支持的对象不能影响现有文件。
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
