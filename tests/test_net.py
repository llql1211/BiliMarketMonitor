"""net 的回归测试：连接按线程复用，而且测试里绝不该真发出去请求。"""

import threading

import pytest
import requests

import net


def test_same_thread_reuses_one_session():
    """同一个线程里连着发的请求共用一条连接（这正是要它的原因）。"""
    assert net.session() is net.session()


def test_each_thread_gets_its_own_session():
    """各线程各持一条：Session 本身不是线程安全的，不跨线程分享最省心。"""
    main_session = net.session()
    other = []
    threads = [
        threading.Thread(target=lambda: other.append(net.session())) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert other[0] is not main_session
    assert other[1] is not main_session


def test_get_returns_the_body(monkeypatch):
    """get 交回的是字节（调用方拿去 loadFromData），不是响应对象。"""
    session = _Session(_Resp(b"\x89PNG-fake"))
    monkeypatch.setattr(net, "session", lambda: session)

    assert net.get("https://example.test/a.png", timeout=3) == b"\x89PNG-fake"


def test_get_passes_the_timeout(monkeypatch):
    """超时照旧透传（拉图那头靠它兜住慢 CDN）。"""
    session = _Session(_Resp())
    monkeypatch.setattr(net, "session", lambda: session)

    net.get("https://example.test/a.png", timeout=7.5)

    assert session.gets == [("https://example.test/a.png", 7.5)]


def test_post_returns_the_response(monkeypatch):
    """post 交回原始响应：接口调用方要自己看状态码和 JSON。"""
    resp = _Resp()
    session = _Session(resp)
    monkeypatch.setattr(net, "session", lambda: session)

    assert net.post("https://example.test/api", json={"a": 1}) is resp
    assert session.posts == [("https://example.test/api", {"json": {"a": 1}})]


class _Resp:
    """假的响应：只需要 content。"""

    def __init__(self, content=b""):
        self.content = content


class _Session:
    """假的 Session：只记下被怎么用了。"""

    def __init__(self, resp):
        self.resp = resp
        self.gets = []
        self.posts = []

    def get(self, url, timeout=None):
        self.gets.append((url, timeout))
        return self.resp

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.resp


def test_offline_guard_covers_the_session(monkeypatch):
    """conftest 的断网关卡必须连 Session 那条路一起堵上。

    这条用例是给关卡本身上的保险：走 net（也就是走 Session）时，若关卡只堵了
    requests.get/post，这里就会真的发出去一个请求——而不是抛 ConnectionError。
    """
    with pytest.raises(requests.ConnectionError):
        net.get("https://example.test/should-not-go-out.png", timeout=1)
