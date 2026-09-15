"""市集商品价格监视器 · PyQt5 主窗口 + 轮询调度。

启动流程：
1. 整体扫描 data/watchlist.txt 与本地缓存库（SQLite）中的商品 clusterID，
   先把所有能显示的数据（名称、缩略图、clusterID）摆上表格；
2. 再对 watchlist 中的商品一行行抓取价格信息并刷新。

与旧版 src/App.py 的关键区别：轮询放在 QThread 子线程里做，
通过信号槽把每条结果回传主线程刷新表格，避免界面假死。
"""

import os
import sys
import threading
import time
import webbrowser

import requests
from PyQt5.QtCore import QObject, QSize, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import client
import links
import parser
import store

POLL_INTERVAL_SEC = 2.0        # 依次轮询的间隔，风控经验：>= 2 秒
RETRY_ONCE_INTERVAL_SEC = 1.0  # 单条失败重试前的等待
IMAGE_SIZE = 96                # 缩略图边长（配合 CDN 裁剪后缀减小流量）
IMAGE_SUFFIX = f"@{IMAGE_SIZE}w_{IMAGE_SIZE}h_85q.webp"

COL_IMG, COL_NAME, COL_CID, COL_PRICE, COL_AVG = 0, 1, 2, 3, 4
COL_DEAL_BASE = 5  # 成交① 占 5/6/7 三列
COL_LINK = 8
HEADERS = ["缩略图", "商品名", "clusterID", "现价", "近30天均价",
           "成交①", "成交②", "成交③", "链接（点击打开）"]

PENDING_TEXT = "…"    # 等待抓取
NO_DATA_TEXT = "—"    # 无数据


class PollerThread(QThread):
    """依次（非并发）查询每个商品，间隔 >= 2 秒，单条失败重试 1 次后放弃。

    tasks 为 [(行号, LinkEntry)]，只轮询 watchlist 里的商品。
    """

    result_ready = pyqtSignal(int, dict)    # 行号, 解析结果
    progress_changed = pyqtSignal(int, int)  # 当前第 x 条, 共 y 条
    finished_all = pyqtSignal()

    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.tasks)
        for i, (row, entry) in enumerate(self.tasks):
            if self._stop:
                break
            self.progress_changed.emit(i + 1, total)

            result = None
            for attempt in (1, 2):  # 失败重试（单条 1 次）
                if self._stop:
                    break
                try:
                    resp = client.fetch_cluster(entry.cluster_id)
                    result = parser.parse_cluster(resp)
                    break
                except client.ApiError as err:
                    result = parser.parse_error(err)
                    if attempt == 1:
                        self._sleep(RETRY_ONCE_INTERVAL_SEC)

            if result is not None:
                self.result_ready.emit(row, result)
            if i < total - 1:
                self._sleep(POLL_INTERVAL_SEC)
        self.finished_all.emit()

    def _sleep(self, seconds):
        """分片 sleep，保证 stop() 能及时生效。"""
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(min(0.1, end - time.time()))


