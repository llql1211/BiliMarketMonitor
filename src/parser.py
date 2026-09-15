"""响应解析：提取名称/价格/均价/成交/图片。

注意：没有成交记录的商品，响应里不存在 clusterRecentBuyFloorVO 字段，
所有取值必须判空，对应展示位显示 "—"（由界面层处理）。
"""

RECENT_DEALS_COUNT = 3  # 近 N 次成交，可调（网页一次返回多条，截取即可）


def parse_error(err: Exception) -> dict:
    """构造"查询失败"时的统一结果结构（单个商品失败不影响其他商品）。"""
    return {
        "ok": False,
        "error": str(err),
        "name": None,
        "price": None,
        "avg_price": None,
        "deals": [],
        "image_url": None,
    }


def parse_cluster(resp: dict) -> dict:
    """解析 cluster_info 响应，返回统一结构的结果 dict。

    取值路径（见 dev_notes/initial_plan.md 字段映射）：
      商品名   data.clusterBasicInfoFloorVO.clusterName
      当前价格 data.clusterPriceFloorVO.priceTag.firstPrice
      近期均价 data.clusterRecentBuyFloorVO.avgPrice（统计口径待确认）
      成交记录 data.clusterRecentBuyFloorVO.recentDeals[]
      缩略图   data.clusterHeaderFloorVO.clusterImgList[0]
    """
    result = {
        "ok": True,
        "error": None,
        "name": None,
        "price": None,
        "avg_price": None,
        "deals": [],
        "image_url": None,
    }

    data = resp.get("data") or {}
    if not isinstance(data, dict):
        result["ok"] = False
        result["error"] = "响应缺少 data 字段"
        return result

    # 商品名
    result["name"] = _dig(data, "clusterBasicInfoFloorVO", "clusterName")

    # 当前价格：price 是"参考价"，一般用 firstPrice（不同商品类型含义待进一步验证）
    result["price"] = _fmt_price(
        _dig(data, "clusterPriceFloorVO", "priceTag", "firstPrice")
    )

    # 成交相关：字段可能整体不存在
    recent = _dig(data, "clusterRecentBuyFloorVO")
    if isinstance(recent, dict):
        result["avg_price"] = _fmt_price(recent.get("avgPrice"))
        raw_deals = recent.get("recentDeals") or []
        result["deals"] = [
            {
                "price": _fmt_price(deal.get("dealPrice")),
                "time": deal.get("dealTime"),  # 相对时间（"8天前"），只能照原样展示
            }
            for deal in raw_deals[:RECENT_DEALS_COUNT]
            if isinstance(deal, dict)
        ]

    # 缩略图：形如 //i0.hdslb.com/bfs/...，需补全 https: 前缀
    img = _dig(data, "clusterHeaderFloorVO", "clusterImgList")
    if isinstance(img, list) and img:
        url = img[0]
        if isinstance(url, str) and url:
            if url.startswith("//"):
                url = "https:" + url
            elif not url.startswith("https:"):
                url = "https://" + url
            result["image_url"] = url

    # 不存在的 clusterId 也会返回 success=true，只是 data 里除了 clusterId 什么都没有；
    # 这种情况明确标成失败，避免界面上一直停留在"待抓取"。
    if not any((result["name"], result["price"], result["image_url"])):
        result["ok"] = False
        result["error"] = "接口没有返回该商品的数据（clusterId 可能已失效）"

    return result


def _dig(d, *path):
    """按路径逐层取 dict 值，任一层缺失返回 None。"""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _fmt_price(value):
    """价格统一成带 ¥ 的字符串；接口可能返回 "¥205" 也可能返回数字。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return f"¥{value:g}"
    text = str(value).strip()
    if text.startswith("¥") or text.startswith("￥"):
        return text
    return f"¥{text}"
