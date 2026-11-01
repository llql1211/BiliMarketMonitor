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

SOLD_OUT_STATE = 2  # clusterPurchaseButton.buttonState：2 就是「已售罄」

# items/info 的「商品不存在或被删除」。它是**答案**而不是错误：这件商品已经
# 问不出店铺了，记下来别再反复问（见 parse_item_shop）
ITEM_GONE_CODE = 81100001

# 购买按钮文案里的剩余件数（形如「最低价仅1件」）。同一处还会出现「当前最低价」
# 和「已售罄」两种没带数字的文案，那两种都当"没有件数可报"。
_STOCK_COUNT_RE = re.compile(r"仅\s*(\d+)\s*件")

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

# 「刚刚」没给数字，凑不出来一分钟以外的上界，按一分钟算
_JUST_NOW_BOUNDS = (0, 60)

_RELATIVE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(.*?)前$")

# 写回「N分钟前」时用的单位表：从大到小，同值的别名只留一个
# （_RELATIVE_UNITS 是"这个说法值多少秒"，这里是"这些秒数该用哪种说法"）
_AGE_UNIT_NAMES = (
    (31536000, "年"),
    (2592000, "个月"),
    (604800, "周"),
    (86400, "天"),
    (3600, "小时"),
    (60, "分钟"),
)

# 价格文本里认数字：只取第一个数，千分位逗号在匹配前先抹掉
_PRICE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def age_bounds(text):
    """「9小时前」这类相对时间对应的年龄区间「下界, 上界」（闭区间，秒）；认不出返回 None。

    dealTime 是接口给的人话，不是时间戳（见 dev_notes/initial_plan.md），它说的是
    「9 个多小时、还不到 10 小时」——给的是个区间，不是个点。这个区间是判断
    「两次抓到的成交是不是同一条」的依据（见 app.new_deals）：同一时刻取上下界
    都算不出来，就说明两条记录对不上。

    闭区间是为了好算：界面显示「9小时前」当且仅当真实年龄落在 [9小时, 10小时] 里，
    拿上界当"最老可能是多少"直接比就行了，不用在调用处记得处理开闭。

    绝对日期、错别字、空值、「9个钟头前」这种没收录的单位一律返回 None——
    调用方据此认定「这条认不出年龄」，退回原文比对而不是瞎猜一个数。
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    if text in _JUST_NOW_WORDS:
        return _JUST_NOW_BOUNDS
    match = _RELATIVE_RE.match(text)
    if match is None:
        return None
    unit = _RELATIVE_UNITS.get(match.group(2).strip())  # "9个钟头前"这种就认不出来了
    if unit is None:
        return None
    lower = float(match.group(1)) * unit
    return int(lower), int(lower + unit) - 1


def parse_relative_time(text) -> int | None:
    """把「9小时前」这类相对时间解析成秒数（取下界）；认不出来返回 None。

    只够粗判新旧：界面拿它决定要不要高亮「近期成交」。要判两条成交是不是同一条
    得用 age_bounds() 的区间，取个下界当点用会把「9小时前」当成正好 9 小时。
    """
    bounds = age_bounds(text)
    return bounds[0] if bounds else None


def format_age(seconds) -> str | None:
    """秒数 →「9小时前」这类说法，parse_relative_time() 的反方向；认不出返回 None。

    给缓存里的成交用：它们存的是抓取那一刻的相对时间，隔一天再显示原文就成了谎话，
    得按"又过了多久"重算一遍（见 app._reaged_deals）。

    按整单位往下取：36000 秒说「10小时前」，也只是说"至少 10 小时"——跟
    age_bounds() 把下界当年龄用是同一个口径，宁可把成交说得老一点，也不要说新。
    不足一分钟的说「刚刚」。
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    for unit, name in _AGE_UNIT_NAMES:
        if seconds >= unit:
            return f"{int(seconds // unit)}{name}前"
    return _JUST_NOW_WORDS[0]


