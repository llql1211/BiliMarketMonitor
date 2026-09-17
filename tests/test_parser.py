"""parser 的回归测试：统一结果结构、价格格式化、成交截断与缩略图补全。"""

import pytest

import parser


def _base_response():
    """一份字段齐全的 cluster_info 响应。"""
    return {
        "success": True,
        "data": {
            "clusterBasicInfoFloorVO": {"clusterName": "测试商品"},
            "clusterPriceFloorVO": {"priceTag": {"firstPrice": 44.5}},
            "clusterRecentBuyFloorVO": {
                "avgPrice": 50,
                "recentDeals": [
                    {"dealPrice": 48, "dealTime": "3天前"},
                    {"dealPrice": 50, "dealTime": "5天前"},
                    {"dealPrice": 52, "dealTime": "1周前"},
                ],
            },
            "clusterHeaderFloorVO": {"clusterImgList": ["//i0.hdslb.com/x.jpg"]},
        },
    }


# ---------------- parse_error ----------------


def test_parse_error_structure():
    """parse_error 只填 error 字段，其余全部是空占位，供界面显示失败态。"""
    result = parser.parse_error(ValueError("boom"))
    assert result == {
        "ok": False,
        "error": "boom",
        "name": None,
        "price": None,
        "reference_price": None,
        "avg_price": None,
        "deals": [],
        "image_url": None,
        "sold_out": False,
    }


# ---------------- parse_cluster 完整响应 ----------------


def test_parse_cluster_full_response():
    """字段齐全时名称/价格/均价/成交/缩略图都能取到，且 ok 为 True。"""
    result = parser.parse_cluster(_base_response())
    assert result["ok"] is True
    assert result["error"] is None
    assert result["name"] == "测试商品"
    assert result["price"] == "¥44.5"
    assert result["avg_price"] == "¥50"
    assert result["deals"] == [
        {"price": "¥48", "time": "3天前", "age_seconds": 3 * 86400},
        {"price": "¥50", "time": "5天前", "age_seconds": 5 * 86400},
        {"price": "¥52", "time": "1周前", "age_seconds": 7 * 86400},
    ]
    assert result["image_url"] == "https://i0.hdslb.com/x.jpg"


def test_parse_cluster_truncates_deals_to_limit():
    """成交多于 RECENT_DEALS_COUNT 时只保留前几条，防界面被刷爆。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = [
        {"dealPrice": i, "dealTime": f"{i}天前"} for i in range(5)
    ]
    result = parser.parse_cluster(resp)
    assert len(result["deals"]) == parser.RECENT_DEALS_COUNT
    assert [d["price"] for d in result["deals"]] == ["¥0", "¥1", "¥2"]


def test_parse_cluster_filters_non_dict_deals():
    """recentDeals 混入非 dict 元素（None/字符串）时被过滤掉，不抛异常。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = [
        {"dealPrice": 1, "dealTime": "1天前"},
        None,
        "garbage",
    ]
    result = parser.parse_cluster(resp)
    # 源码先按条数截取再过滤非 dict，所以这里只剩第一条
    assert result["deals"] == [{"price": "¥1", "time": "1天前", "age_seconds": 86400}]


# ---------------- 价格格式化（经 firstPrice 路径） ----------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (44, "¥44"),
        (44.5, "¥44.5"),
        ("¥205", "¥205"),
        ("￥205", "￥205"),  # 全角 ¥ 也原样保留
        ("205", "¥205"),
        (" 205 ", "¥205"),  # 先 str().strip() 再拼前缀
        (None, None),
        ("", None),
        ("   ", None),  # 纯空白串按「没抓到」处理，不再产出孤零零的 "¥"
        (True, None),   # bool 是 int 的子类，不能被当成价格
        (float("inf"), None),  # inf/nan 会让 "¥{value:g}" 变成 ¥inf / ¥nan
        (float("nan"), None),
        ({"a": 1}, None),  # 类型完全不对时也不再 str() 一下塞进表格
    ],
)
def test_price_formats(raw, expected):
    """各种价格形态经 firstPrice 路径后的展示值，防止格式化回归。"""
    resp = _base_response()
    resp["data"]["clusterPriceFloorVO"]["priceTag"]["firstPrice"] = raw
    assert parser.parse_cluster(resp)["price"] == expected


