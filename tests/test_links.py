"""links 的回归测试：行解析、清单读写格式、备份行为与详情链接拼接。"""

import os

import pytest

import links


# ---------------- parse_line ----------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("10000008780", ("10000008780", "")),
        ("10000008780 | 名字", ("10000008780", "名字")),
        ("10000008780  |  名字", ("10000008780", "名字")),
        (
            "https://mall.bilibili.com/neul-next/resell/detail.html?"
            "clusterId=10000008780&share_medium=android&bbid=abc",
            ("10000008780", ""),
        ),
        (
            "https://mall.bilibili.com/detail?clusterId=10000008780&x=1 | 分享的名字",
            ("10000008780", "分享的名字"),
        ),
        ("# 注释行", (None, None)),
        ("", (None, None)),
        ("   ", (None, None)),
        ("abc", (None, None)),
        ("https://example.com/page?other=1", (None, None)),
    ],
)
def test_parse_line_cases(line, expected):
    """纯 ID / 带名 / 分享链接 / 注释 / 空白 / 乱码各形态的解析结果。"""
    assert links.parse_line(line) == expected


# ---------------- load_links ----------------


def test_load_links(tmp_path):
    """读入时跳过注释空行、重复 ID 留第一次、顺序与名字/raw 都保持。"""
    path = tmp_path / "watchlist.txt"
    path.write_text(
        "# 注释\n"
        "\n"
        "1001 | 甲\n"
        "1002\n"
        "1001 | 重复的名字\n"
        "  1003  |  丙  \n",
        encoding="utf-8",
    )
    entries = links.load_links(str(path))
    assert [(e.cluster_id, e.name) for e in entries] == [
        ("1001", "甲"),
        ("1002", ""),
        ("1003", "丙"),
    ]
    assert entries[0].raw == "1001 | 甲"
    # raw 记录的是去首尾空白后的原行（行内多余空格保留）
    assert entries[2].raw == "1003  |  丙"


def test_load_links_missing_file_raises(tmp_path):
    """清单文件不存在时抛 OSError（FileNotFoundError），由调用方兜底。"""
    with pytest.raises(OSError):
        links.load_links(str(tmp_path / "no_such_watchlist.txt"))


# ---------------- save_watchlist ----------------


def test_save_watchlist_format(tmp_path):
    """写出格式：两行注释头 + 空行 + 每行「ID | 名」，无名时只写 ID。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "甲"), ("1002", "")])
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0].startswith("#")
    assert lines[1].startswith("#")
    assert lines[2] == ""
    assert lines[3] == "1001 | 甲"
    assert lines[4] == "1002"
    assert lines[5] == ""  # 末尾换行产生的空串


def test_save_watchlist_no_tmp_leftover(tmp_path):
    """原子写完成后不能残留 .tmp 临时文件。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "")])
    assert not os.path.exists(str(path) + ".tmp")


def test_save_watchlist_bak_kept_from_first_save(tmp_path):
    """.bak 只留第一份：第二次保存不再覆盖首次的备份内容。"""
    path = tmp_path / "watchlist.txt"
    path.write_text("1001 | 旧数据\n", encoding="utf-8")

    links.save_watchlist(str(path), [("1001", "第一次保存")])
    bak = str(path) + ".bak"
    assert os.path.exists(bak)
    assert open(bak, encoding="utf-8").read() == "1001 | 旧数据\n"

    links.save_watchlist(str(path), [("1001", "第二次保存")])
    assert open(bak, encoding="utf-8").read() == "1001 | 旧数据\n"
    assert "第二次保存" in path.read_text(encoding="utf-8")


def test_save_then_load_roundtrip(tmp_path):
    """保存后再读回，clusterId 与名字都能还原。"""
    path = tmp_path / "watchlist.txt"
    items = [("1001", "甲"), ("1002", "")]
    links.save_watchlist(str(path), items)
    entries = links.load_links(str(path))
    assert [(e.cluster_id, e.name) for e in entries] == items


def test_save_watchlist_empty_items(tmp_path):
    """items 为空时文件只剩注释头和空行。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [])
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0].startswith("#")
    assert lines[1].startswith("#")
    assert lines[2] == ""
    assert lines[3] == ""  # 末尾换行


# ---------------- build_detail_url / default_watchlist_path ----------------


def test_build_detail_url_replaces_placeholder():
    """模板里的 {clusterId} 被替换成传入的 ID。"""
    url = links.build_detail_url("123", "https://x.test/d?clusterId={clusterId}")
    assert url == "https://x.test/d?clusterId=123"


def test_build_detail_url_accepts_int():
    """int 入参也会转成字符串替换进模板。"""
    url = links.build_detail_url(456, "https://x.test/d?clusterId={clusterId}")
    assert url == "https://x.test/d?clusterId=456"


def test_build_detail_url_without_placeholder():
    """模板里没有占位符时原样返回，不做任何改动。"""
    assert links.build_detail_url("123", "https://x.test/plain") == "https://x.test/plain"


def test_default_watchlist_path_points_to_data_dir():
    """默认清单路径是项目根下 data/watchlist.txt。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(links.__file__)))
    path = links.default_watchlist_path()
    assert os.path.basename(path) == "watchlist.txt"
    assert os.path.dirname(path) == os.path.join(root, "data")
