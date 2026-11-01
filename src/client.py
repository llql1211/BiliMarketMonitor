"""API 封装：调用市集详情接口（免登录、无需 Cookie）。

这是无文档的私有接口，无频率承诺：
- 调用方须保证间隔 >= 2 秒、依次（非并发）请求；
- 若接口加签名/风控，表现为非 200 或 success: false，统一抛 ApiError。

调用方递进来的参数也不做任何信任：clusterId 不是纯数字、超时不是有限正数，
要么在发请求之前就报错，要么退回默认值——绝不让 requests 自己抛
ValueError/TypeError 混进调用方的错误路径里。所有失败出口都只有 ApiError 一个。
"""

import math
import time

import requests

import net

API_URL = "https://mall.bilibili.com/mall-search-items/items_detail/cluster_info"

# 会员购主商品信息（只为拿店铺）。见 dev_notes/261101_市集店铺信息调查.md
ITEM_API_URL = "https://mall.bilibili.com/mall-c-search/items/info"

DEFAULT_TIMEOUT = 10.0

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mall.bilibili.com/",
    "Origin": "https://mall.bilibili.com",
}


class ApiError(Exception):
    """统一的接口错误出口。"""


def fetch_cluster(cluster_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """查询一个商品的 cluster_info，成功返回原始 JSON dict，失败抛 ApiError。"""
    raw_id = cluster_id
    cluster_id = _clean_cluster_id(raw_id)
    if cluster_id is None:
        raise ApiError(f"clusterId 不合法（{raw_id!r}），已跳过请求")

    try:
        # 走 net：同一个线程里连着发的这一轮共用一条连接，不必每条都重握手
        # （一轮几百条，一抖就是几十秒的差别，见 net.py 开头）
        resp = net.post(
            API_URL,
            json={"clusterId": cluster_id},
            headers=_HEADERS,
            timeout=_clean_timeout(timeout),
        )
    except requests.RequestException as err:
        raise ApiError(f"网络请求失败: {err}") from err
    except Exception as err:  # 代理层/编码等其他异常也归口成 ApiError
        raise ApiError(f"请求异常: {err}") from err

    if resp.status_code != 200:
        raise ApiError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as err:
        raise ApiError("响应不是合法 JSON") from err
    if not isinstance(data, dict):  # 接口返回列表/数字时 data.get 会直接崩
        raise ApiError(f"响应不是 JSON 对象（{type(data).__name__}）")

    if not data.get("success"):
        raise ApiError(
            f"接口返回失败 (code={data.get('code')}, message={data.get('message')})"
        )
    if not isinstance(data.get("data"), dict):
        raise ApiError("响应缺少 data 字段")

    return data


def fetch_item(item_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """查询会员购主商品信息，返回原始 JSON dict（店铺就在里面，见 parser）。

    跟 fetch_cluster 的区别：这里 ``success: false`` 也是**答案**，不是失败——
    商品被删时接口回 ``code=81100001``，说明这件 item 已经问不出店铺了，该由
    parser.parse_item_shop 判成"没有店铺"，然后别再反复问。所以只有传输层
    出问题（网络、非 200、不是 JSON、不是对象）才抛 ApiError。
    """
    raw_id = item_id
    item_id = _clean_digits(raw_id)
    if item_id is None:
        raise ApiError(f"itemsId 不合法（{raw_id!r}），已跳过请求")

    try:
        resp = net.get_response(
            ITEM_API_URL,
            params={
                "itemsId": item_id,
                "shopId": 0,          # 前端从详情页 URL 带过来，实测给 0 也能查到
                "itemsVersion": "",
                "v": int(time.time() * 1000),  # 防缓存的时间戳
            },
            headers=_HEADERS,
            timeout=_clean_timeout(timeout),
        )
    except requests.RequestException as err:
        raise ApiError(f"网络请求失败: {err}") from err
    except Exception as err:  # 代理层/编码等其他异常也归口成 ApiError
        raise ApiError(f"请求异常: {err}") from err

    if resp.status_code != 200:
        raise ApiError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as err:
        raise ApiError("响应不是合法 JSON") from err
    if not isinstance(data, dict):
        raise ApiError(f"响应不是 JSON 对象（{type(data).__name__}）")

    return data


def _clean_cluster_id(cluster_id) -> str:
    """clusterId 只认纯数字字符串（int 也接受）；其余返回 None，由调用方报错。"""
    return _clean_digits(cluster_id)


def _clean_digits(value) -> str | None:
    """纯数字 ID（clusterId / itemsId）归一成字符串；其余返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if text.isdigit() else None


def _clean_timeout(timeout) -> float:
    """超时必须是有限正数；非法值（含 inf/nan/负数/字符串）退回默认值。"""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return DEFAULT_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_TIMEOUT
    return float(timeout)