# ---------------- 原价（划线价）与售罄 ----------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ({"price": "110", "priceSymbol": "¥"}, "¥110"),
        ({"price": 110, "priceSymbol": "¥"}, "¥110"),
        ({"price": "110"}, "¥110"),          # 没有 priceSymbol 时退回 ¥
        ({"price": "¥110", "priceSymbol": "¥"}, "¥110"),  # 价格自带符号，别拼成 ¥¥110
        ({"price": "110", "priceSymbol": "$"}, "$110"),   # 符号以接口为准
        ({"price": None, "priceSymbol": "¥"}, None),      # 售罄时整组不返回
        ({"priceSymbol": "¥"}, None),
        ({"price": ""}, None),
        ({"price": "   "}, None),
        ({"price": {"a": 1}}, None),
        (None, None),
        ("not-a-dict", None),
    ],
)
def test_reference_price_from_price_tag(tag, expected):
    """原价取 priceTag.price 并带上接口给的货币符号（实测是 "¥"）。"""
    resp = _base_response()
    resp["data"]["clusterPriceFloorVO"]["priceTag"] = tag
    assert parser.parse_cluster(resp)["reference_price"] == expected


def test_parse_cluster_full_response_has_reference_price():
    """字段齐全时原价也跟着出来，界面才有「现价 / 原价」两列可比。"""
    resp = _base_response()
    resp["data"]["clusterPriceFloorVO"]["priceTag"] = {
        "firstPrice": "44",
        "firstPriceSymbol": "¥",
        "price": "110",
        "priceSymbol": "¥",
    }
    result = parser.parse_cluster(resp)
    assert result["price"] == "¥44"
    assert result["reference_price"] == "¥110"
    assert result["sold_out"] is False


@pytest.mark.parametrize(
    "button, expected",
    [
        ({"buttonState": 2, "buttonText": "已售罄"}, True),
        ({"buttonState": 1, "buttonText": "最低价仅1件"}, False),
        ({"buttonState": 3}, False),   # 已下架：没见过实例，先不当售罄
        ({"buttonState": 4}, False),   # 未开始
        ({"buttonText": "已售罄"}, False),  # 没有 buttonState 时不猜
        ({"buttonState": "2"}, False),
        ({"buttonState": True}, False),  # bool 是 int 的子类，别被当成 2
        ({"buttonState": None}, False),
        (None, False),
        ("not-a-dict", False),
    ],
)
def test_sold_out_follows_button_state(button, expected):
    """售罄只认 buttonState == 2；认不出来的一律当在售。"""
    resp = _base_response()
    if button is None:
        resp["data"].pop("clusterPurchaseButton", None)
    else:
        resp["data"]["clusterPurchaseButton"] = button
    assert parser.parse_cluster(resp)["sold_out"] is expected


def test_sold_out_response_holds_the_reference_price_as_first_price():
    """售罄商品的实测形状：priceTag 里没有 price，firstPrice 装的是原价。

    拿真实响应（10000001660）改的：同款在售时划线价 138，售罄后 firstPrice
    就是 138——所以这时候的「现价」必须由界面标成售罄，不能冒充市集现价。
    """
    resp = _base_response()
    resp["data"]["clusterPriceFloorVO"]["priceTag"] = {
        "firstPriceType": 3,
        "firstPrice": "138",
        "firstPricePrefix": "",
        "firstPriceSymbol": "¥",
    }
    resp["data"]["clusterPurchaseButton"] = {
        "buttonState": 2,
        "buttonText": "已售罄",
        "buttonDisabled": True,
    }
    result = parser.parse_cluster(resp)
    assert result["price"] == "¥138"
    assert result["reference_price"] is None  # 接口没给，界面显示 "—"
    assert result["sold_out"] is True


# ---------------- 缺成交字段 ----------------


