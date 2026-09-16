"""client 的回归测试：请求形态，以及各种失败都统一抛 ApiError。

接口是无文档的私有接口，调用方靠 ApiError 区分「网络问题」和「接口变了」，
所以每条失败路径都要有覆盖。
"""

import pytest
import requests

import client


class FakeResponse:
    """假 response：只需要 status_code 和 json()。"""

    def __init__(self, payload=None, status_code=200, json_error=None):
        self.payload = {"success": True, "data": {}} if payload is None else payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self.payload


@pytest.fixture
def posted(monkeypatch):
    """替换 requests.post，返回 (调用记录, 可控状态)。

    conftest 的 _offline 已把 requests.post 换成抛 ConnectionError，
    这里再 patch 一次即覆盖之。
    """
    calls = []
    state = {"response": FakeResponse(), "error": None}

    def _post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        if state["error"] is not None:
            raise state["error"]
        return state["response"]

    monkeypatch.setattr(requests, "post", _post)
    return calls, state


def test_success_returns_payload(posted):
    """成功时原样返回接口的 JSON dict。"""
    calls, state = posted
    payload = {"success": True, "data": {"clusterId": "10000008780"}}
    state["response"] = FakeResponse(payload)

    assert client.fetch_cluster("10000008780") == payload


def test_request_shape(posted):
    """请求形态：打对的地址、clusterId 放在 JSON body 里、带上伪装 headers。"""
    calls, _ = posted
    client.fetch_cluster("10000008780")

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == client.API_URL
    assert call["kwargs"]["json"] == {"clusterId": "10000008780"}
    headers = call["kwargs"]["headers"]
    assert "User-Agent" in headers and "Referer" in headers
    assert call["kwargs"]["timeout"] == 10.0


def test_int_cluster_id_is_stringified(posted):
    """clusterId 传 int 也要转成字符串发出去。"""
    calls, _ = posted
    client.fetch_cluster(10000008780)
    assert calls[0]["kwargs"]["json"] == {"clusterId": "10000008780"}


def test_custom_timeout_is_passed_through(posted):
    """配置里的超时要透传给 requests。"""
    calls, _ = posted
    client.fetch_cluster("10000008780", timeout=2.5)
    assert calls[0]["kwargs"]["timeout"] == 2.5


def test_http_error_raises_api_error(posted):
    """非 200 → ApiError，消息里带状态码。"""
    _, state = posted
    state["response"] = FakeResponse(status_code=500)

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster("10000008780")
    assert "500" in str(excinfo.value)


def test_invalid_json_raises_api_error(posted):
    """响应不是 JSON（例如被风控页面截胡）→ ApiError。"""
    _, state = posted
    state["response"] = FakeResponse(json_error=ValueError("Expecting value"))

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster("10000008780")
    assert "JSON" in str(excinfo.value)


def test_business_failure_raises_api_error(posted):
    """HTTP 200 但 success=false → ApiError，消息里带 code 和 message。"""
    _, state = posted
    state["response"] = FakeResponse(
        {"success": False, "code": -412, "message": "请求被拦截"}
    )

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster("10000008780")
    message = str(excinfo.value)
    assert "-412" in message
    assert "请求被拦截" in message


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},                       # 缺 data
        {"success": True, "data": None},         # data 为 None
        {"success": True, "data": "not-a-dict"},  # data 类型不对
    ],
)
def test_missing_data_raises_api_error(posted, payload):
    """success=true 但 data 不是 dict → ApiError（调用方不必再判空）。"""
    _, state = posted
    state["response"] = FakeResponse(payload)

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster("10000008780")
    assert "data" in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("连接被重置"),
        requests.Timeout("读超时"),
        requests.RequestException("其它网络错误"),
    ],
)
def test_network_errors_are_wrapped(posted, error):
    """网络层异常统一包成 ApiError，并保留原始错误信息。"""
    _, state = posted
    state["error"] = error

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster("10000008780")
    assert str(error) in str(excinfo.value)


@pytest.mark.parametrize("payload", [[], 42, "not-an-object", ["a", "b"]])
def test_non_dict_json_raises_api_error(posted, payload):
    """JSON 本身不是对象（列表/数字/字符串）时也要是 ApiError，不能是 AttributeError。"""
    _, state = posted
    state["response"] = FakeResponse(payload)

    with pytest.raises(client.ApiError):
        client.fetch_cluster("10000008780")


def test_unexpected_request_error_is_wrapped(posted):
    """requests 之外的异常（代理层、编码问题）同样归口成 ApiError。"""
    _, state = posted
    state["error"] = RuntimeError("代理层炸了")

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster("10000008780")
    assert "代理层炸了" in str(excinfo.value)


# ---------------- 入参自带检测 ----------------


@pytest.mark.parametrize("cluster_id", ["", "   ", "abc", None, True, "12a34"])
def test_invalid_cluster_id_never_reaches_the_network(posted, cluster_id):
    """clusterId 不合法时直接报错，不发无效请求（接口没有频率承诺，省着点用）。"""
    calls, _ = posted

    with pytest.raises(client.ApiError) as excinfo:
        client.fetch_cluster(cluster_id)
    assert calls == []
    assert "clusterId" in str(excinfo.value)


def test_cluster_id_is_stripped(posted):
    """前后带空白的 ID 会被 strip，不会把空格发给接口。"""
    calls, _ = posted
    client.fetch_cluster("  10000008780  ")
    assert calls[0]["kwargs"]["json"] == {"clusterId": "10000008780"}


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "5", None, True])
def test_invalid_timeout_falls_back_to_default(posted, timeout):
    """非法超时退回默认值：宁可慢一点，也不能让 requests 抛 ValueError 混进错误路径。"""
    calls, _ = posted
    client.fetch_cluster("10000008780", timeout=timeout)
    assert calls[0]["kwargs"]["timeout"] == client.DEFAULT_TIMEOUT


def test_api_error_is_exception():
    """ApiError 必须是普通异常，调用方 except 得住。"""
    assert issubclass(client.ApiError, Exception)
