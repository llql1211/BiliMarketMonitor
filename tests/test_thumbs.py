"""缩略图磁盘缓存的测试：存得进、取得出、坏了/没权限都不影响看图。

缓存是锦上添花：它自己出任何岔子，都不该让「这张图画不出来」。
"""

import os

import thumbs


def test_save_then_load_round_trip(data_files):
    """存进去、读出来，字节一模一样。"""
    url = "https://img.test/a.png@96w_96h_85q.webp"
    thumbs.save(url, b"\x89PNG-fake")

    assert thumbs.load(url) == b"\x89PNG-fake"


def test_load_without_a_cache_returns_nothing(data_files):
    """没存过就是空字节，调用方据此去网络拿。"""
    assert thumbs.load("https://img.test/never-saved.png") == b""


def test_load_survives_a_missing_directory(data_files):
    """缓存目录被删了（用户随手清过）也不报错，当没缓存。"""
    url = "https://img.test/a.png"
    thumbs.save(url, b"x")
    os.remove(os.path.join(thumbs.cache_dir(), os.listdir(thumbs.cache_dir())[0]))

    assert thumbs.load(url) == b""


def test_the_resize_suffix_is_part_of_the_key(data_files):
    """地址带裁剪后缀，所以换了 IMAGE_SIZE 就是另一个 key，旧缓存自动作废。"""
    small = "https://img.test/a.png@96w_96h_85q.webp"
    big = "https://img.test/a.png@480w_480h_85q.webp"
    thumbs.save(small, b"small")

    assert thumbs.load(small) == b"small"
    assert thumbs.load(big) == b""


def test_save_ignores_empty_bytes(data_files):
    """空内容不落盘：那多半是失败的下载，存了反而把好的挡在外面。"""
    url = "https://img.test/a.png"
    thumbs.save(url, b"")

    assert not os.path.isdir(thumbs.cache_dir())  # 连目录都不该建


def test_save_survives_an_unusable_cache_dir(data_files):
    """缓存目录的位置被一个文件占了：静静地放弃，不能影响这一张图。"""
    with open(thumbs.cache_dir(), "w", encoding="utf-8") as handle:
        handle.write("我不是目录")

    thumbs.save("https://img.test/a.png", b"x")  # 不抛异常即通过
    assert thumbs.load("https://img.test/a.png") == b""


def test_prune_drops_the_oldest_files_only(data_files):
    """超过上限按修改时间删最旧的，留最新那批。"""
    thumbs.prune(limit=0)  # 先清空沙盒里可能有的东西
    urls = [f"https://img.test/{i}.png" for i in range(6)]
    for index, url in enumerate(urls):
        thumbs.save(url, b"x")
        os.utime(thumbs.path_for(url), (1000 + index, 1000 + index))  # 依次变新

    assert thumbs.prune(limit=2) == 4

    assert [thumbs.load(url) for url in urls] == [b"", b"", b"", b"", b"x", b"x"]


def test_prune_leaves_other_files_alone(data_files):
    """目录里不是我们写的文件一律不碰（用户自己丢进去的东西）。"""
    thumbs.prune(limit=0)
    os.makedirs(thumbs.cache_dir(), exist_ok=True)
    stray = os.path.join(thumbs.cache_dir(), "笔记.txt")
    with open(stray, "w", encoding="utf-8") as handle:
        handle.write("别删我")

    assert thumbs.prune(limit=0) == 0
    assert os.path.exists(stray)


def test_prune_survives_a_missing_directory(data_files):
    """目录还不存在（一张图都没下过）时返回 0，不抛异常。"""
    assert thumbs.prune(limit=0) == 0