@pytest.mark.parametrize("payload", [None, "not-a-dict", 42])
def test_missing_or_invalid_recent_vo(payload):
    """clusterRecentBuyFloorVO 缺失或不是 dict 时均价为空、成交列表为空。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"] = payload
    result = parser.parse_cluster(resp)
    assert result["ok"] is True  # 名称和价格还在，商品本身有效
    assert result["avg_price"] is None
    assert result["deals"] == []


# ---------------- data 字段异常 ----------------


def test_data_not_dict_reports_missing_data():
    """data 不是 dict 时按「缺少 data 字段」失败，不能抛异常。"""
    result = parser.parse_cluster({"success": True, "data": "oops"})
    assert result["ok"] is False
    assert result["error"] == "响应缺少 data 字段"


def test_data_missing_falls_into_invalid_cluster_path():
    """现状：data 整个缺失时被 or 成空 dict，走的是「clusterId 失效」分支。"""
    result = parser.parse_cluster({"success": True})
    assert result["ok"] is False
    assert "clusterId" in result["error"]


# ---------------- 失效 clusterId ----------------


def test_empty_data_is_invalid_cluster_id():
    """data 里只有 clusterId（商品已失效）时明确标失败，提示里带 clusterId。"""
    result = parser.parse_cluster({"success": True, "data": {"clusterId": "123"}})
    assert result["ok"] is False
    assert "clusterId" in result["error"]


@pytest.mark.parametrize(
    "patch_key, patch_value",
    [
        ("clusterBasicInfoFloorVO", {"clusterName": "有名字"}),
        ("clusterPriceFloorVO", {"priceTag": {"firstPrice": 1}}),
        ("clusterHeaderFloorVO", {"clusterImgList": ["//i0.hdslb.com/a.jpg"]}),
    ],
)
def test_any_single_field_keeps_ok(patch_key, patch_value):
    """名称/价格/缩略图只要有其一就算有效，防止误判成失效。"""
    resp = {"success": True, "data": {"clusterId": "123", patch_key: patch_value}}
    result = parser.parse_cluster(resp)
    assert result["ok"] is True


# ---------------- 缩略图补全 ----------------


@pytest.mark.parametrize(
    "img_list, expected",
    [
        (["//i0.hdslb.com/x.jpg"], "https://i0.hdslb.com/x.jpg"),
        (["i0.hdslb.com/x.jpg"], "https://i0.hdslb.com/x.jpg"),
        (["https://i0.hdslb.com/x.jpg"], "https://i0.hdslb.com/x.jpg"),
        ([], None),
        ([""], None),
        ([123], None),
        (None, None),
    ],
)
def test_thumbnail_url_normalization(img_list, expected):
    """缩略图地址统一补全成 https:// 开头，异常输入一律落到 None。"""
    resp = _base_response()
    resp["data"]["clusterHeaderFloorVO"]["clusterImgList"] = img_list
    assert parser.parse_cluster(resp)["image_url"] == expected


@pytest.mark.parametrize(
    "url", ["http://i0.hdslb.com/x.jpg", "https://i0.hdslb.com/x.jpg"]
)
def test_thumbnail_url_with_scheme_not_double_prefixed(url):
    """已经带了 scheme 的地址原样保留，不能拼成 https://https://…（http 尤其要注意）。"""
    resp = _base_response()
    resp["data"]["clusterHeaderFloorVO"]["clusterImgList"] = [url]
    assert parser.parse_cluster(resp)["image_url"] == url


# ---------------- 调用方保证不了的情况 ----------------


@pytest.mark.parametrize("resp", [None, [], 42, "not-a-dict", {"data": []}])
def test_non_dict_response_is_a_failure_not_a_crash(resp):
    """响应本身不是 dict（列表/数字/None）时按失败处理，绝不抛异常。"""
    result = parser.parse_cluster(resp)
    assert result["ok"] is False
    assert result["error"]


@pytest.mark.parametrize(
    "resp",
    [
        None,
        [],
        {"success": True},
        {"success": True, "data": {"clusterId": "1"}},
        _base_response(),
        _base_response()["data"],
    ],
)
def test_result_structure_is_always_the_same(resp):
    """不管喂进去什么，出口都是固定 7 个键 + 固定类型，界面层不用再判空。"""
    result = parser.parse_cluster(resp)
    assert set(result) == {
        "ok", "error", "name", "price", "reference_price", "avg_price",
        "deals", "image_url", "sold_out",
    }
    assert isinstance(result["ok"], bool)
    assert isinstance(result["sold_out"], bool)
    for key in ("error", "name", "price", "reference_price", "avg_price", "image_url"):
        assert result[key] is None or isinstance(result[key], str)
    assert isinstance(result["deals"], list)
    for deal in result["deals"]:
        assert set(deal) == {"price", "time", "age_seconds"}
        assert isinstance(deal["price"], str) and deal["price"]
        assert isinstance(deal["time"], str)
        # 时间认不出来时给 None（界面据此不上色），认出来的必须是整数秒
        assert deal["age_seconds"] is None or (
            isinstance(deal["age_seconds"], int)
            and not isinstance(deal["age_seconds"], bool)
        )


@pytest.mark.parametrize("error", [ValueError("boom"), ValueError(""), Exception()])
def test_parse_error_always_has_a_reason(error):
    """失败结果必须有 error 文案：异常本身没消息时给一句兜底，不留 None。"""
    result = parser.parse_error(error)
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"]


# ---------------- 成交记录里的脏数据 ----------------


