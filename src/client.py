"""API 封装：调用市集详情接口（免登录、无需 Cookie）。

这是无文档的私有接口，无频率承诺：
- 调用方须保证间隔 >= 2 秒、依次（非并发）请求；
- 若接口加签名/风控，表现为非 200 或 success: false，统一抛 ApiError。
"""

import requests

API_URL = "https://mall.bilibili.com/mall-search-items/items_detail/cluster_info"

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


def fetch_cluster(cluster_id: str, timeout: float = 10) -> dict:
    """查询一个商品的 cluster_info，成功返回原始 JSON dict，失败抛 ApiError。"""
    try:
        resp = requests.post(
            API_URL,
            json={"clusterId": str(cluster_id)},
            headers=_HEADERS,
            timeout=timeout,
        )
    except requests.RequestException as err:
        raise ApiError(f"网络请求失败: {err}") from err

    if resp.status_code != 200:
        raise ApiError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as err:
        raise ApiError("响应不是合法 JSON") from err

    if not data.get("success"):
        raise ApiError(
            f"接口返回失败 (code={data.get('code')}, message={data.get('message')})"
        )
    if not isinstance(data.get("data"), dict):
        raise ApiError("响应缺少 data 字段")

    return data
