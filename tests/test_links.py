"""links 的回归测试：行解析、清单读写格式、备份行为与详情链接拼接。"""

import os

import pytest

import links


# ---------------- parse_line ----------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("10000008780", ("10000008780", "", "", False)),
        ("10000008780 | 名字", ("10000008780", "名字", "", False)),
        ("10000008780  |  名字", ("10000008780", "名字", "", False)),
        # 第三段是预期价；没设预期价的行维持老的两段写法
        ("10000008780 | 名字 | 50", ("10000008780", "名字", "50", False)),
        ("10000008780 | 名字 | ¥1,299.50", ("10000008780", "名字", "¥1,299.50", False)),
        # 名称未知但设了预期价：中间那段留空，靠末段是价格来认
        ("10000008780 |  | 50", ("10000008780", "", "50", False)),
        # 末段不是数字就不当预期价；多余段粘回名字里，不丢东西
        ("10000008780 | 名字 | 50 | 多余", ("10000008780", "名字 50 多余", "", False)),
        ("10000008780 | 名字 | 40 | 50", ("10000008780", "名字 40", "50", False)),
        # 名字带 "|" 的手工行：末段不像价格，整段按老规矩当名字
        ("10000008780 | 甲|乙", ("10000008780", "甲 乙", "", False)),
        ("10000008780 | 甲|50", ("10000008780", "甲", "50", False)),
        # 第四段那个 * 是收藏标记，一二三四段都能带
        ("10000008780 | *", ("10000008780", "", "", True)),
        ("10000008780 | 名字 | *", ("10000008780", "名字", "", True)),
        ("10000008780 | 名字 | 50 | *", ("10000008780", "名字", "50", True)),
        ("10000008780 |  | 50 | *", ("10000008780", "", "50", True)),
        ("10000008780 | 名字 |   50   |   *  ", ("10000008780", "名字", "50", True)),
        # 收藏先剥、预期价后认："... | 50 | *" 剥掉末段之后，50 才是那个价格
        # （反过来先认价格的话，* 不像价格，整段会连 50 一起被当成名字）
        # 名字以星号结尾 ≠ 收藏：认的是「最后一段整个就是 *」
        ("10000008780 | 限定版*", ("10000008780", "限定版*", "", False)),
        # 名字里带 "|" 又要收藏：中间那几段照样粘回名字里
        ("10000008780 | 甲|乙 | *", ("10000008780", "甲 乙", "", True)),
        ("10000008780 | 甲|乙|50 | *", ("10000008780", "甲 乙", "50", True)),
        (
            "https://mall.bilibili.com/neul-next/resell/detail.html?"
            "clusterId=10000008780&share_medium=android&bbid=abc",
            ("10000008780", "", "", False),
        ),
        (
            "https://mall.bilibili.com/detail?clusterId=10000008780&x=1 | 分享的名字",
            ("10000008780", "分享的名字", "", False),
        ),
        (
            "https://mall.bilibili.com/detail?clusterId=10000008780&x=1 | 分享的名字 | 88",
            ("10000008780", "分享的名字", "88", False),
        ),
        (
            "https://mall.bilibili.com/detail?clusterId=10000008780&x=1 | 名字 | 88 | *",
            ("10000008780", "名字", "88", True),
        ),
        ("# 注释行", (None, None, "", False)),
        ("", (None, None, "", False)),
        ("   ", (None, None, "", False)),
        ("abc", (None, None, "", False)),
        ("https://example.com/page?other=1", (None, None, "", False)),
    ],
)
def test_parse_line_cases(line, expected):
    """纯 ID / 带名 / 带预期价 / 带收藏 / 分享链接 / 注释 / 空白 / 乱码的解析结果。"""
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


def test_save_watchlist_format_with_expected_price(tmp_path):
    """设了预期价才写第三段：没设的行不多一个空尾巴，名称空着也留出中间那段。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(
        str(path),
        [("1001", "甲", "50"), ("1002", "乙"), ("1003", "", "88"), ("1004", "", "")],
    )
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[3] == "1001 | 甲 | 50"
    assert lines[4] == "1002 | 乙"
    assert lines[5] == "1003 |  | 88"  # 名称空着也得占住第二段，解析是按位置认的
    assert lines[6] == "1004"


def test_expected_price_roundtrip(tmp_path):
    """设过的预期价写出去再读回来还在；没设的读回来是空串。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "甲", "50"), ("1002", "乙")])
    entries = links.load_links(str(path))
    assert [(e.cluster_id, e.name, e.expected_price) for e in entries] == [
        ("1001", "甲", "50"),
        ("1002", "乙", ""),
    ]


