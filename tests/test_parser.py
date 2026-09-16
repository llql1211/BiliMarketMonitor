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
        "avg_price": None,
        "deals": [],
        "image_url": None,
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
        {"price": "¥48", "time": "3天前"},
        {"price": "¥50", "time": "5天前"},
        {"price": "¥52", "time": "1周前"},
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
    assert result["deals"] == [{"price": "¥1", "time": "1天前"}]


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
        ("   ", "¥"),  # 现状如此：纯空白串会产出孤零零的 "¥"
    ],
)
def test_price_formats(raw, expected):
    """各种价格形态经 firstPrice 路径后的展示值，防止格式化回归。"""
    resp = _base_response()
    resp["data"]["clusterPriceFloorVO"]["priceTag"]["firstPrice"] = raw
    assert parser.parse_cluster(resp)["price"] == expected


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
