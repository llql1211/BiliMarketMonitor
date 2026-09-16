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


@pytest.mark.parametrize(
    "template, expected",
    [
        # 没有占位符时补到查询串上：宁可链接长得怪，也不能悄悄丢掉商品 ID
        ("https://x.test/plain", "https://x.test/plain?clusterId=123"),
        ("https://x.test/p?a=1", "https://x.test/p?a=1&clusterId=123"),
    ],
)
def test_build_detail_url_appends_missing_placeholder(template, expected):
    """模板里没有 {clusterId} 时自动补一个查询参数，保证链接一定带 ID。"""
    assert links.build_detail_url("123", template) == expected


@pytest.mark.parametrize(
    "cluster_id, template",
    [
        ("abc", "https://x.test/p?clusterId={clusterId}"),  # ID 不是数字
        (None, "https://x.test/p?clusterId={clusterId}"),
        ("123", ""),  # 模板为空
        ("123", None),
        ("123", "   "),
    ],
)
def test_build_detail_url_unusable_returns_empty(cluster_id, template):
    """ID 或模板不可用时返回空串，界面显示「—」，不会点出一个错链接。"""
    assert links.build_detail_url(cluster_id, template) == ""


# ---------------- 手工编辑出来的脏数据 ----------------


@pytest.mark.parametrize("line", [None, 42, b"1001", ["1001"], {"id": "1"}])
def test_parse_line_non_string(line):
    """非字符串入参（None/数字/字节）返回 (None, None)，不抛 AttributeError。"""
    assert links.parse_line(line) == (None, None)


@pytest.mark.parametrize("extra_junk", ["", "1001 二\n"])
def test_load_links_notes_unparsable_lines(tmp_path, extra_junk):
    """有认不出来的行时写一条 notes；注释行和空行不算数。"""
    path = tmp_path / "watchlist.txt"
    path.write_text(
        "# 注释\n\n1002 | 乙\n这行是什么鬼\n" + extra_junk, encoding="utf-8"
    )

    notes = []
    entries = links.load_links(str(path), notes)

    assert [e.cluster_id for e in entries] == ["1002"]
    assert len(notes) == 1
    expected = 2 if extra_junk else 1  # "1001 二" 少了 "|"，也算认不出来
    assert f"{expected} 行认不出" in notes[0]


def test_load_links_gbk_file_is_read_not_crashed(tmp_path, monkeypatch):
    """被存成 GBK 的清单：硬按 UTF-8 读会抛 UnicodeDecodeError（不是 OSError，调用方兜不住）。

    这里把本地编码固定成 gbk，免得断言随运行环境的 locale 变化。
    """
    monkeypatch.setattr(links.locale, "getpreferredencoding", lambda *_: "gbk")
    path = tmp_path / "watchlist.txt"
    path.write_bytes("1001 | 中文名字\n1002\n".encode("gbk"))

    notes = []
    entries = links.load_links(str(path), notes)

    assert [e.cluster_id for e in entries] == ["1001", "1002"]
    assert entries[0].name == "中文名字"  # 按本地编码读回来，不是乱码
    assert any("UTF-8" in note for note in notes)


def test_load_links_undecodable_file_still_reads_ascii(tmp_path, monkeypatch):
    """本地编码也解不开时忽略非法字节强读，ID 这类 ASCII 内容必须保住。"""
    monkeypatch.setattr(links.locale, "getpreferredencoding", lambda *_: "utf-8")
    path = tmp_path / "watchlist.txt"
    path.write_bytes(b"1001 | \xff\xfe\xfa\n")

    notes = []
    entries = links.load_links(str(path), notes)

    assert [e.cluster_id for e in entries] == ["1001"]
    assert entries[0].name  # 乱码也留着，总比把整行丢掉好
    assert len(notes) == 1


def test_load_links_utf8_bom_first_line_is_kept(tmp_path):
    """记事本另存会加 BOM；带 BOM 时第一行不能整行丢掉。"""
    path = tmp_path / "watchlist.txt"
    path.write_bytes(b"\xef\xbb\xbf1001 | \xe7\x94\xb2\n1002\n")

    entries = links.load_links(str(path))
    assert [e.cluster_id for e in entries] == ["1001", "1002"]
    assert entries[0].name == "甲"


def test_load_links_without_notes_channel(tmp_path):
    """不传 notes 时同样降级，只是不留痕。"""
    path = tmp_path / "watchlist.txt"
    path.write_bytes("1001 | 中文名字\n乱码行\n".encode("gbk"))
    assert [e.cluster_id for e in links.load_links(str(path))] == ["1001"]


# ---------------- 名字里的危险字符 ----------------


def test_save_watchlist_cleans_pipe_and_newline_in_name(tmp_path):
    """名字里的 "|" 和换行会写坏清单（多出一行/多出一个字段），保存时清洗掉。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "名字|带竖线"), ("1002", "带\n换行")])

    body = [line for line in path.read_text(encoding="utf-8").split("\n") if line]
    assert body[2:] == ["1001 | 名字 带竖线", "1002 | 带 换行"]
    # 关键：读回来还是两条，没有被撑成四条
    entries = links.load_links(str(path))
    assert [e.cluster_id for e in entries] == ["1001", "1002"]


def test_save_watchlist_cleans_name_on_roundtrip(tmp_path):
    """清洗后的名字读回来与写进去的一致（不会越存越乱）。"""
    path = tmp_path / "watchlist.txt"
    items = [("1001", "  前后空格  "), ("1002", "名字|x")]
    links.save_watchlist(str(path), items)
    entries = links.load_links(str(path))
    assert [e.name for e in entries] == ["前后空格", "名字 x"]


@pytest.mark.parametrize(
    "item",
    [
        "1001",           # 字符串被当成 (id, name) 拆 → 拆不动
        ("1001",),        # 缺名称
        ("1001", "a", "b"),  # 多一项
        ("abc", "名字"),   # ID 不是数字
        (None, "名字"),
        ("", "名字"),
    ],
)
def test_save_watchlist_skips_bad_items(tmp_path, item):
    """条目形状不对/ID 非法的跳过不写，返回跳过条数并记 notes。"""
    path = tmp_path / "watchlist.txt"
    notes = []
    skipped = links.save_watchlist(str(path), [item, ("1002", "乙")], notes)

    assert skipped == 1
    assert len(notes) == 1 and "跳过" in notes[0]
    assert [e.cluster_id for e in links.load_links(str(path))] == ["1002"]


def test_save_watchlist_returns_zero_when_clean(tmp_path):
    """全部合法时返回 0，且不产生 notes。"""
    notes = []
    assert links.save_watchlist(str(tmp_path / "w.txt"), [("1001", "甲")], notes) == 0
    assert notes == []


def test_save_watchlist_creates_missing_directory(tmp_path):
    """data/ 被删掉时自己建出来，不让保存失败。"""
    path = tmp_path / "gone" / "deeper" / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "甲")])
    assert [e.cluster_id for e in links.load_links(str(path))] == ["1001"]


def test_save_watchlist_empty_or_none_items(tmp_path):
    """items 传 None 时按空清单处理，不抛 TypeError。"""
    path = tmp_path / "watchlist.txt"
    assert links.save_watchlist(str(path), None) == 0
    assert links.load_links(str(path)) == []


def test_default_watchlist_path_points_to_data_dir():
    """默认清单路径是项目根下 data/watchlist.txt。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(links.__file__)))
    path = links.default_watchlist_path()
    assert os.path.basename(path) == "watchlist.txt"
    assert os.path.dirname(path) == os.path.join(root, "data")
