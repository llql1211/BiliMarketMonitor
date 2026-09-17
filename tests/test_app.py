"""界面与调度层测试：MainWindow 渲染/清单操作、PollerThread 轮询、缩略图线程。

所有用例都在 Qt offscreen 平台上跑；数据走 conftest 的临时目录，
网络请求与浏览器一律被替换，见 tests/conftest.py。
"""

import time

import pytest
import requests
from PyQt5.QtCore import QMimeData, QModelIndex, QPoint, QRect, Qt
from PyQt5.QtGui import (
    QDrag,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QMessageBox,
    QTableWidgetSelectionRange,
)

import app as app_module
import client
import links
import parser
import theme


# ---------------- 工具 ----------------


def _raw_response(
    name="测试商品", price="¥44", avg=205, deals=3, img="//i0.hdslb.com/bfs/x.jpg",
    deal_times=None,
):
    """构造一份 cluster_info 原始响应（走真实 parser，避免手搓结果 dict）。

    deal_times 给成交时间（用于高亮相关用例）；不给就按 deals 条数全填「8天前」——
    8 天在默认 24 小时阈值之外，不会被误判成「近期成交」。
    """
    data = {
        "clusterBasicInfoFloorVO": {"clusterName": name},
        "clusterPriceFloorVO": {"priceTag": {"firstPrice": price}},
    }
    if avg is not None:
        times = ["8天前"] * deals if deal_times is None else list(deal_times)
        data["clusterRecentBuyFloorVO"] = {
            "avgPrice": avg,
            "recentDeals": [{"dealPrice": "¥205", "dealTime": t} for t in times],
        }
    if img:
        data["clusterHeaderFloorVO"] = {"clusterImgList": [img]}
    return {"success": True, "data": data}


def raw_ok(**kwargs):
    """解析成功的原始响应——给假的 fetch_cluster 用（线程里还会再 parse 一次）。"""
    return _raw_response(**kwargs)


def result_ok(**kwargs):
    """解析成功的结果结构——给直接调 OnResultReady 的用例用。"""
    return parser.parse_cluster(_raw_response(**kwargs))


def result_fail(message="HTTP 500"):
    """解析失败的统一结果结构。"""
    return parser.parse_error(client.ApiError(message))


def _tasks(ids):
    """PollerThread 要的 [(行号, LinkEntry)]。"""
    return [(i, links.LinkEntry(cid, f"名{cid}", "")) for i, cid in enumerate(ids)]


def _fetch_by_id(mapping, log=None):
    """按 clusterId 返回结果的假 fetch_cluster；mapping 里放异常即表示该条失败。"""

    def _fetch(cluster_id, timeout=None):
        if log is not None:
            log.append(str(cluster_id))
        value = mapping.get(str(cluster_id), client.ApiError("测试未提供响应"))
        if isinstance(value, Exception):
            raise value
        return value

    return _fetch


class FakePoller:
    """只实现界面用到的那几个方法，用来在不起真线程的情况下测按钮逻辑。"""

    def __init__(self, running=True, paused=False):
        self._running = running
        self._paused = paused
        self.stopped = False

    def isRunning(self):
        return self._running

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self.stopped = True
        self._paused = False
        self._running = False

    def wait(self, timeout=0):
        return True


def _fake_add_dialog(monkeypatch, text, accepted=True):
    """把「添加」弹窗换成直接给文本的假弹窗。"""

    class _Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec_(self):
            return QDialog.Accepted if accepted else QDialog.Rejected

        def text(self):
            return text

    monkeypatch.setattr(app_module, "AddDialog", _Dialog)


# ---------------- 初始界面 ----------------


def test_initial_layout(window):
    """表头、列数与按钮初始状态：抓取相关按钮在空闲时应当是灰的。"""
    w = window()
    assert w.windowTitle() == "市集商品价格监视器"
    assert w.table.columnCount() == len(app_module.HEADERS)
    headers = [
        w.table.horizontalHeaderItem(i).text() for i in range(w.table.columnCount())
    ]
    assert headers == app_module.HEADERS
    assert w.table.rowCount() == 0
    assert w.progress_label.text() == "就绪"
    assert w.btn_fetch.isEnabled()
    assert not w.btn_pause.isEnabled()
    assert not w.btn_stop.isEnabled()


def test_link_header_tooltip_explains_template(window):
    """「链接」列表头挂的是配置里的模板说明，别又挂回主窗口上。"""
    w = window()
    tip = w.table.horizontalHeaderItem(app_module.COL_LINK).toolTip()
    assert w.config["detail_url_template"] in tip


def test_idle_buttons_enabled(window):
    """空闲态下清单管理类按钮都应可点。"""
    w = window()
    for btn in (w.btn_add, w.btn_delete, w.btn_normalize, w.btn_refresh_list):
        assert btn.isEnabled()


# ---------------- 清单载入 -----------------


def test_load_watchlist_builds_rows_and_syncs_cache(window, data_files):
    """载入清单：建行、缓存跟随、状态栏给出计数。"""
    w = window("10000008780 | 甲\n# 注释行\n10000000002\n")
    assert w.LoadWatchlist(str(data_files / "watchlist.txt")) is True
    assert [item["entry"].cluster_id for item in w.rows] == ["10000008780", "10000000002"]
    assert w.table.rowCount() == 2
    assert sorted(row["cluster_id"] for row in w.store.get_all()) == [
        "10000000002",
        "10000008780",
    ]
    assert "共 2 件商品" in w.progress_label.text()


def test_load_watchlist_reports_read_error(window, data_files, msgboxes):
    """读不到文件要弹提示并返回 False；show_errors=False 时不打扰用户。"""
    w = window()
    missing = str(data_files / "missing.txt")
    assert w.LoadWatchlist(missing) is False
    assert msgboxes[-1]["kind"] == "warning"

    before = len(msgboxes)
    assert w.LoadWatchlist(missing, show_errors=False) is False
    assert len(msgboxes) == before