def price_number(text) -> float | None:
    """把「¥1,299.50」这类价格还原成数字，用来比较两次抓取的涨跌。

    只认数字本身：货币符号、千分位逗号、空白都先脱掉，取第一个数。
    认不出来（"面议"、空值、dict 等）返回 None——调用方据此不显示涨跌，
    宁可少显示一处，也不要拿三个问号去猜这个价格是多少。
    """
    if text is None or isinstance(text, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(text, (int, float)):
        return float(text) if math.isfinite(text) else None
    if not isinstance(text, str):
        return None
    match = _PRICE_NUMBER_RE.search(text.replace(",", "").replace("，", ""))
    if match is None:
        return None
    value = float(match.group())
    return value if math.isfinite(value) else None


def parse_error(err: Exception) -> dict:
    """构造"查询失败"时的统一结果结构（单个商品失败不影响其他商品）。"""
    return _normalize_result({"ok": False, "error": str(err)})


def parse_cluster(resp: dict) -> dict:
    """解析 cluster_info 响应，返回统一结构的结果 dict。

    取值路径（见 dev_notes/initial_plan.md 字段映射）：
      商品名   data.clusterBasicInfoFloorVO.clusterName
      当前价格 data.clusterPriceFloorVO.priceTag.firstPrice
      原价     data.clusterPriceFloorVO.priceTag.price（+ priceSymbol）
      售罄     data.clusterPurchaseButton.buttonState == 2
      剩余件数 data.clusterPurchaseButton.buttonText（形如「最低价仅1件」）
      近期均价 data.clusterRecentBuyFloorVO.avgPrice（统计口径待确认）
      成交记录 data.clusterRecentBuyFloorVO.recentDeals[]
      缩略图   data.clusterHeaderFloorVO.clusterImgList[0]
    """
    result = {
        "ok": True,
        "error": None,
        "name": None,
        "price": None,
        "reference_price": None,
        "avg_price": None,
        "deals": [],
        "image_url": None,
        "sold_out": False,
        "stock_count": None,
        "item_id": None,
        "self_hint": False,
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

    # 会员购主商品 ID：店铺只能靠它再查一次 items/info（见 parse_item_shop）
    result["item_id"] = _clean_id(_dig(data, "reportParams", "item_id"))

    # 服务标签里的「自营 / 官仓直发」：主商品查不到（已下架）时，这是唯一还能说明
    # "这单是谁卖的"的东西（见 parse_item_shop 与 app.NameDelegate）
    result["self_hint"] = _self_hint(_dig(data, "clusterServiceFloorVO", "services"))

    # 价格区：firstPrice 是「现价」，price 是划线那个「原价」。
    # 注意售罄时接口整组不返回 price/priceSymbol，此时 firstPrice 装的就是原价
    # ——所以判售罄不能靠"有没有 price"，得看购买按钮的状态（见下）。
    price_tag = _dig(data, "clusterPriceFloorVO", "priceTag")
    if isinstance(price_tag, dict):
        result["price"] = _fmt_price(price_tag.get("firstPrice"))
        result["reference_price"] = _fmt_reference_price(price_tag)

    button = _dig(data, "clusterPurchaseButton")
    result["sold_out"] = _is_sold_out(button)
    result["stock_count"] = _stock_count(button)

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


def parse_item_shop(resp) -> dict:
    """解析 items/info 响应里的售卖店铺，返回固定结构。

    取数口径（抽样 143 件验证，见 dev_notes/261101_市集店铺信息调查.md）：
      merchantId == 0  -> 会员购自营（shop 为 None、self_sold 为 True）
      merchantId != 0  -> 商家店铺名 shopInfoVO.shopName（self_sold 为 False）
      code == 81100001 -> 商品已被删除（两者都是 None，gone 为 True）

    不能用的两个字段：shopVO.shopName 恒为「会员购 」（市集商品的壳）、
    selfSold 恒为 true，拿它们判店铺只会把每件商品都认成会员购自营。

    ok 表示"这次有定论"：连「商品没了」「认不出店铺」都算定论（调用方据此
    记一笔别再问）；只有响应坏了、或回了个不认识的错误码才是 ok=False，
    那才是下轮该重试的。
    """
    result = {"ok": True, "error": None, "shop": None, "self_sold": None, "gone": False}

    if not isinstance(resp, dict):
        return _normalize_shop(
            {"ok": False, "error": f"响应格式异常（{type(resp).__name__}），不是 JSON 对象"}
        )

    code = resp.get("code")
    if code != 0:
        message = _clean_text(resp.get("message"))
        if code == ITEM_GONE_CODE:
            # 商品被删/下架：问不出店铺了，但这是个确定的答案
            return _normalize_shop({"gone": True, "error": message})
        return _normalize_shop(
            {"ok": False, "error": f"接口返回失败 (code={code}, message={message})"}
        )

    data = resp.get("data")
    if not isinstance(data, dict):
        return _normalize_shop({"ok": False, "error": "响应缺少 data 字段"})

    merchant_id = _clean_id(data.get("merchantId"))
    if merchant_id == "0":
        return _normalize_shop({"self_sold": True})

    shop = _clean_text(_dig(data, "shopInfoVO", "shopName"))
    if merchant_id is not None:
        return _normalize_shop({"shop": shop, "self_sold": False})
    # 连 merchantId 都没有：只能看店名。有店名就按商家算，什么都没有就当"没看出来"
    if shop is not None:
        return _normalize_shop({"shop": shop, "self_sold": False})
    return _normalize_shop({})


def _normalize_shop(result: dict) -> dict:
    """店铺结果的出口兜底：固定键与类型，界面层不必再判空。

    ok 默认 True：调用点只在"这次没问出结果"时才显式写 ``{"ok": False}``，
    其余都是查到的（自营、商家、商品已删）——默认 False 会让每条成功的结果
    都变成失败。
    """
    ok = result.get("ok", True) is not False
    error = _clean_text(result.get("error"))
    if not ok and not error:
        error = "未知错误"
    self_sold = result.get("self_sold")
    return {
        "ok": ok,
        "error": error,
        "shop": _clean_text(result.get("shop")) if ok else None,
        "self_sold": self_sold if ok and isinstance(self_sold, bool) else None,
        "gone": bool(result.get("gone")) if ok else False,
    }


def _self_hint(services) -> bool:
    """服务标签里有没有「自营 / 官仓直发」。

    抽样 120 件：带这组标签的 27 件全是会员购自营，商家商品零例（见
    dev_notes/261101_市集店铺信息调查.md），所以命中即自营；但**没有标签说明不了
    任何事**——自营商品里只有一半左右带它。

    它是给「主商品已经查不到」的行兜底的：items/info 回 81100001 时问不出店铺，
    而 cluster 自己还带着这组标签，靠它至少能把「自营」标出来，而不是一片空白。
    """
    if not isinstance(services, list):
        return False
    for service in services:
        if not isinstance(service, dict):
            continue
        kind = _clean_text(service.get("type")) or ""
        name = _clean_text(service.get("name")) or ""
        if kind in ("self", "direct") or "自营" in name or "直发" in name:
            return True
    return False


def _clean_id(value) -> str | None:
    """纯数字 ID（clusterId / itemsId / merchantId）归一成字符串；其余返回 None。

    统一成字符串是因为 JSON 里同一个字段可能给数字也可能给字符串，而 0 和 "0"
    在判断「是不是自营」时必须是同一个东西。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    text = _clean_text(value)
    return text if text is not None and text.isdigit() else None


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


def _fmt_reference_price(price_tag) -> str | None:
    """「原价」（网页上现价右边那个划线价）：price + 接口给的货币符号。

    符号单独放在 priceSymbol 里，所以不能直接套 _fmt_price——那会把符号
    一律当成 ¥，接口哪天换成别的符号就会拼错。价格为空的商品（实测是售罄
    的那种）这里返回 None，由界面显示 "—"。
    """
    if not isinstance(price_tag, dict):
        return None
    price = _clean_text(price_tag.get("price"))
    if price is None:
        return None
    symbol = _clean_text(price_tag.get("priceSymbol")) or ""
    if not symbol or price.startswith(("¥", "￥")):  # 价格里自带符号时别拼两个
        return _fmt_price(price)
    return _clean_text(f"{symbol}{price}")


def _is_sold_out(button) -> bool:
    """购买按钮是否处于「已售罄」（buttonState == 2）。

    只有 2 算售罄：3（已下架）、4（未开始）同样"没有市集现价"，但手上没有
    实例可对照，先不替它们编展示文案；状态认不出来时一律当在售，照常显示
    价格——宁可少标一次售罄，也不要凭猜测给商品扣帽子。
    """
    if not isinstance(button, dict):
        return False
    state = button.get("buttonState")
    if isinstance(state, bool) or not isinstance(state, int):  # 字符串等都不认
        return False
    return state == SOLD_OUT_STATE


def _stock_count(button) -> int | None:
    """购买按钮文案里的「当前价格还剩几件」，没有数字时返回 None。

    接口只在货少时才把件数写进文案（实测「最低价仅1件」这种，1~3 件），货还够时
    写「当前最低价」、卖完了写「已售罄」，都没带数字。这两种一律返回 None——界面
    据此不显示件数，宁可少显示一处，也不要凭空编个件数出来。
    """
    if not isinstance(button, dict):
        return None
    match = _STOCK_COUNT_RE.search(_clean_text(button.get("buttonText")) or "")
    if match is None:
        return None
    return _positive_int(int(match.group(1)))


def _positive_int(value) -> int | None:
    """归一成正整数或 None；bool、字符串、0、负数都当"没有"。

    0 件和"没报件数"对界面是同一件事（都不显示），归到一起省得调用处再判一遍。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


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
        "reference_price": _clean_text(result.get("reference_price")),
        "avg_price": _clean_text(result.get("avg_price")),
        "deals": deals,
        "image_url": _clean_text(result.get("image_url")),
        "sold_out": bool(result.get("sold_out")),
        "stock_count": _positive_int(result.get("stock_count")),
        "item_id": _clean_id(result.get("item_id")),
        "self_hint": bool(result.get("self_hint")),
    }
