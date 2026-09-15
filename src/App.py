"""市集商品价格监视器 · PyQt5 主窗口 + 轮询调度。

与旧版 src/App.py 的关键区别：轮询放在 QThread 子线程里做，
通过信号槽把每条结果回传主线程刷新表格，避免界面假死。
"""

import os
import sys
import threading
import time
import webbrowser

import requests
from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal
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

POLL_INTERVAL_SEC = 2.0        # 依次轮询的间隔，风控经验：>= 2 秒
RETRY_ONCE_INTERVAL_SEC = 1.0  # 单条失败重试前的等待
IMAGE_SIZE = 64                # 缩略图边长（配合 CDN 裁剪后缀减小流量）
IMAGE_SUFFIX = f"@{IMAGE_SIZE}w_{IMAGE_SIZE}h_85q.webp"

COL_IMG, COL_NAME, COL_PRICE, COL_AVG = 0, 1, 2, 3
COL_DEAL_BASE = 4  # 成交① 占 4/5/6 三列
COL_LINK = 7
HEADERS = ["缩略图", "商品名", "现价", "近30天均价", "成交①", "成交②", "成交③", "链接（点击打开）"]


class PollerThread(QThread):
    """依次（非并发）查询每个商品，间隔 >= 2 秒，单条失败重试 1 次后放弃。"""

    result_ready = pyqtSignal(int, dict)  # 行号, 解析结果
    progress_changed = pyqtSignal(int, int)  # 当前第 x 条, 共 y 条
    finished_all = pyqtSignal()

    def __init__(self, entries):
        super().__init__()
        self.entries = entries
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.entries)
        for i, entry in enumerate(self.entries):
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
                self.result_ready.emit(i, result)
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
        self.entries = []        # List[links.LinkEntry]，行号与之一一对应
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
        header.resizeSection(COL_IMG, 80)
        header.resizeSection(COL_NAME, 240)
        for col in (COL_PRICE, COL_AVG, COL_DEAL_BASE, COL_DEAL_BASE + 1, COL_DEAL_BASE + 2):
            header.resizeSection(col, 110)
        header.setSectionResizeMode(COL_LINK, QHeaderView.Stretch)
        self.table.cellClicked.connect(self.OnCellClick)
        layout.addWidget(self.table)

        self.setCentralWidget(central)

    def LoadLinks(self, path):
        try:
            entries = links.load_links(path)
        except OSError as err:
            QMessageBox.warning(self, "导入失败", f"读取文件出错：\n{err}")
            return
        self.entries = entries
        self.BuildTable()
        self.progress_label.setText(f"已导入 {len(entries)} 条链接")
        if not entries:
            QMessageBox.information(self, "导入完成", "文件中没有解析到有效的商品链接。")

    def BuildTable(self):
        self.table.setRowCount(len(self.entries))
        self.row_urls.clear()
        for row, entry in enumerate(self.entries):
            self.table.setRowHeight(row, IMAGE_SIZE + 8)
            for col in (COL_NAME, COL_PRICE, COL_AVG):
                self.table.setItem(row, col, QTableWidgetItem("…"))
            for i in range(3):
                self.table.setItem(row, COL_DEAL_BASE + i, QTableWidgetItem("…"))
            img_item = QTableWidgetItem()
            img_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_IMG, img_item)

            url = links.get_detail_url(entry)
            self.row_urls[row] = url
            link_item = QTableWidgetItem("打开")
            link_item.setData(Qt.UserRole, url)
            link_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_LINK, link_item)

    def OnImport(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择链接文件", links.default_watchlist_path(),
            "Text (*.txt);;All Files (*)",
        )
        if path:
            self.LoadLinks(path)

    def OnRefresh(self):
        if self.poller is not None and self.poller.isRunning():
            return  # 轮询中不允许重复启动
        if not self.entries:
            QMessageBox.information(self, "提示", "请先导入商品链接。")
            return
        self.btn_refresh.setEnabled(False)
        self.poller = PollerThread(self.entries)
        self.poller.result_ready.connect(self.OnResultReady)
        self.poller.progress_changed.connect(self.OnProgress)
        self.poller.finished_all.connect(self.OnPollFinished)
        self.poller.start()

    def OnProgress(self, current, total):
        self.progress_label.setText(f"查询中 {current}/{total}")

    def OnResultReady(self, row, result):
        if row >= self.table.rowCount():
            return

        def text(value, error_note=None):
            if value:
                return str(value)
            if error_note:
                return error_note
            return "—"

        name_item = QTableWidgetItem(text(result["name"], "（查询失败）"))
        self.table.setItem(row, COL_NAME, name_item)
        self.table.setItem(row, COL_PRICE, QTableWidgetItem(text(result["price"])))
        self.table.setItem(row, COL_AVG, QTableWidgetItem(text(result["avg_price"])))

        deals = result["deals"]
        for i in range(3):
            if i < len(deals):
                deal_text = f"{deals[i]['price']} · {deals[i]['time']}"
            else:
                deal_text = "—"
            self.table.setItem(row, COL_DEAL_BASE + i, QTableWidgetItem(deal_text))

        if not result["ok"]:
            self.table.item(row, COL_NAME).setToolTip(result["error"] or "未知错误")

        # 缩略图：子线程拉取，回传后填充
        image_url = result["image_url"]
        if image_url:
            self.image_fetcher.fetch(row, image_url + IMAGE_SUFFIX)

    def OnImageFetched(self, row, pixmap):
        if row >= self.table.rowCount():
            return
        item = self.table.item(row, COL_IMG)
        if item is not None:
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

    # 程序启动时自动轮询一遍根目录的 watchlist.txt（若存在）
    default_path = links.default_watchlist_path()
    if os.path.exists(default_path):
        window.LoadLinks(default_path)
        window.OnRefresh()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
