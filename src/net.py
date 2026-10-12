"""共用的 HTTP 连接：一个线程一条，用完接着用，不反复重建。

为什么要有这一层：``requests.get`` / ``requests.post`` 每次调用都新建一条连接，
连 SSLContext 都重建一遍（实测单次约 20ms 纯 CPU，还得重走 DNS + TCP + TLS）。
这个程序一启动就要拉两百多张缩略图、一轮抓取要发两百多次请求，全新建连接的
代价在网络不稳的时候会被放大——实测坏网络下单张图**单独**一次请求就要 10s，
两百多条并发排下来，就是十几秒见不到第一张图、一轮抓取几十分钟。

每个线程各持一条 ``requests.Session``（threading.local）：轮询是一轮一个新线程，
拉图是几个常驻 worker，两边都只有一个线程用它，于是不必操心 Session 本身
不是线程安全的那件麻烦事。线程退出时它的连接跟着释放。

测试想拦网络请堵 ``requests.Session.request``（或本模块的 get/post），别只堵
``requests.get``：那两道挡不住走 Session 的调用。
"""

import threading

import requests

_local = threading.local()


def session() -> requests.Session:
    """本线程的共用 Session（头一次调用时建，之后一直用它）。"""
    conn = getattr(_local, "session", None)
    if conn is None:
        conn = requests.Session()
        _local.session = conn
    return conn


def get(url: str, timeout: float) -> bytes:
    """取一段字节（拉图用）。

    状态码不在这里判：拿回来的不是图片，``loadFromData`` 自然解不出来，走的是
    同一条失败路径（跟以前直接 ``requests.get`` 的脾气一致）。
    """
    return session().get(url, timeout=timeout).content


def post(url: str, **kwargs) -> requests.Response:
    """POST 并返回原始响应（接口调用方要自己看状态码和 JSON）。"""
    return session().post(url, **kwargs)