def test_load_links_old_format_without_expected_price(tmp_path):
    """老清单（只有 ID 和名称两段）读进来预期价是空的，不用先手工改格式。"""
    path = tmp_path / "watchlist.txt"
    path.write_text("1001 | 甲\n1002\n", encoding="utf-8")
    entries = links.load_links(str(path))
    assert [(e.cluster_id, e.expected_price) for e in entries] == [
        ("1001", ""),
        ("1002", ""),
    ]


def test_save_watchlist_cleans_pipe_in_expected_price(tmp_path):
    """预期价里的 "|" 会被清掉，免得写出一行下次读进来就散架的清单。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "甲", "5|0")])
    assert [e.expected_price for e in links.load_links(str(path))] == ["5 0"]


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


# ---------------- 收藏标记 ----------------

def _body(path):
    """清单里去掉注释头和空行之后的正文本，专供比对写回格式。"""
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


def test_save_watchlist_favorite_appends_a_mark(tmp_path):
    """收藏的行末尾追加「| *」；没收藏的行一个字符都不多写（老写法原样保留）。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(
        str(path),
        [
            ("1001", "甲", "50", True),   # 名 + 预期价 + 收藏
            ("1002", "乙", "88"),         # 三两段的写法照样收，默认不收藏
            ("1003", "丙", "", True),     # 只有名
            ("1004", "", "", True),       # 连名都没有
            ("1005", "戊"),               # 不收藏，也不设预期价
        ],
    )
    assert _body(path)[2:] == [
        "1001 | 甲 | 50 | *",
        "1002 | 乙 | 88",
        "1003 | 丙 | *",
        "1004 | *",
        "1005 | 戊",
    ]


def test_favorite_roundtrip(tmp_path):
    """收藏标记写出去再读回来还在；没收藏的读回来是 False。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "甲", "50", True), ("1002", "乙")])
    entries = links.load_links(str(path))
    assert [(e.cluster_id, e.favorite) for e in entries] == [("1001", True), ("1002", False)]


def test_load_links_keeps_favorite_in_raw(tmp_path):
    """raw 记的是原行（带着收藏段），保存别的地方时不会把它抹掉。"""
    path = tmp_path / "watchlist.txt"
    path.write_text("1001 | 甲 | 50 | *\n", encoding="utf-8")
    entry = links.load_links(str(path))[0]
    assert entry.favorite is True
    assert entry.raw == "1001 | 甲 | 50 | *"


def test_load_links_old_format_without_favorite(tmp_path):
    """老清单（一段 / 两段 / 三段）读进来收藏都是 False，写回也不多长出一段。"""
    path = tmp_path / "watchlist.txt"
    path.write_text("1001 | 甲\n1002 | 乙 | 50\n1003\n", encoding="utf-8")

    entries = links.load_links(str(path))
    assert [(e.cluster_id, e.favorite) for e in entries] == [
        ("1001", False),
        ("1002", False),
        ("1003", False),
    ]

    links.save_watchlist(
        str(path),
        [(e.cluster_id, e.name, e.expected_price, e.favorite) for e in entries],
    )
    assert _body(path)[2:] == ["1001 | 甲", "1002 | 乙 | 50", "1003"]


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        ("*", True),
        ("  *  ", True),
        (False, False),
        ("", False),       # 空串不是收藏
        ("x", False),      # 别的字符也不是
        (1, False),        # 只认 True 和 "*"，数字 1 不认
        (None, False),
    ],
)
def test_save_watchlist_favorite_strictness(tmp_path, value, expected):
    """收藏段只认 True 和清单里那个 "*" 写法，其余一律当没收藏，免得写出怪东西。"""
    path = tmp_path / "watchlist.txt"
    links.save_watchlist(str(path), [("1001", "甲", "", value)])
    assert links.load_links(str(path))[0].favorite is expected


# ---------------- 手工编辑出来的脏数据 ----------------


@pytest.mark.parametrize("line", [None, 42, b"1001", ["1001"], {"id": "1"}])
def test_parse_line_non_string(line):
    """非字符串入参（None/数字/字节）返回 (None, None, "", False)，不抛 AttributeError。"""
    assert links.parse_line(line) == (None, None, "", False)


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
        "1001",                        # 字符串会被逐字拆开
        ("1001",),                     # 缺名称
        ("1001", "a", "b", "c", "d"),  # 多一项
        ("abc", "名字"),                # ID 不是数字
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
