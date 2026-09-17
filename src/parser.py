"""响应解析：提取名称/价格/均价/成交/图片。

注意：没有成交记录的商品，响应里不存在 clusterRecentBuyFloorVO 字段，
所有取值必须判空，对应展示位显示 "—"（由界面层处理）。

本模块不假设调用方一定递进来一份格式正确的响应：字段缺失、类型不对、
接口哪天换了结构，都在这里降级成"查询失败"（ok=False + error），
并把还能用的字段照常带出去——解析失败的是这一个商品，不该影响其他商品。
出口统一过 _normalize_result()，界面层拿到的一定是固定结构、固定类型。
"""

import math
import re

RECENT_DEALS_COUNT = 3  # 近 N 次成交，可调（网页一次返回多条，截取即可）

# 「9小时前」里的单位换算成秒；接口给的是相对时间，只够粗判新旧
_RELATIVE_UNITS = {
    "秒": 1,
    "分钟": 60, "分": 60,
    "小时": 3600, "时": 3600,
    "天": 86400, "日": 86400,
    "周": 604800, "星期": 604800, "礼拜": 604800,
    "个月": 2592000, "月": 2592000,
    "年": 31536000,
}

# 已经「刚刚发生」的说法：等价于 0 秒前
_JUST_NOW_WORDS = ("刚刚", "刚才", "现在", "此刻")

_RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(.*?)前$")


def parse_relative_time(text) -> int | None:
    """把「9小时前」这类相对时间解析成秒数；认不出来返回 None。

    dealTime 是接口给的人话，不是时间戳（见 dev_notes/initial_plan.md），
    只够粗判新旧：界面拿它决定要不要高亮。绝对日期、错别字、空值等一律
    返回 None——调用方据此「不上色」，照旧把原文显示出来，不影响展示。
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    if text in _JUST_NOW_WORDS:
        return 0
    match = _RELATIVE_RE.match(text)
    if match is None:
        return None
    unit = _RELATIVE_UNITS.get(match.group(2).strip())  # "9个钟头前"这种就认不出来了
    if unit is None:
        return None
    return int(float(match.group(1)) * unit)


def parse_error(err: Exception) -> dict:
    """构造"查询失败"时的统一结果结构（单个商品失败不影响其他商品）。"""
    return _normalize_result({"ok": False, "error": str(err)})


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

    # 调用方传进来的不一定是 dict（None/列表/数字），直接按"查询失败"处理
    if not isinstance(resp, dict):
        result["ok"] = False
        result["error"] = f"响应格式异常（{type(resp).__name__}），不是 JSON 对象"
        return _normalize_result(result)

    data = resp.get("data") or {}
    if not isinstance(data, dict):
        result["ok"] = False
        result["error"] = "响应缺少 data 字段"
        return _normalize_result(result)

    # 商品名
    result["name"] = _clean_text(_dig(data, "clusterBasicInfoFloorVO", "clusterName"))

    # 当前价格：price 是"参考价"，一般用 firstPrice（不同商品类型含义待进一步验证）
    result["price"] = _fmt_price(
        _dig(data, "clusterPriceFloorVO", "priceTag", "firstPrice")
    )

    # 成交相关：字段可能整体不存在
    recent = _dig(data, "clusterRecentBuyFloorVO")
    if isinstance(recent, dict):
        result["avg_price"] = _fmt_price(recent.get("avgPrice"))
        raw_deals = recent.get("recentDeals")
        if not isinstance(raw_deals, list):
            raw_deals = []  # 类型不对时当没有成交
        for deal in raw_deals:
            if not isinstance(deal, dict):
                continue
            price = _fmt_price(deal.get("dealPrice"))
            if price is None:
                continue  # 没有价格的成交记录没有展示价值，丢掉而不是渲染成 None
            time_text = _clean_text(deal.get("dealTime")) or ""
            result["deals"].append(
                {
                    "price": price,
                    "time": time_text,
                    "age_seconds": parse_relative_time(time_text),
                }
            )
            if len(result["deals"]) >= RECENT_DEALS_COUNT:
                break

    # 缩略图：形如 //i0.hdslb.com/bfs/...，需补全 https: 前缀
    img = _dig(data, "clusterHeaderFloorVO", "clusterImgList")
    if isinstance(img, list) and img:
        url = img[0]
        if isinstance(url, str) and url:
            if url.startswith("//"):
                url = "https:" + url
            elif not url.startswith(("http://", "https://")):
                # 只补"没有 scheme"的情况；http:// 已带 scheme，不能重复拼出 https://http://…
                url = "https://" + url
            result["image_url"] = url

    # 不存在的 clusterId 也会返回 success=true，只是 data 里除了 clusterId 什么都没有；
    # 这种情况明确标成失败，避免界面上一直停留在"待抓取"。
    if not any(
        (result["name"], result["price"], result["avg_price"], result["image_url"])
    ):
        result["ok"] = False
        result["error"] = "接口没有返回该商品的数据（clusterId 可能已失效）"

    return _normalize_result(result)


def _dig(d, *path):
    """按路径逐层取 dict 值，任一层缺失返回 None。"""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _clean_text(value):
    """把字段值归一成非空字符串或 None；认不出来的类型一律当"没有"。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"{value:g}" if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _fmt_price(value):
    """价格统一成带 ¥ 的字符串；接口可能返回 "¥205" 也可能返回数字。

    认不出来的（bool、inf/nan、纯空白、dict 等）返回 None，由界面显示 "—"，
    宁可留空也不能把 "¥nan"、孤零零的 "¥" 这种东西渲染到表格里。
    """
    if value is None or isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):  # inf/nan：接口偶尔会返回，直接当没抓到
            return None
        return f"¥{value:g}"
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("¥") or text.startswith("￥"):
        return text
    return f"¥{text}"


def _normalize_result(result: dict) -> dict:
    """出口兜底：固定各级的键与类型，界面层可以放心直接用，不必再判空。"""
    ok = bool(result.get("ok"))
    error = _clean_text(result.get("error"))
    if not ok and not error:
        error = "未知错误"  # 失败就必须有原因，否则界面只能弹出空白提示
    deals = []
    for deal in result.get("deals") or []:
        if isinstance(deal, dict) and deal.get("price"):
            time_text = _clean_text(deal.get("time")) or ""
            deals.append(
                {
                    "price": str(deal["price"]),
                    "time": time_text,
                    # 从展示用的时间文本现算，保证「显示的是 9小时前」和
                    # 「要不要高亮」永远对得上，不受调用方递进来的值影响
                    "age_seconds": parse_relative_time(time_text),
                }
            )
        if len(deals) >= RECENT_DEALS_COUNT:
            break
    return {
        "ok": ok,
        "error": error,
        "name": _clean_text(result.get("name")),
        "price": _clean_text(result.get("price")),
        "avg_price": _clean_text(result.get("avg_price")),
        "deals": deals,
        "image_url": _clean_text(result.get("image_url")),
    }