def test_pending_row_renders_placeholders(window):
    """还没抓取的行：名字是「…」，价格/成交是「—」，链接列可点。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    assert w.table.item(0, app_module.COL_NAME).text() == app_module.PENDING_TEXT
    for col in (
        app_module.COL_PRICE,
        app_module.COL_AVG,
        app_module.COL_DEAL_BASE,
        app_module.COL_DEAL_BASE + 2,
    ):
        assert w.table.item(0, col).text() == app_module.NO_DATA_TEXT
    link = w.table.item(0, app_module.COL_LINK)
    assert link.text() == "打开"
    assert link.toolTip() == "https://example.test/detail?clusterId=10000008780"
    assert w.row_urls[0] == link.toolTip()


def test_row_prefers_cached_name_and_requests_thumbnail(window, monkeypatch):
    """缓存里有名字和缩略图时直接显示，并按 CDN 裁剪后缀去取图。"""
    w = window("10000008780\n")
    w.store.upsert_item("10000008780", "缓存里的名字", "https://img.test/a.jpg")
    fetched = []
    monkeypatch.setattr(w.image_fetcher, "fetch", lambda row, url: fetched.append((row, url)))

    w.LoadWatchlist(w.watchlist_path)
    assert w.table.item(0, app_module.COL_NAME).text() == "缓存里的名字"
    assert fetched == [(0, "https://img.test/a.jpg" + app_module.IMAGE_SUFFIX)]


def test_make_cell_tooltip_rules(window):
    """tooltip 默认是该格全文，但占位符和空文本不挂。"""
    w = window()
    assert w.MakeCell("¥44").toolTip() == "¥44"
    assert w.MakeCell(app_module.NO_DATA_TEXT).toolTip() == ""
    assert w.MakeCell(app_module.PENDING_TEXT).toolTip() == ""
    assert w.MakeCell("").toolTip() == ""
    assert w.MakeCell("x", tooltip="自定义").toolTip() == "自定义"
    assert w.MakeCell("x", align=Qt.AlignCenter).textAlignment() & Qt.AlignHCenter


# ---------------- 抓取结果回填 -----------------


def test_result_ready_updates_row_cache_and_name(window, monkeypatch):
    """抓到结果：表格刷新、缓存写入、学到的名字记进清单条目、顺带取缩略图。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    picked = []
    monkeypatch.setattr(w.image_fetcher, "fetch", lambda row, url: picked.append((row, url)))
    w.names_learned = False

    w.OnResultReady(0, result_ok(name="抓到的新名字"))

    assert w.table.item(0, app_module.COL_NAME).text() == "抓到的新名字"
    assert w.table.item(0, app_module.COL_NAME).toolTip() == "抓到的新名字"
    assert w.table.item(0, app_module.COL_PRICE).text() == "¥44"
    assert w.table.item(0, app_module.COL_AVG).text() == "¥205"
    assert w.table.item(0, app_module.COL_DEAL_BASE).text() == "¥205 · 8天前"
    assert w.rows[0]["entry"].name == "抓到的新名字"
    assert w.names_learned is True
    assert w.store.get_item("10000008780")["name"] == "抓到的新名字"
    assert picked == [(0, "https://i0.hdslb.com/bfs/x.jpg" + app_module.IMAGE_SUFFIX)]


def test_result_ready_leaves_missing_deals_blank(window):
    """成交不足 3 条时，多出来的格子留「—」。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_ok(deals=1))
    assert w.table.item(0, app_module.COL_DEAL_BASE).text() == "¥205 · 8天前"
    assert w.table.item(0, app_module.COL_DEAL_BASE + 1).text() == app_module.NO_DATA_TEXT


def test_result_ready_failure_marks_name_cell(window):
    """查询失败：名字格标「（查询失败）」并把原因放进 tooltip。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_fail("HTTP 500"))

    cell = w.table.item(0, app_module.COL_NAME)
    assert cell.text() == app_module.FAILED_TEXT
    assert cell.toolTip() == "HTTP 500"
    assert w.table.item(0, app_module.COL_PRICE).text() == app_module.NO_DATA_TEXT
    assert w.rows[0]["values"]["ok"] is False


def test_failure_keeps_known_name(window):
    """失败时若本来就有名字，保留名字，只把原因放进 tooltip。"""
    w = window("10000008780 | 已知名字\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_fail("读取超时"))

    cell = w.table.item(0, app_module.COL_NAME)
    assert cell.text() == "已知名字"
    assert cell.toolTip() == "读取超时"


def test_result_for_unknown_row_is_ignored(window):
    """行号越界的结果直接丢掉（线程与表格重建可能有竞态）。"""
    w = window()
    w.OnResultReady(7, result_ok())  # 不抛异常即通过
    assert w.table.rowCount() == 0


def test_rebuild_rows_keeps_or_drops_values(window):
    """重建表格：keep_values 默认保留本次抓到的数据，False 时退回占位符。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_ok(price="¥99"))

    w.RebuildRows()
    assert w.table.item(0, app_module.COL_PRICE).text() == "¥99"

    w.RebuildRows(keep_values=False)
    assert w.table.item(0, app_module.COL_PRICE).text() == app_module.NO_DATA_TEXT


def test_progress_updates_label(window):
    """进度信号直接落到状态栏。"""
    w = window()
    w.OnProgress(3, 12)
    assert w.progress_label.text() == "查询中 3/12"


# ---------------- 按钮状态 ----------------


def test_set_busy_toggles_buttons(window):
    """抓取中清单管理类按钮置灰、暂停/停止放开；回到空闲时反过来并复位文案。"""
    w = window()
    w.SetBusy(True)
    assert not w.btn_add.isEnabled()
    assert not w.btn_delete.isEnabled()
    assert not w.btn_normalize.isEnabled()
    assert not w.btn_refresh_list.isEnabled()
    assert not w.btn_fetch.isEnabled()
    assert w.btn_pause.isEnabled() and w.btn_stop.isEnabled()

    w.btn_pause.setText(app_module.RESUME_TEXT)
    w.SetBusy(False)
    assert w.btn_add.isEnabled() and w.btn_fetch.isEnabled()
    assert not w.btn_pause.isEnabled() and not w.btn_stop.isEnabled()
    assert w.btn_pause.text() == app_module.PAUSE_TEXT


def test_toggle_pause_swaps_button_text(window):
    """暂停/继续：按钮文案与状态栏都要跟着状态走。"""
    w = window()
    w.poller = FakePoller()

    w.OnTogglePause()
    assert w.poller.is_paused() is True
    assert w.btn_pause.text() == app_module.RESUME_TEXT
    assert "已暂停" in w.progress_label.text()

    w.OnTogglePause()
    assert w.poller.is_paused() is False
    assert w.btn_pause.text() == app_module.PAUSE_TEXT
    assert w.progress_label.text() == "继续抓取…"


def test_toggle_pause_ignored_when_not_polling(window):
    """没在抓取时点暂停应当什么都不做。"""
    w = window()
    w.poller = FakePoller(running=False)
    w.OnTogglePause()
    assert w.btn_pause.text() == app_module.PAUSE_TEXT
    assert w.progress_label.text() == "就绪"


def test_stop_fetch_marks_state_and_disables_buttons(window):
    """停止抓取：置停止标记、停掉线程、暂停/停止按钮一起置灰。"""
    w = window()
    w.poller = FakePoller()
    w.SetBusy(True)

    w.OnStopFetch()
    assert w.poller_stopped is True
    assert w.poller.stopped is True
    assert not w.btn_pause.isEnabled()
    assert not w.btn_stop.isEnabled()
    assert w.progress_label.text() == "正在停止…"


def test_stop_fetch_ignored_when_not_polling(window):
    """空闲时点停止不应改动状态。"""
    w = window()
    w.poller = FakePoller(running=False)
    w.OnStopFetch()
    assert w.poller_stopped is False
    assert w.poller.stopped is False


def test_poll_finished_after_stop(window):
    """停止后的收尾：提示结果保留，按钮回到空闲态，且不写回清单。"""
    w = window()
    w.SetBusy(True)
    w.poller_stopped = True
    w.names_learned = True

    w.OnPollFinished()
    assert w.progress_label.text() == "已停止抓取，已抓到的结果保留"
    assert w.btn_fetch.isEnabled()


def test_poll_finished_writes_names_back(window, data_files):
    """正常收尾且学到新名字时，顺手把清单刷新一次。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_ok(name="学到的新名字"))
    w.names_learned = True
    w.SetBusy(True)

    w.OnPollFinished()
    assert "写回" in w.progress_label.text()
    content = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    assert "10000008780 | 学到的新名字" in content


