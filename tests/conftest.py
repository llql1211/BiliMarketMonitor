"""测试公共设施：无头 Qt、临时数据目录、真实数据看门狗。

三条规矩：

1. 任何测试都不得碰真实的 ``data/``——这里把程序眼里的数据路径整体指向
   tmp_path，并在整个会话前后比对 ``data/`` 的内容指纹，被改动过就 fail；
2. 全部离线——真实网络请求在测试里一律被替换（``requests`` 抛 ConnectionError）；
3. 不弹窗——模态对话框与浏览器一律被拦下，界面测试才能无人值守地跑。

GUI 测试用 Qt 的 offscreen 平台，不需要真实显示器；关卡在 import PyQt5
之前落下，所以本模块必须先于任何 PyQt5 导入执行。
"""

import hashlib
import os
import pathlib
import sys
import time

# 必须早于 PyQt5 的导入
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
import requests  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
REAL_DATA = ROOT / "data"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import client  # noqa: E402
import config  # noqa: E402
import links  # noqa: E402
import store  # noqa: E402

# 临时数据目录里的 config.example.toml：间隔调到 0.01 秒，
# 免得真去启动抓取的用例照着默认值干等 2 秒。
EXAMPLE_CONFIG = """\
detail_url_template = "https://example.test/detail?clusterId={clusterId}"
poll_interval_seconds = 0.01
retry_interval_seconds = 0.01
request_timeout_seconds = 1.0
"""

# ---------------- 真实数据看门狗 ----------------


def _snapshot(path: pathlib.Path):
    """目录内容指纹：相对路径 -> sha256（目录不存在时为空）。"""
    if not path.exists():
        return {}
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="session", autouse=True)
def _guard_real_data():
    """会话前后比对真实 data/：被测试写入过就直接 fail。

    这是为一次真实事故加的保险——早期测试直接构造 MainWindow，
    store.sync() 的「以清单为准」语义把用户的 cache.db 清空过。
    """
    before = _snapshot(REAL_DATA)
    yield
    after = _snapshot(REAL_DATA)
    changed = sorted(
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )
    assert not changed, (
        f"测试改动了真实的 data/ 目录：{changed}\n"
        "（测试应当通过 data_files fixture 使用临时目录）"
    )


# ---------------- Qt ----------------


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication（Qt 不允许建第二个）。"""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def wait_until(qapp):
    """驱动事件循环直到条件成立，返回是否在超时前成立。

    跨线程信号是排队投递的，光 sleep 是等不到的，必须转事件循环。
    """

    def _wait(predicate, timeout=5.0, step=0.01):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qapp.processEvents()
            if predicate():
                return True
            time.sleep(step)
        qapp.processEvents()
        return predicate()

    return _wait


@pytest.fixture
def join_thread(qapp):
    """等 QThread 跑完，并冲一次事件队列把排队中的信号送出去。"""

    def _join(thread, timeout=5000):
        finished = thread.wait(timeout)
        qapp.processEvents()
        return finished

    return _join


# ---------------- 临时数据目录 ----------------


@pytest.fixture
def data_files(tmp_path, monkeypatch):
    """把程序眼里的 data/ 整体挪到临时目录，返回该目录的 Path。

    在构造 MainWindow **之前**就打好补丁，窗口从一打开用的就是临时库。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.example.toml").write_text(EXAMPLE_CONFIG, encoding="utf-8")
    (data_dir / "watchlist.txt").write_text("", encoding="utf-8")

    monkeypatch.setattr(config, "data_dir", lambda: str(data_dir))
    monkeypatch.setattr(links, "_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(store, "default_db_path", lambda: str(data_dir / "cache.db"))
    return data_dir


@pytest.fixture
def window(qapp, data_files):
    """构造 MainWindow 的工厂，并在 teardown 收干净线程与窗口。

    用法：``w = window("10000008780 | 名字\\n")``（可选：直接给出清单内容）。
    """
    import app as app_module

    made = []

    def _make(watchlist=None, config_toml=None):
        if watchlist is not None:
            (data_files / "watchlist.txt").write_text(watchlist, encoding="utf-8")
        if config_toml is not None:
            (data_files / "config.toml").write_text(config_toml, encoding="utf-8")
        win = app_module.MainWindow()
        made.append(win)
        return win

    yield _make

    for win in made:
        if win.poller is not None and win.poller.isRunning():
            win.poller.stop()
            win.poller.wait(2000)
        win.close()
        win.deleteLater()
    qapp.processEvents()


# ---------------- 拦下副作用 ----------------


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """禁用真实网络：requests 一律失败（调用方按各自的错误路径处理）。"""

    def _blocked(*args, **kwargs):
        raise requests.ConnectionError("测试中已禁用网络")

    monkeypatch.setattr(requests, "get", _blocked)
    monkeypatch.setattr(requests, "post", _blocked)


@pytest.fixture(autouse=True)
def opened_urls(monkeypatch):
    """拦下 webbrowser.open，返回被打开过的 URL 列表。"""
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url))
    return opened


@pytest.fixture(autouse=True)
def msgboxes(monkeypatch):
    """拦下模态对话框：记录调用并按「否」作答，免得测试卡在弹窗上。

    返回 ``[{"kind", "title", "text"}, ...]``；要改变应答的用例自己再 patch 一次
    QMessageBox.question 即可。
    """
    from PyQt5.QtWidgets import QMessageBox

    calls = []

    def record(kind, answer):
        def _call(parent=None, title="", text="", *args, **kwargs):
            calls.append({"kind": kind, "title": title, "text": text})
            return answer

        return staticmethod(_call)

    monkeypatch.setattr(QMessageBox, "warning", record("warning", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "information", record("information", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "question", record("question", QMessageBox.No))
    return calls


@pytest.fixture
def png_bytes():
    """一张真实的 PNG 字节流（真的让 Qt 编出来，手写 base64 容易编坏）。"""
    from PyQt5.QtCore import QBuffer, QByteArray
    from PyQt5.QtGui import QImage

    image = QImage(4, 4, QImage.Format_RGB32)
    image.fill(0xFF3366)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


@pytest.fixture
def fake_client(monkeypatch):
    """把 client.fetch_cluster 换成可控的假实现。

    用法：``fake_client({"10000008780": parser.parse_cluster(RESP)}, "默认结果")``，
    也可以传异常实例让某个 clusterId 失败；返回调用记录列表。
    """
    calls = []

    def _install(by_id=None, default=None, exc=None):
        by_id = by_id or {}

        def _fetch(cluster_id, timeout=None):
            calls.append({"cluster_id": str(cluster_id), "timeout": timeout})
            if exc is not None:
                raise exc
            if str(cluster_id) in by_id:
                value = by_id[str(cluster_id)]
                if isinstance(value, Exception):
                    raise value
                return value
            if default is None:
                raise client.ApiError(f"测试未提供 {cluster_id} 的响应")
            return default

        monkeypatch.setattr(client, "fetch_cluster", _fetch)
        return calls

    return _install
