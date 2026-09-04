"""跨平台独占文件锁：Unix 用 fcntl.flock，Windows 用 msvcrt.locking。

原 MedGraph-System 实现直接 `import fcntl`（仅 Unix 可用），在 Windows 上会直接崩溃。
本模块按平台选择锁原语，接口与 fcntl 用法对齐：
    acquire_exclusive(stream)  —— 非阻塞获取排他锁；被占用时抛 BlockingIOError
    release_exclusive(stream)  —— 释放锁
"""

from __future__ import annotations

try:
    import fcntl  # 仅 Unix 平台存在
    _USE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _USE_FCNTL = False


def acquire_exclusive(stream) -> None:
    """非阻塞获取排他锁；已被其他进程占用时抛 BlockingIOError。"""
    if _USE_FCNTL:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    # Windows：msvcrt.locking 按"字节偏移 + 长度"锁定，文件至少要有 1 字节可锁
    import msvcrt
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write("\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        # ERROR_LOCK_VIOLATION（winerror 33）表示区域已被其他进程锁定
        if getattr(exc, "winerror", None) == 33 or exc.errno in (13, 33):
            raise BlockingIOError(exc.errno, "file is locked by another process", getattr(exc, "filename", None)) from exc
        raise


def release_exclusive(stream) -> None:
    """释放由 acquire_exclusive 获取的排他锁。"""
    if _USE_FCNTL:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