def test_poll_finished_without_new_names(window, data_files):
    """没学到新名字就不动清单，只报完成。"""
    w = window("10000008780 | 甲\n")
    w.LoadWatchlist(w.watchlist_path)
    before = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    w.names_learned = False
    w.poller_stopped = False
    w.SetBusy(True)

    w.OnPollFinished()
    assert w.progress_label.text() == "完成"
    assert (data_files / "watchlist.txt").read_text(encoding="utf-8") == before


# ---------------- 添加 / 删除 / 整理 ----------------


def test_add_appends_new_items_and_reports_skipped(window, data_files, monkeypatch):
    """「添加」：批量粘贴时批内去重，认不出的行计数上报。"""
    w = window("10000008780 | 甲\n")
    w.LoadWatchlist(w.watchlist_path)
    _fake_add_dialog(
        monkeypatch,
        "10000000002 | 乙\n"
        "10000000002\n"
        "https://mall.bilibili.com/neul-next/resell/detail.html?clusterId=10000000003&share_medium=android\n"
        "乱写的一行\n",
    )

    w.OnAdd()
    assert [item["entry"].cluster_id for item in w.rows] == [
        "10000008780",
        "10000000002",
        "10000000003",
    ]
    assert w.table.rowCount() == 3
    content = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    assert "10000000002 | 乙" in content
    assert "10000000003" in content
    assert "已添加 2 件商品" in w.progress_label.text()
    assert "1 行无法识别" in w.progress_label.text()


def test_add_reports_duplicates_already_in_list(window, monkeypatch):
    """已经在清单里的条目不再重复添加，但要告诉用户忽略了几个。"""
    w = window("10000008780 | 甲\n")
    w.LoadWatchlist(w.watchlist_path)
    _fake_add_dialog(monkeypatch, "10000008780\n10000000009\n")

    w.OnAdd()
    assert len(w.rows) == 2
    assert "已添加 1 件商品" in w.progress_label.text()
    assert "1 条已存在" in w.progress_label.text()


def test_add_all_known_items_tells_user(window, monkeypatch):
    """输入的全是已有商品时不写文件，只提示没有新商品。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    _fake_add_dialog(monkeypatch, "10000008780\n")

    w.OnAdd()
    assert len(w.rows) == 1
    assert "没有新商品" in w.progress_label.text()


def test_add_nothing_recognized_warns(window, msgboxes, monkeypatch):
    """一行都认不出来时弹提示，什么都不加。"""
    w = window()
    _fake_add_dialog(monkeypatch, "乱七八糟\n# 注释行\n")

    w.OnAdd()
    assert len(w.rows) == 0
    assert msgboxes[-1]["kind"] == "warning"
    assert "没有可添加的商品" in msgboxes[-1]["title"]


def test_add_cancelled_changes_nothing(window, data_files, monkeypatch):
    """取消弹窗后清单和界面都不变。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    before = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    _fake_add_dialog(monkeypatch, "10000000002\n", accepted=False)

    w.OnAdd()
    assert len(w.rows) == 1
    assert (data_files / "watchlist.txt").read_text(encoding="utf-8") == before


def test_delete_selected_removes_row_file_and_cache(window, data_files, monkeypatch):
    """删除选中：界面、清单、缓存三处一起删。"""
    w = window("10000008780 | 甲\n10000000002 | 乙\n")
    w.LoadWatchlist(w.watchlist_path)
    w.table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    w.OnDeleteSelected()
    assert [item["entry"].cluster_id for item in w.rows] == ["10000000002"]
    assert w.store.get_item("10000008780") is None
    content = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    assert "10000008780" not in content
    assert "10000000002" in content
    assert "已删除 1 件商品" in w.progress_label.text()


def test_delete_without_selection_asks_for_one(window, msgboxes):
    """没选中任何行时给个提示，不做删除。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    w.table.clearSelection()

    w.OnDeleteSelected()
    assert msgboxes[-1]["kind"] == "information"
    assert "请先" in msgboxes[-1]["text"]
    assert len(w.rows) == 1


def test_delete_cancelled_keeps_everything(window, data_files):
    """在确认框里选「否」：什么都不删（msgboxes fixture 默认答否）。"""
    w = window("10000008780 | 甲\n10000000002 | 乙\n")
    w.LoadWatchlist(w.watchlist_path)
    before = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    w.table.selectRow(0)

    w.OnDeleteSelected()
    assert len(w.rows) == 2
    assert w.store.get_item("10000008780") is not None
    assert (data_files / "watchlist.txt").read_text(encoding="utf-8") == before


def test_refresh_list_picks_up_external_edits(window, data_files):
    """手工改过清单后点「刷新商品列表」：新增补行，已抓到的数据原地保留。"""
    w = window("10000008780 | 甲\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_ok(price="¥99", name="甲"))
    (data_files / "watchlist.txt").write_text(
        "10000008780 | 甲\n10000000002 | 乙\n", encoding="utf-8"
    )

    w.OnRefreshList()
    assert [item["entry"].cluster_id for item in w.rows] == ["10000008780", "10000000002"]
    assert w.table.item(0, app_module.COL_PRICE).text() == "¥99"  # 已抓到的价格不丢
    assert w.table.item(0, app_module.COL_NAME).text() == "甲"
    assert w.table.item(0, app_module.COL_DEAL_BASE).text() == "¥205 · 8天前"
    assert w.table.item(1, app_module.COL_PRICE).text() == app_module.NO_DATA_TEXT  # 新行待抓
    assert "清单已同步" in w.progress_label.text()


def test_refresh_list_keeps_failed_rows_marked(window, data_files):
    """刷新时连「查询失败」的结果一起保留：错误原因不会因为刷新被抹掉。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_fail("HTTP 500"))

    w.OnRefreshList()
    cell = w.table.item(0, app_module.COL_NAME)
    assert cell.text() == app_module.FAILED_TEXT
    assert cell.toolTip() == "HTTP 500"


def test_normalize_rewrites_watchlist(window, data_files):
    """「整理清单」把链接和裸 ID 统一成 `clusterId | 名字` 的规范格式。"""
    w = window(
        "# 手写的注释\n"
        "https://mall.bilibili.com/neul-next/resell/detail.html"
        "?clusterId=10000008780&share_medium=android\n"
        "10000000002\n"
    )
    w.LoadWatchlist(w.watchlist_path)

    w.OnNormalize()
    content = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0].startswith("# 监视清单")
    assert "10000008780" in lines
    assert "10000000002" in lines
    assert "https://mall.bilibili.com" not in content
    assert "清单已按规范格式整理" in w.progress_label.text()