class ImageFetcher(QObject):
    """在普通子线程里拉取缩略图，通过信号回传 QPixmap（避免卡界面）。"""

    fetched = pyqtSignal(int, QPixmap)

    def fetch(self, row, url):
        threading.Thread(target=self._work, args=(row, url), daemon=True).start()

    def _work(self, row, url):
        try:
            req = requests.get(url, timeout=10)
            pixmap = QPixmap()
            pixmap.loadFromData(req.content)
        except Exception:
            return
        if not pixmap.isNull():
            self.fetched.emit(row, pixmap)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = store.Store(store.default_db_path())
        self.watch_entries = []  # List[links.LinkEntry]，来自 watchlist.txt
        self.rows = []           # 表格行模型：[{"entry": LinkEntry|None, "record": dict|None}]
        self.row_urls = {}       # 行号 -> 详情页链接
        self.poller = None
        self.image_fetcher = ImageFetcher()
        self.image_fetcher.fetched.connect(self.OnImageFetched)
        self.InitUI()

    def InitUI(self):
        self.setWindowTitle("市集商品价格监视器")
        self.resize(1100, 600)

        central = QWidget()
        layout = QVBoxLayout(central)

        # 顶部按钮行
        btn_bar = QHBoxLayout()
        self.btn_import = QPushButton("导入链接…")
        self.btn_refresh = QPushButton("刷新")
        self.btn_import.clicked.connect(self.OnImport)
        self.btn_refresh.clicked.connect(self.OnRefresh)
        self.progress_label = QLabel("就绪")
        btn_bar.addWidget(self.btn_import)
        btn_bar.addWidget(self.btn_refresh)
        btn_bar.addStretch()
        btn_bar.addWidget(self.progress_label)
        layout.addLayout(btn_bar)

        # 表格
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setIconSize(QSize(IMAGE_SIZE, IMAGE_SIZE))
        header.resizeSection(COL_IMG, IMAGE_SIZE + 8)
        for col in (COL_PRICE, COL_AVG):
            header.resizeSection(col, 100)
        for col in (COL_DEAL_BASE, COL_DEAL_BASE + 1, COL_DEAL_BASE + 2):
            header.resizeSection(col, 120)
        header.resizeSection(COL_CID, 100)
        header.resizeSection(COL_LINK, 60)
        # 商品名占剩余全部横向空间
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.table.cellClicked.connect(self.OnCellClick)
        layout.addWidget(self.table)

        self.setCentralWidget(central)

    def LoadWatchlist(self, path):
        """读取 watchlist，与缓存库合并成表格行，先显示缓存里已有的数据。"""
        try:
            entries = links.load_links(path)
        except OSError as err:
            QMessageBox.warning(self, "导入失败", f"读取文件出错：\n{err}")
            return
        self.watch_entries = entries
        self.RebuildRows()

    def RebuildRows(self):
        """watchlist 与缓存库求并集：watchlist 条目在前，仅存在于缓存中的条目在后。"""
        rows = []
        seen = set()
        for entry in self.watch_entries:
            seen.add(entry.cluster_id)
            rows.append({"entry": entry, "record": self.store.get_item(entry.cluster_id)})
        for record in self.store.get_all():
            if record["cluster_id"] not in seen:
                rows.append({"entry": None, "record": record})
        self.rows = rows
        self.BuildTable()

    def BuildTable(self):
        self.table.setRowCount(len(self.rows))
        self.row_urls.clear()
        for row, item in enumerate(self.rows):
            self.table.setRowHeight(row, IMAGE_SIZE + 8)
            entry, record = item["entry"], item["record"]
            cluster_id = entry.cluster_id if entry else record["cluster_id"]

            # 商品名：有缓存用缓存；清单里的新商品先占位等抓取
            if record and record["name"]:
                name_text = record["name"]
            elif entry:
                name_text = PENDING_TEXT
            else:
                name_text = "（无缓存记录）"
            name_item = QTableWidgetItem(name_text)
            if entry is None:
                name_item.setToolTip("不在 watchlist 中，仅显示缓存信息")
            self.table.setItem(row, COL_NAME, name_item)

            self.table.setItem(row, COL_CID, QTableWidgetItem(cluster_id))

            # 价格信息：清单内商品等待抓取，仅缓存条目没有价格
            pending = PENDING_TEXT if entry else NO_DATA_TEXT
            self.table.setItem(row, COL_PRICE, QTableWidgetItem(pending))
            self.table.setItem(row, COL_AVG, QTableWidgetItem(pending))
            for i in range(3):
                self.table.setItem(row, COL_DEAL_BASE + i, QTableWidgetItem(pending))

            img_item = QTableWidgetItem()
            img_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_IMG, img_item)

            # 缩略图：有缓存直接加载，不用等抓取
            if record and record["image_url"]:
                self.image_fetcher.fetch(row, record["image_url"] + IMAGE_SUFFIX)

            url = links.get_detail_url(entry) if entry else links.build_detail_url(cluster_id)
            self.row_urls[row] = url
            link_item = QTableWidgetItem("打开")
            link_item.setData(Qt.UserRole, url)
            link_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_LINK, link_item)

        cached = sum(1 for item in self.rows if item["record"])
        self.progress_label.setText(
            f"共 {len(self.rows)} 件商品（{cached} 件有本地缓存），待抓取 "
            f"{sum(1 for item in self.rows if item['entry'])} 件"
        )

    def OnImport(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择链接文件", links.default_watchlist_path(),
            "Text (*.txt);;All Files (*)",
        )
        if path:
            self.LoadWatchlist(path)

    def OnRefresh(self):
        if self.poller is not None and self.poller.isRunning():
            return  # 轮询中不允许重复启动
        tasks = [(i, item["entry"]) for i, item in enumerate(self.rows) if item["entry"]]
        if not tasks:
            QMessageBox.information(self, "提示", "watchlist 中没有商品链接。")
            return
        self.btn_refresh.setEnabled(False)
        self.poller = PollerThread(tasks)
        self.poller.result_ready.connect(self.OnResultReady)
        self.poller.progress_changed.connect(self.OnProgress)
        self.poller.finished_all.connect(self.OnPollFinished)
        self.poller.start()

    def OnProgress(self, current, total):
        self.progress_label.setText(f"查询中 {current}/{total}")

    def OnResultReady(self, row, result):
        if row >= self.table.rowCount():
            return
        row_item = self.rows[row]

        if result["ok"]:
            # 第一次抓到的新商品写入缓存；已缓存的也顺手刷新名称/缩略图
            cluster_id = row_item["entry"].cluster_id
            self.store.upsert_item(cluster_id, result["name"], result["image_url"])
            if row_item["record"] is None:
                row_item["record"] = {
                    "cluster_id": cluster_id,
                    "name": result["name"],
                    "image_url": result["image_url"],
                }

        # 价格信息：每次抓取都刷新
        def text(value):
            return str(value) if value else NO_DATA_TEXT

        self.table.setItem(row, COL_PRICE, QTableWidgetItem(text(result["price"])))
        self.table.setItem(row, COL_AVG, QTableWidgetItem(text(result["avg_price"])))
        deals = result["deals"]
        for i in range(3):
            if i < len(deals):
                deal_text = f"{deals[i]['price']} · {deals[i]['time']}"
            else:
                deal_text = NO_DATA_TEXT
            self.table.setItem(row, COL_DEAL_BASE + i, QTableWidgetItem(deal_text))

        # 名称/缩略图：优先复用缓存，只在当前没有时才用抓取结果补上
        name_item = self.table.item(row, COL_NAME)
        if result["ok"] and name_item.text() == PENDING_TEXT and result["name"]:
            name_item.setText(result["name"])
        if not result["ok"]:
            name_item.setToolTip(result["error"] or "未知错误")
        if result["ok"] and result["image_url"]:
            img_item = self.table.item(row, COL_IMG)
            if img_item is not None and img_item.icon().isNull():
                self.image_fetcher.fetch(row, result["image_url"] + IMAGE_SUFFIX)

    def OnImageFetched(self, row, pixmap):
        if row >= self.table.rowCount():
            return
        item = self.table.item(row, COL_IMG)
        if item is not None:
            # CDN 已按 1:1 裁剪，这里兜底缩放保证不超出单元格
            pixmap = pixmap.scaled(
                IMAGE_SIZE, IMAGE_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            item.setIcon(QIcon(pixmap))

    def OnPollFinished(self):
        self.btn_refresh.setEnabled(True)
        self.progress_label.setText("完成")

    def OnCellClick(self, row, col):
        if col == COL_LINK:
            url = self.row_urls.get(row)
            if url:
                webbrowser.open(url)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    # 程序启动时：扫描 watchlist + 缓存库，先显示缓存，
    # 再对 watchlist 中的商品轮询一遍价格
    default_path = links.default_watchlist_path()
    if os.path.exists(default_path):
        window.LoadWatchlist(default_path)
        window.OnRefresh()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
