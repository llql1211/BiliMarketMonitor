"""缩略图的磁盘缓存：同一张图下过一次，之后重启就直接从盘里贴。

为什么要落盘：缩略图跟着商品走，一个商品的图片地址几乎不变，而程序每次启动都要
把清单里几百张图重下一遍。网络通畅时这只是白费流量，网络一抖（VPN、线路抖动）
就是「打开十几秒不见图」——盘里存过的那份跟网络没关系。

key 用**带 CDN 裁剪后缀的完整地址**：改了 IMAGE_SIZE 之后地址跟着变，旧文件自然
就用不上了，不必另外维护一个版本号。

存在 data/thumbs/ 下，跟缓存库一个家：可以随手删掉，丢了只是下次重下一遍。
写入先落 .part 再改名，中途挂掉不会留下半张图当成缓存。
"""

import hashlib
import os
import threading

import links

SUBDIR = "thumbs"
MAX_FILES = 2000  # 上限：一张十几 KB，满打满算几十 MB
PRUNE_EVERY = 100  # 每存这么多张才扫一次目录——扫一遍比存一张贵得多

_lock = threading.Lock()
_saved_since_prune = 0


def cache_dir() -> str:
    """缓存目录（惰性求值：测试把 data 目录换掉，这里跟着换）。"""
    return os.path.join(links._data_dir(), SUBDIR)


def path_for(url: str) -> str:
    """某个地址对应的缓存文件名。

    用 sha1 而不是原名：地址里有 `?`、`/`、`@`，直接当文件名在 Windows 上写不出去。
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir(), digest[:16] + ".img")


def load(url: str) -> bytes:
    """取缓存里的字节。

    没有、读不动（文件被删、没权限）都返回空字节：调用方一律当「没缓存」，
    该走网络走网络。读到的是不是好图不在这儿判——解码那头解不出来自然会退回网络。
    """
    try:
        with open(path_for(url), "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def save(url: str, data: bytes) -> None:
    """把下载回来的字节存一份。

    存不下（盘满、没权限、目录被占）就当没这回事：缓存是锦上添花，绝不因为它
    影响这一张图能不能显示。
    """
    global _saved_since_prune
    if not data:
        return
    path = path_for(url)
    tmp = path + ".part"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return

    with _lock:
        _saved_since_prune += 1
        due = _saved_since_prune >= PRUNE_EVERY
        if due:
            _saved_since_prune = 0
    if due:
        prune()


def prune(limit: int = MAX_FILES) -> int:
    """超过上限就按修改时间删最旧的，返回删掉了几个。

    清单换过好几轮之后，那些早就不跟踪的商品会一直躺着；按 mtime 删的是最久没
    再被写过的，也就是最不可能再看到的那批。
    """
    directory = cache_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    entries = []
    for name in names:
        if not name.endswith(".img"):
            continue  # 不是我们写的（比如用户丢进来的东西）一律不碰
        path = os.path.join(directory, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue  # 刚好被别的实例删了，跳过
    if len(entries) <= limit:
        return 0
    entries.sort()
    removed = 0
    for _mtime, path in entries[: len(entries) - limit]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed
