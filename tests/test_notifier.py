"""notifier 的回归测试：请求形态，以及各种失败都统一抛 NotifyError。

企业微信的规矩是 HTTP 200 只代表送到了，成没成看响应体里的 errcode；
推送又是「错了就再也送不到」的东西，所以每条失败路径都要有覆盖。
"""

import threading
import time

import pytest
import requests

import config
import notifier

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"


def test_webhook_prefix_matches_config():
    """config 里那份前缀校验跟这儿的常量得是同一个。

    两处都没法 import 对方（config 不依赖 Qt，notifier 不依赖项目模块），只好
    各留一份，这个用例就是盯着别写岔的那根绳子——写岔了的后果是配置层放行一个
    发不出去的地址，或者拒掉一个能用的。
    """
    assert config._WECOM_PREFIX == notifier.WECOM_API_PREFIX
    assert WEBHOOK.startswith(notifier.WECOM_API_PREFIX)


class FakeResponse:
    """假 response：只需要 status_code 和 json()。"""

    def __init__(self, payload=None, status_code=200, json_error=None):
        self.payload = {"errcode": 0, "errmsg": "ok"} if payload is None else payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self.payload


@pytest.fixture
def posted(monkeypatch):
    """替换 requests.post，返回 (调用记录, 可控状态)。

    conftest 的 _offline 已把 requests.post 换成抛 ConnectionError，这里再
    patch 一次即覆盖之。
    """
    calls = []
    state = {"response": FakeResponse(), "error": None}

    def _post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        if state["error"] is not None:
            raise state["error"]
        return state["response"]

    monkeypatch.setattr(notifier.requests, "post", _post)
    return calls, state


# ---------------- _post_wecom：请求形态 ----------------

def test_success_sends_markdown(posted):
    """成功路径：POST 到配置里的地址，消息按企业微信的 markdown 格式装。"""
    calls, _ = posted

    notifier._post_wecom(WEBHOOK, "**标题**\n> 正文")

    assert len(calls) == 1
    assert calls[0]["url"] == WEBHOOK
    assert calls[0]["kwargs"]["json"] == {
        "msgtype": "markdown",
        "markdown": {"content": "**标题**\n> 正文"},
    }
    assert calls[0]["kwargs"]["timeout"] == notifier.WECOM_TIMEOUT


@pytest.mark.parametrize(
    "webhook",
    [
        "",
        "   ",
        None,
        123,
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",  # 不是 https
        "https://example.com/hook?key=x",  # 不是企业微信的域名
        "https://qyapi.weixin.qq.com.evil.test/hook",  # 前缀像但不落在同一域名
    ],
)
def test_bad_webhook_is_rejected(posted, webhook):
    """地址不对就不发请求——推给别人的群比不推糟糕得多。"""
    calls, _ = posted

    with pytest.raises(notifier.NotifyError):
        notifier._post_wecom(webhook, "正文")

    assert calls == []


@pytest.mark.parametrize("markdown", ["", "   ", None, 42])
def test_empty_markdown_is_rejected(posted, markdown):
    """空消息不发：企业微信会拒收，报出来的还是句看不懂的 errcode。"""
    calls, _ = posted

    with pytest.raises(notifier.NotifyError):
        notifier._post_wecom(WEBHOOK, markdown)

    assert calls == []


def test_webhook_is_stripped(posted):
    """地址两头的手写空格自己剪掉，跟其他配置项一个待遇。"""
    calls, _ = posted

    notifier._post_wecom(f"  {WEBHOOK}  ", "正文")

    assert calls[0]["url"] == WEBHOOK


# ---------------- _post_wecom：失败路径 ----------------

def test_http_error(posted):
    """HTTP 不为 200：报状态码。"""
    _, state = posted
    state["response"] = FakeResponse(status_code=500)

    with pytest.raises(notifier.NotifyError, match="500"):
        notifier._post_wecom(WEBHOOK, "正文")


def test_business_error(posted):
    """HTTP 200 但 errcode 不是 0：这才是最常见的失败，得把 errmsg 带出来。"""
    _, state = posted
    state["response"] = FakeResponse(
        {"errcode": 93000, "errmsg": "invalid webhook url, hint: [xxx]"}
    )

    with pytest.raises(notifier.NotifyError, match="93000"):
        notifier._post_wecom(WEBHOOK, "正文")


def test_response_is_not_json(posted):
    """响应不是 JSON（被网关拦了、返回一页 HTML 之类）。"""
    _, state = posted
    state["response"] = FakeResponse(json_error=ValueError("Expecting value"))

    with pytest.raises(notifier.NotifyError, match="不是合法 JSON"):
        notifier._post_wecom(WEBHOOK, "正文")


def test_response_is_not_an_object(posted):
    """响应是列表/数字：再往下 .get 会直接崩，得在这儿拦住。"""
    _, state = posted
    state["response"] = FakeResponse([1, 2, 3])

    with pytest.raises(notifier.NotifyError, match="不是 JSON 对象"):
        notifier._post_wecom(WEBHOOK, "正文")