def test_save_watchlist_prefers_freshest_name(window, data_files):
    """回写时名字的优先级：本次抓到的 > 缓存 > 清单里的旧名。"""
    w = window("10000008780 | 清单里的旧名\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnResultReady(0, result_ok(name="接口给的新名"))

    assert w.SaveWatchlist() is True
    assert "10000008780 | 接口给的新名" in (data_files / "watchlist.txt").read_text(
        encoding="utf-8"
    )


def test_click_on_link_column_opens_browser(window, opened_urls):
    """只有「链接」列被点才开浏览器，其他列不理会。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)

    w.OnCellClick(0, app_module.COL_LINK)
    assert opened_urls == [w.row_urls[0]]

    w.OnCellClick(0, app_module.COL_NAME)
    assert len(opened_urls) == 1


# ---------------- 拖拽排序 ----------------


def _ids(w):
    """行模型里各行的 clusterId，按表格顺序。"""
    return [item["entry"].cluster_id for item in w.rows]


def _selected(w):
    """当前选中的行号（升序）。"""
    return sorted({index.row() for index in w.table.selectedIndexes()})


def _loaded(window, *items):
    """建窗口并把清单载入表格（拖拽相关的用例都要先有行）。"""
    w = window("".join(f"{cid} | {name}\n" for cid, name in items))
    w.LoadWatchlist(w.watchlist_path)
    return w


@pytest.mark.parametrize(
    "sources, target, expected",
    [
        ([0], 2, "B A C D"),      # 往下挪：插到原 2 号前面
        ([3], 0, "D A B C"),      # 往上挪
        ([1, 2], 4, "A D B C"),   # 多选整块挪到末尾
        ([0, 1], 0, "A B C D"),   # 原地放下
        ([0, 1], 2, "A B C D"),   # 挪到紧挨着的下一行：等于没动
    ],
)
def test_move_rows(sources, target, expected):
    """行号按挪动前的下标算，被挪的行之间保持原有相对顺序。"""
    assert app_module.move_rows(list("ABCD"), sources, target) == expected.split()


@pytest.mark.parametrize(
    "sources, target",
    [
        ([], 0),         # 没拖任何行
        ([9], 0),        # 行号越界
        ([-1], 0),
        (["0"], 0),      # 行号不是整数
        ([0], "1"),      # 落点不是整数
        (None, 0),       # 形状不对
    ],
)
def test_move_rows_degrades(sources, target):
    """入参不合法就原样返回：排序是给人看的，宁可不动也不能把清单弄乱。"""
    assert app_module.move_rows(list("ABCD"), sources, target) == list("ABCD")


def test_reorder_enabled_only_when_idle(window):
    """空闲时可以拖行；抓取中关掉（轮询任务记的是行号，中途插队会填错行）。"""
    w = window()
    assert w.table.dragDropMode() == QAbstractItemView.InternalMove
    w.SetBusy(True)
    assert w.table.dragDropMode() == QAbstractItemView.NoDragDrop
    w.SetBusy(False)
    assert w.table.dragDropMode() == QAbstractItemView.InternalMove


def test_table_rejects_foreign_drops(window):
    """只有自己拖自己才受理：从文件管理器拖进来的内容一律不理。

    （拖拽事件只存 QMimeData 的指针、不接管所有权，所以得留个引用给垃圾回收。）
    """
    w = window()
    mime = QMimeData()
    event = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)

    w.table.dragEnterEvent(event)
    assert not event.isAccepted()


def test_drop_reorders_rows_table_and_watchlist(window, data_files):
    """拖拽排序：行模型、表格、清单三处顺序一致，并按新顺序写回文件。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))

    w.OnRowsDropped([2], 0)  # 把「丙」拖到最前面

    assert _ids(w) == ["10000000003", "10000008780", "10000000002"]
    assert w.table.item(0, app_module.COL_CID).text() == "10000000003"
    assert w.row_urls[0].endswith("clusterId=10000000003")
    body = [
        line
        for line in (data_files / "watchlist.txt").read_text(encoding="utf-8").split("\n")
        if line and not line.startswith("#")
    ]
    assert body == ["10000000003 | 丙", "10000008780 | 甲", "10000000002 | 乙"]
    assert "已调整顺序" in w.progress_label.text()


def test_reorder_keeps_fetched_values_with_their_row(window):
    """已抓到的数据跟着商品走，不会留在原来的行号上。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    w.rows[1]["values"] = result_ok(name="乙最新")

    w.OnRowsDropped([1], 0)

    assert _ids(w) == ["10000000002", "10000008780"]
    assert w.rows[0]["values"]["name"] == "乙最新"
    assert w.table.item(0, app_module.COL_NAME).text() == "乙最新"
    assert w.table.item(0, app_module.COL_PRICE).text() == "¥44"
    assert w.table.item(1, app_module.COL_PRICE).text() == app_module.NO_DATA_TEXT


def test_reorder_moves_selection_with_the_rows(window):
    """重排后选中态跟着被挪的行走，不能留在原来的行号上。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))
    w.table.selectRow(2)  # 选中「丙」

    w.OnRowsDropped([2], 0)  # 把「丙」拖到最前面

    assert _ids(w) == ["10000000003", "10000008780", "10000000002"]
    assert _selected(w) == [0]


def test_reorder_keeps_the_whole_selection_together(window):
    """一次拖多行时，选中的几行要一起跟着走，不能只剩最后一行。"""
    w = _loaded(
        window,
        ("10000008780", "甲"),
        ("10000000002", "乙"),
        ("10000000003", "丙"),
        ("10000000004", "丁"),
    )
    # selectRow 会顶掉上一次的选择，多选得用 setRangeSelected
    w.table.setRangeSelected(QTableWidgetSelectionRange(1, 0, 2, 8), True)

    w.OnRowsDropped([1, 2], 4)  # 把「乙」「丙」拖到末尾

    assert _ids(w) == ["10000008780", "10000000004", "10000000002", "10000000003"]
    assert _selected(w) == [2, 3]


def test_reorder_to_same_place_keeps_selection(window, data_files):
    """原地放下不动表格，选中态也不该被动过。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    w.table.selectRow(1)

    w.OnRowsDropped([1], 1)

    assert _selected(w) == [1]


def test_reorder_to_same_place_changes_nothing(window, data_files):
    """原地放下（或又拖回原位）不重画也不写文件。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    before = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    w.progress_label.setText("哨兵")

    w.OnRowsDropped([0], 0)

    assert (data_files / "watchlist.txt").read_text(encoding="utf-8") == before
    assert w.progress_label.text() == "哨兵"