def test_deals_without_price_are_dropped():
    """没有 dealPrice 的成交记录直接丢掉，不能渲染成「None · 8天前」。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = [
        {"dealPrice": 48, "dealTime": "3天前"},
        {"dealTime": "8天前"},  # 缺价格
        {"dealPrice": None, "dealTime": "9天前"},
        {"dealPrice": "", "dealTime": "10天前"},
    ]
    result = parser.parse_cluster(resp)
    assert result["deals"] == [{"price": "¥48", "time": "3天前", "age_seconds": 259200}]


@pytest.mark.parametrize(
    "deal_time, expected",
    [
        (None, ""),      # 缺失 → 空串（界面按「只有价格」展示，不再是 "· None"）
        ("", ""),
        ("   ", ""),
        ({"a": 1}, ""),  # 认不出来的类型 → 空串
        (123, "123"),    # 数字会被字符串化，至少不是 None
        (4.5, "4.5"),
        ("3天前", "3天前"),
    ],
)
def test_deal_time_is_normalized_to_string(deal_time, expected):
    """dealTime 统一成字符串：缺失/类型不对时是空串，绝不把 None 渲染到表格里。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = [
        {"dealPrice": 48, "dealTime": deal_time}
    ]
    # 只取 price/time 两项比对：本用例管的是时间文本本身，
    # 成交记录还有别的键（age_seconds，另有专门的用例），不该绊在这里
    deals = parser.parse_cluster(resp)["deals"]
    assert [(d["price"], d["time"]) for d in deals] == [("¥48", expected)]


def test_deals_filtered_before_truncating():
    """先滤掉脏记录再截断，前几条是垃圾时不会白白占掉 3 个展示位。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = [
        None,
        "garbage",
        {"dealPrice": 1, "dealTime": "1天前"},
        {"dealPrice": 2, "dealTime": "2天前"},
        {"dealPrice": 3, "dealTime": "3天前"},
        {"dealPrice": 4, "dealTime": "4天前"},
    ]
    result = parser.parse_cluster(resp)
    assert [d["price"] for d in result["deals"]] == ["¥1", "¥2", "¥3"]


@pytest.mark.parametrize("recent_deals", [{"a": 1}, "garbage", 42])
def test_recent_deals_not_a_list(recent_deals):
    """recentDeals 不是列表时当没有成交处理，不抛异常。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = recent_deals
    assert parser.parse_cluster(resp)["deals"] == []


def test_only_avg_price_still_counts_as_valid():
    """只有均价也算接口返回了数据，不该误判成「clusterId 已失效」。"""
    resp = {"success": True, "data": {"clusterRecentBuyFloorVO": {"avgPrice": 50}}}
    result = parser.parse_cluster(resp)
    assert result["ok"] is True
    assert result["avg_price"] == "¥50"


# ---------------- 相对时间 -> 秒数（成交新鲜度） ----------------
#
# 接口给的 dealTime 是「9小时前」这样的人话，界面拿它判断要不要高亮，
# 所以得先换算成秒。认不出来的格式必须是 None（= 不高亮），不能瞎猜。


@pytest.mark.parametrize(
    "text, expected",
    [
        ("刚刚", 0),
        ("刚才", 0),
        ("5秒前", 5),
        ("30分钟前", 1800),
        ("9小时前", 9 * 3600),
        ("1天前", 86400),
        ("33天前", 33 * 86400),
        ("1周前", 7 * 86400),
        ("2星期前", 14 * 86400),
        ("2个月前", 60 * 86400),
        ("3月前", 90 * 86400),   # 「月」和「个月」都要认
        ("1年前", 365 * 86400),
        (" 9小时前 ", 9 * 3600),  # 两端空白先剥掉
        ("1.5小时前", 5400),      # 半个钟头也得算得出来
    ],
)
def test_parse_relative_time(text, expected):
    """常见相对时间都能换算成秒数。"""
    assert parser.parse_relative_time(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        42,              # 不是字符串
        {"a": 1},
        "很久以前",       # 没有数字
        "9个钟头前",      # 数字认得出，单位认不出
        "前",            # 只有后缀
        "3天",           # 缺「前」字
        "2026-08-01",    # 绝对日期：宁可不高亮，也不要猜
        "3天前（约）",    # 不是干净的相对时间
    ],
)
def test_parse_relative_time_unknown_format(text):
    """认不出来的格式一律 None——界面据此不上色，照旧显示原文。"""
    assert parser.parse_relative_time(text) is None


def test_deal_age_follows_the_displayed_time():
    """age_seconds 必须和格子里显示的时间文本对得上，否则颜色会骗人。"""
    resp = _base_response()
    resp["data"]["clusterRecentBuyFloorVO"]["recentDeals"] = [
        {"dealPrice": 48, "dealTime": "9小时前"},
        {"dealPrice": 50, "dealTime": "最近"},  # 认不出来的时间
    ]
    deals = parser.parse_cluster(resp)["deals"]
    assert [(d["time"], d["age_seconds"]) for d in deals] == [
        ("9小时前", 9 * 3600),
        ("最近", None),
    ]