def test_connection_error(posted):
    """网络不通：requests 的异常统一归口成 NotifyError。"""
    _, state = posted
    state["error"] = requests.ConnectionError("连不上")

    with pytest.raises(notifier.NotifyError, match="网络请求失败"):
        notifier._post_wecom(WEBHOOK, "正文")


def test_network_error_does_not_leak_the_webhook(posted):
    """报错文案里不能出现 webhook 的 key。

    requests 的网络异常会把整个 URL 抄进消息（带上 ?key=xxx），而这条消息要打到
    控制台、还要进状态栏。webhook 等同于发消息的权限，不能因为报个错就漏出去。
    """
    _, state = posted
    state["error"] = requests.ConnectionError(
        "HTTPSConnectionPool(host='qyapi.weixin.qq.com', port=443): Max retries "
        "exceeded with url: /cgi-bin/webhook/send?key=abc-123 (Caused by "
        "NewConnectionError('<urllib3.connection.HTTPSConnection object>'))"
    )

    with pytest.raises(notifier.NotifyError) as err:
        notifier._post_wecom(WEBHOOK, "正文")

    assert "abc-123" not in str(err.value)
    assert "key=***" in str(err.value)


def test_business_error_does_not_leak_the_webhook(posted):
    """对方的 errmsg 回显地址时也一样：那是别人给的字符串，同样得过一遍。"""
    _, state = posted
    state["response"] = FakeResponse(
        {"errcode": 93000, "errmsg": f"invalid webhook url: {WEBHOOK}"}
    )

    with pytest.raises(notifier.NotifyError) as err:
        notifier._post_wecom(WEBHOOK, "正文")

    assert "abc-123" not in str(err.value)


def test_redact_keeps_other_text(posted):
    """只抹 key，别把整句话也抹了——不然失败原因就没法看了。"""
    assert notifier._redact_webhook("url: /x?key=abc&a=1 failed") == (
        "url: /x?key=***&a=1 failed"
    )
    assert notifier._redact_webhook("没有 key 的一句话") == "没有 key 的一句话"


def test_unexpected_request_exception(posted):
    """requests 之外的异常（代理层、编码）也归到同一个出口。"""
    _, state = posted
    state["error"] = RuntimeError("代理炸了")

    with pytest.raises(notifier.NotifyError, match="请求异常"):
        notifier._post_wecom(WEBHOOK, "正文")


# ---------------- Notifier：线程与信号 ----------------

def test_send_emits_sent_on_success(qapp, wait_until, posted):
    """发得出去就发 sent 信号：结果回主线程，界面才敢动。"""
    notifier_ = notifier.Notifier()
    got = []
    notifier_.sent.connect(got.append)
    notifier_.failed.connect(lambda reason: got.append(("失败", reason)))

    notifier_.send(WEBHOOK, "**标题**")

    assert wait_until(lambda: bool(got))
    assert got[0] == "企业微信推送成功"


def test_send_emits_failed_on_error(qapp, wait_until, posted):
    """发不出去发 failed 信号，原因原样带回来（状态栏要显示它）。"""
    _, state = posted
    state["response"] = FakeResponse({"errcode": 93000, "errmsg": "invalid webhook url"})
    notifier_ = notifier.Notifier()
    got = []
    notifier_.sent.connect(got.append)
    notifier_.failed.connect(got.append)

    notifier_.send(WEBHOOK, "**标题**")

    assert wait_until(lambda: bool(got))
    assert "93000" in got[0]


def test_work_reports_unexpected_errors(qapp, wait_until, monkeypatch):
    """_post_wecom 之外的意外异常也得报出来。

    子线程里的异常没人接，默认只会打到 stderr：推送因此失败时，界面上
    一句提示都没有，用户以为通知发出去了。
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("炸")

    monkeypatch.setattr(notifier, "_post_wecom", _boom)
    notifier_ = notifier.Notifier()
    got = []
    notifier_.failed.connect(got.append)

    notifier_.send(WEBHOOK, "**标题**")

    assert wait_until(lambda: bool(got))
    assert "炸" in got[0]


def test_send_does_not_block():
    """send 立刻返回，活儿在别处干：网络那几秒不该压在界面上。"""
    started = threading.Event()

    def _slow(*args, **kwargs):
        started.set()
        time.sleep(0.3)

    notifier_ = notifier.Notifier()
    notifier_._work = _slow  # 换成一个睡一小会儿的桩，好看清 send 自己花了多久
    before = time.monotonic()

    notifier_.send(WEBHOOK, "**标题**")

    spent = time.monotonic() - before
    assert spent < 0.2, f"send 等了 {spent:.3f} 秒，说明它在主线程里同步发了"
    assert started.wait(2.0), "活儿没起来"
    time.sleep(0.35)  # 等那个线程睡完，别把尾巴留给后面的用例