def test_reorder_ignored_while_polling(window, data_files):
    """抓取中忽略重排：轮询任务记的是行号，中途插队会把结果填错行。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    before = (data_files / "watchlist.txt").read_text(encoding="utf-8")
    w.poller = FakePoller()

    w.OnRowsDropped([1], 0)

    assert _ids(w) == ["10000008780", "10000000002"]
    assert (data_files / "watchlist.txt").read_text(encoding="utf-8") == before


ROW1_RECT = QRect(0, 100, 300, 100)  # 假装第 1 行占了 y=100..199 这么一块


def test_drop_target_splits_the_row_at_its_middle(window, monkeypatch):
    """落点按行的中线分上/下半格——和画出来的那条横线是同一套算法。

    几何直接给死，免得依赖 offscreen 平台下真实的行高与滚动位置。
    """
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))
    monkeypatch.setattr(w.table, "indexAt", lambda pos: w.table.model().index(1, 0))
    monkeypatch.setattr(w.table, "visualRect", lambda index: ROW1_RECT)
    middle = ROW1_RECT.center().y()

    assert w.table._drop_target(QPoint(10, ROW1_RECT.top())) == 1
    assert w.table._drop_target(QPoint(10, middle)) == 1  # 正中间算上半格
    assert w.table._drop_target(QPoint(10, middle + 1)) == 2
    assert w.table._drop_target(QPoint(10, ROW1_RECT.bottom())) == 2


def test_drop_target_below_last_row_appends(window, monkeypatch):
    """拖到表格下方的空白区 = 挪到最后。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    monkeypatch.setattr(w.table, "indexAt", lambda pos: QModelIndex())

    assert w.table._drop_target(QPoint(0, 0)) == 2


def test_drop_hint_tracks_the_drag(window, monkeypatch):
    """拖动时记下落点（好画出那条横线），离开表格或松手就清掉。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))
    w.table.selectRow(0)
    monkeypatch.setattr(w.table, "indexAt", lambda pos: w.table.model().index(1, 0))
    monkeypatch.setattr(w.table, "visualRect", lambda index: ROW1_RECT)
    mime = w.table.model().mimeData([w.table.model().index(0, 0)])
    move = QDragMoveEvent(
        QPoint(10, ROW1_RECT.bottom()), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier
    )

    w.table.dragMoveEvent(move)
    assert move.isAccepted()
    assert w.table._drop_at == 2

    w.table.dragLeaveEvent(QDragLeaveEvent())
    assert w.table._drop_at is None


def test_drop_hint_is_painted(window, qapp):
    """那条横线是真画出来了：有落点和没落点的截图得不一样。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    w.show()  # 没显示过就没有可视区，画不出东西
    qapp.processEvents()
    plain = w.table.viewport().grab().toImage()

    w.table._set_drop_hint(1)
    hinted = w.table.viewport().grab().toImage()

    assert hinted != plain


def test_drop_event_reports_target_and_leaves_cells_alone(window, monkeypatch):
    """dropEvent 只算落点、发信号，自己一行都不搬。

    "Qt 会不会顺手把源行删掉"只能在真实拖拽里才会发生（本类靠 startDrag 里
    自己起 QDrag 绕开它），这里退一步，守住"表格行数不变"这条底线。
    """
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))
    w.table.selectRow(0)
    monkeypatch.setattr(w.table, "indexAt", lambda pos: w.table.model().index(1, 0))
    monkeypatch.setattr(w.table, "visualRect", lambda index: ROW1_RECT)  # 落在下半格
    mime = QMimeData()  # 得留个引用：QDropEvent 只存指针，不接管所有权
    event = QDropEvent(
        QPoint(10, ROW1_RECT.bottom()), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier
    )

    w.table.dropEvent(event)

    assert event.isAccepted()
    assert w.table._drop_at is None  # 松手后提示要收掉
    assert w.table.rowCount() == 3
    assert _ids(w) == ["10000000002", "10000008780", "10000000003"]


def test_drop_event_requires_a_selection(window, monkeypatch):
    """没选中任何行就没什么可挪的，事件退回给 Qt。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"))
    w.table.clearSelection()
    mime = QMimeData()
    event = QDropEvent(QPoint(10, 10), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

    w.table.dropEvent(event)

    assert not event.isAccepted()
    assert _ids(w) == ["10000008780", "10000000002"]


def test_start_drag_carries_model_mime_and_keeps_rows(window, qapp, monkeypatch):
    """自己起的那个拖拽带着模型认得的 mime，且一行都不动。

    真拖一遍要点住鼠标、走进 Qt 的拖拽循环，测试里跑不了；这里把 exec_ 换掉，
    只验"起出来的是个什么样的拖拽"，不跑那圈循环。
    """
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))
    w.show()  # 取拖拽小图要用到真实几何
    qapp.processEvents()
    w.table.selectRow(1)
    made = []

    def fake_exec(drag, *args, **kwargs):
        made.append((drag.mimeData(), drag.pixmap()))
        return Qt.IgnoreAction

    monkeypatch.setattr(QDrag, "exec_", fake_exec)
    w.table.startDrag(Qt.MoveAction)

    assert len(made) == 1
    mime, pixmap = made[0]
    assert mime is not None
    assert mime.hasFormat(w.table.model().mimeTypes()[0])  # 有它 Qt 那边才 canDrop
    assert not pixmap.isNull()
    assert w.table.rowCount() == 3
    assert _ids(w) == ["10000008780", "10000000002", "10000000003"]


def test_start_drag_without_selection_does_nothing(window, monkeypatch):
    """没选中行时不起拖拽。"""
    w = _loaded(window, ("10000008780", "甲"))
    w.table.clearSelection()
    made = []
    monkeypatch.setattr(QDrag, "exec_", lambda drag, *a, **k: made.append(drag))

    w.table.startDrag(Qt.MoveAction)

    assert made == []


def test_reorder_uses_whole_selection(window, monkeypatch):
    """多选时整块一起挪，块内保持原顺序。"""
    w = _loaded(window, ("10000008780", "甲"), ("10000000002", "乙"), ("10000000003", "丙"))
    # selectRow 会顶掉上一次的选择，多选得用 setRangeSelected
    w.table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 8), True)
    monkeypatch.setattr(w.table, "indexAt", lambda pos: QModelIndex())  # 落在空白区
    mime = QMimeData()
    event = QDropEvent(QPoint(10, 10), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)

    w.table.dropEvent(event)

    assert _ids(w) == ["10000000003", "10000008780", "10000000002"]


def test_thumbnails_are_reused_across_redraws(window, monkeypatch):
    """下过的缩略图存起来复用：重画表格（拖拽排序、增删）不再重下一遍。"""
    w = window("10000008780 | 甲\n10000000002 | 乙\n")
    w.store.upsert_item("10000008780", "甲", "https://example.test/a.png")
    w.store.upsert_item("10000000002", "乙", "https://example.test/b.png")
    fetched = []
    monkeypatch.setattr(w.image_fetcher, "fetch", lambda row, url: fetched.append(url))

    w.LoadWatchlist(w.watchlist_path)
    assert len(fetched) == 2  # 缓存里没有，两张都得下

    for row in (0, 1):  # 假装两张图都下载回来了
        pixmap = QPixmap(8, 8)
        pixmap.fill(Qt.red)
        w.OnImageFetched(row, pixmap)

    fetched.clear()
    w.RenderTable()
    assert fetched == []
    assert not w.table.item(0, app_module.COL_IMG).icon().isNull()


# ---------------- 主题 ----------------


def test_toggle_theme_persists_choice(window, qapp):
    """切主题：立即生效、写进设置，下次启动沿用（不再跟随系统）。"""
    w = window()
    start = w.dark

    w.OnToggleTheme()
    assert w.dark is not start
    assert w.store.get_setting("theme") == ("dark" if w.dark else "light")
    assert w.btn_theme.text() == ("浅色模式" if w.dark else "暗色模式")
    assert qapp.styleSheet() == (theme.DARK_QSS if w.dark else theme.LIGHT_QSS)

    w2 = window()  # 同一个临时库：新窗口应当沿用刚才的选择
    assert w2.dark is w.dark


def test_apply_theme_updates_button_label(window):
    """直接应用主题时，按钮文案与 tooltip 也要跟着变。"""
    w = window()
    w.ApplyTheme(True)
    assert w.btn_theme.text() == "浅色模式"
    assert "暗色主题" in w.btn_theme.toolTip()

    w.ApplyTheme(False)
    assert w.btn_theme.text() == "暗色模式"
    assert "浅色主题" in w.btn_theme.toolTip()


def test_apply_theme_installs_tooltip_delay(window, qapp, monkeypatch):
    """应用主题时顺带拉长悬停提示的延时（样式表管不到，得走样式提示）。"""
    installed = []
    monkeypatch.setattr(theme, "install_tooltip_delay", installed.append)

    w = window()
    assert installed[-1] is qapp  # 建窗口时就装上了

    w.OnToggleTheme()
    assert installed[-1] is qapp  # 切主题时再装一次也无妨（装过就跳过）


# ---------------- 缩略图 ----------------


def test_image_ready_sets_icon(window):
    """图片线程回传后给单元格装上图标，越界行号直接忽略。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    pixmap = QPixmap(8, 8)
    pixmap.fill(Qt.red)

    w.OnImageFetched(0, pixmap)
    assert not w.table.item(0, app_module.COL_IMG).icon().isNull()

    w.OnImageFetched(99, pixmap)  # 不抛异常即通过


def test_image_fetcher_emits_downloaded_pixmap(qapp, png_bytes, wait_until, monkeypatch):
    """ImageFetcher 下载成功时通过信号回传 QPixmap。"""

    class _Resp:
        content = png_bytes

    monkeypatch.setattr(requests, "get", lambda url, timeout=None: _Resp())
    fetcher = app_module.ImageFetcher()
    got = []
    fetcher.fetched.connect(lambda row, pixmap: got.append((row, pixmap)))

    fetcher.fetch(3, "https://example.test/x.png@96w_96h_85q.webp")

    assert wait_until(lambda: got, timeout=5)
    row, pixmap = got[0]
    assert row == 3
    assert not pixmap.isNull()


def test_image_fetcher_survives_download_failure(qapp, wait_until):
    """下载失败（conftest 已把 requests.get 换成抛 ConnectionError）时静默放弃。"""
    fetcher = app_module.ImageFetcher()
    got = []
    fetcher.fetched.connect(lambda row, pixmap: got.append(row))

    fetcher.fetch(0, "https://example.test/broken.png")

    time.sleep(0.2)  # 给线程一点时间跑完那条注定失败的下载
    qapp.processEvents()
    assert got == []


# ---------------- 轮询线程 ----------------


def test_poller_emits_results_in_order_with_progress(qapp, monkeypatch, join_thread):
    """依次（非并发）轮询：每条都回传结果，进度按 1/2、2/2 递增。"""
    log = []
    monkeypatch.setattr(
        client,
        "fetch_cluster",
        _fetch_by_id({"1": raw_ok(name="甲"), "2": raw_ok(name="乙")}, log),
    )
    thread = app_module.PollerThread(_tasks(["1", "2"]), 0.01, 0.01, 1.0)
    results, progress, done = [], [], []
    thread.result_ready.connect(lambda row, res: results.append((row, res["name"])))
    thread.progress_changed.connect(lambda cur, total: progress.append((cur, total)))
    thread.finished_all.connect(lambda: done.append(True))

    thread.start()
    assert join_thread(thread)

    assert results == [(0, "甲"), (1, "乙")]
    assert progress == [(1, 2), (2, 2)]
    assert done == [True]
    assert log == ["1", "2"]


def test_poller_retries_once_after_failure(qapp, monkeypatch, join_thread):
    """单条失败后重试 1 次，第二次成功就照常出结果。"""
    attempts = []

    def _flaky(cluster_id, timeout=None):
        attempts.append(str(cluster_id))
        if len(attempts) == 1:
            raise client.ApiError("第一次失败")
        return raw_ok(name="第二次成功")

    monkeypatch.setattr(client, "fetch_cluster", _flaky)
    thread = app_module.PollerThread(_tasks(["1"]), 0.01, 0.01, 1.0)
    results = []
    thread.result_ready.connect(lambda row, res: results.append(res))

    thread.start()
    assert join_thread(thread)

    assert attempts == ["1", "1"]
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["name"] == "第二次成功"


def test_poller_reports_failure_after_two_attempts(qapp, monkeypatch, join_thread):
    """两次都失败才认输，结果里带着错误原因（界面据此显示「查询失败」）。"""
    attempts = []

    def _always_fail(cluster_id, timeout=None):
        attempts.append(str(cluster_id))
        raise client.ApiError("HTTP 503")

    monkeypatch.setattr(client, "fetch_cluster", _always_fail)
    thread = app_module.PollerThread(_tasks(["1"]), 0.01, 0.01, 1.0)
    results = []
    thread.result_ready.connect(lambda row, res: results.append(res))

    thread.start()
    assert join_thread(thread)

    assert len(attempts) == 2
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert results[0]["error"] == "HTTP 503"


def test_poller_stop_interrupts_long_wait(qapp, monkeypatch, wait_until, join_thread):
    """停止要能立刻打断间隔等待，而不是干等完 poll_interval。"""
    log = []
    monkeypatch.setattr(
        client,
        "fetch_cluster",
        _fetch_by_id({"1": raw_ok(), "2": raw_ok(), "3": raw_ok()}, log),
    )
    thread = app_module.PollerThread(_tasks(["1", "2", "3"]), 5.0, 0.01, 1.0)
    results = []
    thread.result_ready.connect(lambda row, res: results.append(row))

    started = time.monotonic()
    thread.start()
    assert wait_until(lambda: results, timeout=5)

    thread.stop()
    assert join_thread(thread, timeout=3000)
    assert time.monotonic() - started < 3  # 没傻等那 5 秒
    assert log == ["1"]  # 后续商品不再发请求


def test_poller_stop_releases_pause(qapp, monkeypatch, wait_until, join_thread):
    """暂停中调用 stop 也能退出（否则线程会卡在暂停循环里）。"""
    monkeypatch.setattr(
        client, "fetch_cluster", _fetch_by_id({"1": raw_ok(), "2": raw_ok()})
    )
    thread = app_module.PollerThread(_tasks(["1", "2"]), 0.01, 0.01, 1.0)
    results = []
    thread.result_ready.connect(lambda row, res: results.append(row))

    thread.start()
    assert wait_until(lambda: results, timeout=5)

    thread.pause()
    time.sleep(0.1)
    thread.stop()
    assert join_thread(thread, timeout=3000)
    assert thread.is_paused() is False


def test_poller_pause_holds_until_resume(qapp, monkeypatch, wait_until, join_thread):
    """暂停期间不发下一条请求，继续后才接着抓。"""
    log = []
    monkeypatch.setattr(
        client,
        "fetch_cluster",
        _fetch_by_id({"1": raw_ok(), "2": raw_ok()}, log),
    )
    thread = app_module.PollerThread(_tasks(["1", "2"]), 1.0, 0.01, 1.0)
    results = []
    thread.result_ready.connect(lambda row, res: results.append(row))

    thread.start()
    assert wait_until(lambda: results, timeout=5)

    thread.pause()
    time.sleep(0.3)
    qapp.processEvents()
    assert log == ["1"]  # 暂停期间没有新请求

    thread.resume()
    assert wait_until(lambda: len(results) == 2, timeout=5)
    assert join_thread(thread)
    assert log == ["1", "2"]


def test_poller_with_no_tasks_just_finishes(qapp, join_thread):
    """空清单启动轮询：不发请求但照样报结束，界面不会卡在「抓取中」。"""
    thread = app_module.PollerThread([], 0.01, 0.01, 1.0)
    done = []
    thread.finished_all.connect(lambda: done.append(True))

    thread.start()
    assert join_thread(thread)
    assert done == [True]


def test_poller_stopped_before_start_does_nothing(qapp, monkeypatch, join_thread):
    """启动前就被停止的轮询不该发任何请求。"""
    log = []
    monkeypatch.setattr(client, "fetch_cluster", _fetch_by_id({"1": raw_ok()}, log))
    thread = app_module.PollerThread(_tasks(["1"]), 0.01, 0.01, 1.0)
    results = []
    thread.result_ready.connect(lambda row, res: results.append(row))

    thread.stop()
    thread.start()
    assert join_thread(thread)
    assert results == []
    assert log == []


# ---------------- 从界面发起抓取（端到端） ----------------


def test_start_fetch_runs_poller_and_updates_ui(window, monkeypatch, wait_until, data_files):
    """点「开始抓取」：轮询跑完、表格出价、新名字写回清单、按钮复位。"""
    w = window("10000008780 | 甲\n10000000002 | 乙\n")
    w.LoadWatchlist(w.watchlist_path)
    log = []
    monkeypatch.setattr(
        client,
        "fetch_cluster",
        _fetch_by_id(
            {
                "10000008780": raw_ok(name="甲的新名", price="¥10"),
                "10000000002": raw_ok(name="乙的新名", price="¥20"),
            },
            log,
        ),
    )
    monkeypatch.setattr(w.image_fetcher, "fetch", lambda row, url: None)

    w.OnFetchPrices()
    assert w.IsPolling()
    assert not w.btn_fetch.isEnabled()

    assert wait_until(lambda: w.btn_fetch.isEnabled(), timeout=10)
    assert log == ["10000008780", "10000000002"]
    assert w.table.item(0, app_module.COL_PRICE).text() == "¥10"
    assert w.table.item(1, app_module.COL_NAME).text() == "乙的新名"
    assert w.names_learned is True
    assert "写回" in w.progress_label.text()
    assert "10000008780 | 甲的新名" in (data_files / "watchlist.txt").read_text(
        encoding="utf-8"
    )


def test_start_fetch_without_items_warns(window, msgboxes):
    """清单为空时点抓取只提示，不起线程。"""
    w = window()
    w.OnFetchPrices()
    assert msgboxes[-1]["kind"] == "information"
    assert not w.IsPolling()


def test_start_fetch_ignored_while_polling(window):
    """轮询进行中重复点抓取不应再起一个线程。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)
    existing = FakePoller()
    w.poller = existing

    w.OnFetchPrices()
    assert w.poller is existing


# ---------------- 小工具 ----------------


@pytest.mark.parametrize(
    "duplicated, invalid, expected",
    [
        (0, 0, ""),
        (2, 0, "（已忽略：2 条已存在）"),
        (0, 3, "（已忽略：3 行无法识别）"),
        (1, 2, "（已忽略：1 条已存在、2 行无法识别）"),
    ],
)
def test_skipped_note(duplicated, invalid, expected):
    """「添加」结果里的忽略说明按两种计数拼接。"""
    assert app_module._skipped_note(duplicated, invalid) == expected


@pytest.mark.parametrize(
    "deal, expected",
    [
        ({"price": "¥205", "time": "8天前"}, "¥205 · 8天前"),
        ({"price": "¥205", "time": ""}, "¥205"),  # 没有时间就不留个空尾巴
        ({"price": "", "time": "8天前"}, "8天前"),
        ({"price": "", "time": ""}, ""),
    ],
)
def test_deal_text(deal, expected):
    """成交格的文本：有价格有时间才拼「·」，缺一半时不留分隔符。"""
    assert app_module._deal_text(deal) == expected


# ---------------- 子模块降级提示的接线 ----------------


def test_note_helper_prints_and_formats(window, capsys):
    """Note() 既是留档（打印）也是状态栏文案（拼接）。"""
    w = window()

    assert w.Note(["一条提示"], None, []) == "；一条提示"
    assert w.Note() == ""
    assert w.Note(None, []) == ""
    assert "[降级] 一条提示" in capsys.readouterr().out


def test_startup_reports_corrupt_cache(window, data_files, capsys):
    """缓存库损坏时：程序照样起来，坏文件改名留档，状态栏和控制台都有提示。"""
    (data_files / "cache.db").write_text("这不是 SQLite 文件", encoding="utf-8")

    w = window("10000008780 | 甲\n")
    w.LoadWatchlist(w.watchlist_path)
    w.ReportStartupNotes()

    assert w.store_warnings and "损坏" in w.store_warnings[0]
    assert "损坏" in w.progress_label.text()
    assert "cache.db.bad-" in w.progress_label.text()
    assert len(list(data_files.glob("cache.db.bad-*"))) == 1
    assert "[cache]" in capsys.readouterr().out
    # 缓存坏了也不影响使用：清单照常载入，写回清单也能成功
    assert w.table.rowCount() == 1
    assert w.SaveWatchlist() is True


def test_load_watchlist_surfaces_notes(window, data_files):
    """清单里有认不出来的行时，状态栏要说出来（而不是默默少几件商品）。"""
    w = window("# 注释\n1001 | 甲\n这行是什么鬼\n")

    assert w.LoadWatchlist(w.watchlist_path) is True

    assert "1 行认不出" in w.progress_label.text()
    assert w.table.rowCount() == 1


def test_refresh_list_keeps_the_note(window, data_files):
    """「刷新商品列表」会覆盖状态栏文案，降级提示必须跟着一起显示。"""
    w = window("1001 | 甲\n乱码行\n")
    w.LoadWatchlist(w.watchlist_path)
    w.OnRefreshList()

    assert "清单已同步" in w.progress_label.text()
    assert "认不出" in w.progress_label.text()


def test_save_watchlist_reports_skipped_rows(window):
    """行模型里混进非法 ID 时：能写的照写，跳过的条数报给用户。"""
    w = window("1001 | 甲\n1002 | 乙\n")
    w.LoadWatchlist(w.watchlist_path)
    w.rows[1]["entry"].cluster_id = "不是数字"

    assert w.SaveWatchlist() is True
    w.OnNormalize()

    assert "跳过" in w.last_note
    assert "跳过" in w.progress_label.text()
    body = [
        line
        for line in open(w.watchlist_path, encoding="utf-8").read().split("\n")
        if line and not line.startswith("#")
    ]
    assert body == ["1001 | 甲"]  # 非法条目没被写进清单


def test_dangerous_name_is_cleaned_on_load_and_save(window):
    """清单里手写的名字带 "|" 时不进表格也不写回文件，两头都不会被撑坏。"""
    w = window("1001 | 甲|乙\n")
    w.LoadWatchlist(w.watchlist_path)

    assert w.table.item(0, app_module.COL_NAME).text() == "甲 乙"

    w.OnNormalize()
    body = [
        line
        for line in open(w.watchlist_path, encoding="utf-8").read().split("\n")
        if line and not line.startswith("#")
    ]
    assert body == ["1001 | 甲 乙"]


def test_deal_cell_without_time_has_no_separator(window):
    """接口没给成交时间时，单元格只显示价格，不是「¥205 · 」。"""
    w = window("10000008780\n")
    w.LoadWatchlist(w.watchlist_path)

    result = result_ok(deals=1)
    result["deals"][0]["time"] = ""
    w.OnResultReady(0, result)

    assert w.table.item(0, app_module.COL_DEAL_BASE).text() == "¥205"
    assert w.table.item(0, app_module.COL_DEAL_BASE + 1).text() == app_module.NO_DATA_TEXT


def test_close_event_closes_store(window):
    """关窗口时把缓存连接关掉（SQLite 写入落盘、别占着文件句柄）。"""
    w = window("1001 | 甲\n")
    w.LoadWatchlist(w.watchlist_path)

    w.close()

    assert w.store.conn is None


# ---------------- 近期成交高亮 ----------------
#
# 阈值（默认 24 小时）内的成交在「成交①/②/③」里加粗 + 上配置色。
# 判定按每条成交自己的时间走，不依赖接口的排序。


def _deal_item(w, index=0):
    return w.table.item(0, app_module.COL_DEAL_BASE + index)


def _highlight_window(window, config_toml=None):
    """建一个单行窗口并载入清单（高亮用例都从这一步开始）。

    名字避开上面拖拽用例的 _loaded()——那个的第二个参数是清单条目，签名不一样。
    """
    w = window("10000008780\n", config_toml=config_toml)
    w.LoadWatchlist(w.watchlist_path)
    return w


def test_fresh_deal_is_highlighted(window):
    """24 小时内的成交加粗 + 用默认高亮色。"""
    w = _highlight_window(window)
    w.OnResultReady(0, result_ok(deal_times=["2小时前"]))

    item = _deal_item(w)
    assert item.text() == "¥205 · 2小时前"
    assert item.font().bold()
    assert item.foreground().color().name() == "#e07000"


def test_stale_deal_is_left_alone(window):
    """超过阈值的成交保持原样：不加粗，也不设前景色（留着走主题默认色）。"""
    w = _highlight_window(window)
    w.OnResultReady(0, result_ok(deal_times=["8天前"]))

    item = _deal_item(w)
    assert item.text() == "¥205 · 8天前"
    assert not item.font().bold()
    assert item.foreground().style() == Qt.NoBrush


def test_only_fresh_deals_are_highlighted(window):
    """三条成交各按自己的时间判定，只亮该亮的那几条。"""
    w = _highlight_window(window)
    w.OnResultReady(0, result_ok(deal_times=["2小时前", "8天前", "9小时前"]))

    assert _deal_item(w, 0).font().bold()
    assert not _deal_item(w, 1).font().bold()
    assert _deal_item(w, 2).font().bold()


@pytest.mark.parametrize(
    "time_text, expected",
    [
        ("刚刚", True),
        ("23小时前", True),
        ("24小时前", True),   # 阈值是「不超过」，边界算在内
        ("25小时前", False),
        ("2天前", False),
    ],
)
def test_highlight_threshold_boundary(window, time_text, expected):
    """阈值是「多少小时以内」，边界值算在内。"""
    w = _highlight_window(window)
    w.OnResultReady(0, result_ok(deal_times=[time_text]))
    assert _deal_item(w).font().bold() is expected


def test_threshold_comes_from_config(window):
    """阈值改成 1 小时后，2 小时前的成交就不再高亮。"""
    w = _highlight_window(window, config_toml="deal_highlight_within_hours = 1\n")
    w.OnResultReady(0, result_ok(deal_times=["2小时前"]))

    assert w.deal_highlight_seconds == 3600
    assert not _deal_item(w).font().bold()


def test_zero_threshold_disables_highlight(window):
    """阈值为 0 表示关闭高亮，再新鲜的成交也不上色。"""
    w = _highlight_window(window, config_toml="deal_highlight_within_hours = 0\n")
    w.OnResultReady(0, result_ok(deal_times=["刚刚"]))

    assert not _deal_item(w).font().bold()
    assert _deal_item(w).foreground().style() == Qt.NoBrush


def test_highlight_color_comes_from_config(window):
    """高亮色换成配置里的那个。"""
    w = _highlight_window(window, config_toml='deal_highlight_color = "#00ff00"\n')
    w.OnResultReady(0, result_ok(deal_times=["2小时前"]))

    assert _deal_item(w).foreground().color().name() == "#00ff00"


def test_unparsable_color_falls_back_instead_of_silently_losing_highlight(window):
    """配置写了个不存在的颜色名：退回默认色，而不是「功能看起来没生效」。"""
    w = _highlight_window(window, config_toml='deal_highlight_color = "orangejuice"\n')
    w.OnResultReady(0, result_ok(deal_times=["2小时前"]))

    assert _deal_item(w).foreground().color().name() == theme.DEAL_HIGHLIGHT_COLOR


def test_unknown_deal_time_is_not_highlighted(window):
    """时间认不出来时不上色——宁可少高亮一处，也不拿猜出来的新旧误导人。"""
    w = _highlight_window(window)
    w.OnResultReady(0, result_ok(deal_times=["很久以前"]))

    item = _deal_item(w)
    assert item.text() == "¥205 · 很久以前"  # 原文照旧显示，只是不上色
    assert not item.font().bold()


def test_highlight_survives_table_rebuild(window):
    """重画表格（拖拽排序、刷新清单、增删）后高亮还在。

    高亮走 FillRow -> SetDealCells，和抓取回填是同一条渲染路径，
    不能只在上一次回填时亮一下。
    """
    w = _highlight_window(window)
    w.OnResultReady(0, result_ok(deal_times=["2小时前"]))
    assert _deal_item(w).font().bold()

    w.RenderTable()

    assert _deal_item(w).font().bold()
    assert _deal_item(w).foreground().color().name() == "#e07000"
