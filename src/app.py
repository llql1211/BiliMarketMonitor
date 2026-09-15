"""市集商品价格监视器 · PyQt5 主窗口 + 轮询调度。

启动流程：
1. 读取 watchlist.txt，并让缓存库严格跟随清单（缺的补、多的删）；
2. 把能显示的数据（名称、缩略图、clusterID）摆上表格；
3. 点「抓取价格」时逐个抓取价格，新商品连名称、缩略图一起抓。

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
    QAbstractItemView,
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
import config
import links
import parser
import store
import theme

IMAGE_SIZE = 96  # 缩略图边长（配合 CDN 裁剪后缀减小流量）
IMAGE_SUFFIX = f"@{IMAGE_SIZE}w_{IMAGE_SIZE}h_85q.webp"

COL_IMG, COL_NAME, COL_CID, COL_PRICE, COL_AVG = 0, 1, 2, 3, 4
COL_DEAL_BASE = 5  # 成交① 占 5/6/7 三列
COL_LINK = 8
HEADERS = ["缩略图", "商品名", "clusterID", "现价", "近30天均价",
           "成交①", "成交②", "成交③", "链接（点击打开）"]

PENDING_TEXT = "…"      # 等待抓取
NO_DATA_TEXT = "—"      # 无数据
FAILED_TEXT = "（查询失败）"


class PollerThread(QThread):
    """依次（非并发）查询每个商品，单条失败重试 1 次后放弃。

    tasks 为 [(行号, LinkEntry)]，只轮询 watchlist 里的商品。
    """

    result_ready = pyqtSignal(int, dict)     # 行号, 解析结果
    progress_changed = pyqtSignal(int, int)  # 当前第 x 条, 共 y 条
    finished_all = pyqtSignal()

    def __init__(self, tasks, poll_interval, retry_interval, request_timeout):
        super().__init__()
        self.tasks = tasks
        self.poll_interval = poll_interval
        self.retry_interval = retry_interval
        self.request_timeout = request_timeout
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
                    resp = client.fetch_cluster(entry.cluster_id, self.request_timeout)
                    result = parser.parse_cluster(resp)
                    break
                except client.ApiError as err:
                    result = parser.parse_error(err)
                    if attempt == 1:
                        self._sleep(self.retry_interval)

            if result is not None:
                self.result_ready.emit(row, result)
            if i < total - 1:
                self._sleep(self.poll_interval)
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
        self.config, self.config_warnings = config.load_config()
        self.store = store.Store(store.default_db_path())
        self.watchlist_path = links.default_watchlist_path()
        self.watch_entries = []  # List[links.LinkEntry]，来自 watchlist.txt
        self.rows = []           # 表格行模型：见 MakeRow() 的字段说明
        self.row_urls = {}       # 行号 -> 详情页链接
        self.poller = None
        self.names_learned = False  # 本次抓取是否学到了新名称（决定要不要回写清单）
        self.image_fetcher = ImageFetcher()
        self.image_fetcher.fetched.connect(self.OnImageFetched)
        self.InitUI()
        self.ApplyTheme(self.InitialDark())

    # ---------------- 界面 ----------------

    def InitUI(self):
        template = self.config["detail_url_template"]
        self.setWindowTitle("市集商品价格监视器")
        self.resize(1200, 620)

        central = QWidget()
        layout = QVBoxLayout(central)

        # 顶部按钮行
        btn_bar = QHBoxLayout()
        self.btn_import = QPushButton("导入…")
        self.btn_delete = QPushButton("删除选中")
        self.btn_normalize = QPushButton("整理清单")
        self.btn_refresh_list = QPushButton("刷新商品列表")
        self.btn_fetch = QPushButton("抓取价格")
        for btn, slot, tip in (
            (self.btn_import, self.OnImport,
             "从 txt 文件导入链接，追加写入 watchlist.txt 末尾"),
            (self.btn_delete, self.OnDeleteSelected,
             "把选中的商品从 watchlist.txt 和缓存中删除"),
            (self.btn_normalize, self.OnNormalize,
             "按「clusterId | 商品名」格式重写 watchlist.txt"),
            (self.btn_refresh_list, self.OnRefreshList,
             "重新读取 watchlist.txt（手工改动后点这里同步）"),
            (self.btn_fetch, self.OnFetchPrices,
             f"逐个抓取价格，间隔 {self.config['poll_interval_seconds']:g} 秒"),
        ):
            btn.setToolTip(tip)
            btn_bar.addWidget(btn)
        self.btn_import.clicked.connect(self.OnImport)
        self.btn_delete.clicked.connect(self.OnDeleteSelected)
        self.btn_normalize.clicked.connect(self.OnNormalize)
        self.btn_refresh_list.clicked.connect(self.OnRefreshList)
        self.btn_fetch.clicked.connect(self.OnFetchPrices)

        btn_bar.addStretch()
        self.progress_label = QLabel("就绪")
        self.btn_theme = QPushButton()
        self.btn_theme.clicked.connect(self.OnToggleTheme)
        btn_bar.addWidget(self.progress_label)
        btn_bar.addWidget(self.btn_theme)
        layout.addLayout(btn_bar)

        # 表格
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
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
        self.setToolTip(f"详情页链接模板：{template}")

    # ---------------- 数据载入 ----------------

    def LoadWatchlist(self, path, show_errors=True):
        """读取清单 -> 同步缓存 -> 重建表格。"""
        try:
            entries = links.load_links(path)
        except OSError as err:
            if show_errors:
                QMessageBox.warning(self, "读取失败", f"读取 watchlist 出错：\n{err}")
            return False
        self.watchlist_path = path
        self.watch_entries = entries
        added, removed = self.store.sync(
            [(e.cluster_id, e.name) for e in entries]
        )
        self.RebuildRows(keep_values=False)
        self.progress_label.setText(
            f"共 {len(self.rows)} 件商品（缓存新增 {added}，删除 {removed}）"
            f"，点「抓取价格」开始查询"
        )
        return True

    def MakeRow(self, entry):
        """行模型字段：

        entry   LinkEntry，来自清单（本工具只跟踪清单里的商品）
        record  缓存的 {name, image_url}
        values  本次运行抓到的结果，表格重建时用来保留已显示的数据
        """
        return {
            "entry": entry,
            "record": self.store.get_item(entry.cluster_id),
            "values": None,
        }

    def RebuildRows(self, keep_values=True):
        """按清单重建行模型；keep_values=False 时丢弃本次抓到的数据。"""
        old = {}
        if keep_values:
            old = {item["entry"].cluster_id: item.get("values") for item in self.rows}
        rows = []
        for entry in self.watch_entries:
            item = self.MakeRow(entry)
            item["values"] = old.get(entry.cluster_id)
            rows.append(item)
        self.rows = rows
        self.RenderTable()

    def RenderTable(self):
        """按行模型重画整张表（已抓到的数据通过 values 保留）。"""
        self.table.setRowCount(len(self.rows))
        self.row_urls.clear()
        for row, item in enumerate(self.rows):
            self.FillRow(row, item)

    def FillRow(self, row, item):
        """填充一行：优先显示本次抓到的值，其次缓存，最后留待抓取。"""
        values = item["values"] or {}
        record = item["record"] or {}
        cluster_id = item["entry"].cluster_id

        self.table.setRowHeight(row, IMAGE_SIZE + 8)

        name_text = (
            values.get("name")
            or record.get("name")
            or item["entry"].name
            or (FAILED_TEXT if values and not values.get("ok") else PENDING_TEXT)
        )
        name_item = QTableWidgetItem(name_text)
        if values and not values.get("ok"):
            name_item.setToolTip(values.get("error") or "未知错误")
        self.table.setItem(row, COL_NAME, name_item)

        self.table.setItem(row, COL_CID, QTableWidgetItem(cluster_id))

        def text(value):
            return str(value) if value else NO_DATA_TEXT

        self.table.setItem(row, COL_PRICE, QTableWidgetItem(text(values.get("price"))))
        self.table.setItem(row, COL_AVG, QTableWidgetItem(text(values.get("avg_price"))))
        deals = values.get("deals") or []
        for i in range(3):
            deal_text = (
                f"{deals[i]['price']} · {deals[i]['time']}"
                if i < len(deals) else NO_DATA_TEXT
            )
            self.table.setItem(row, COL_DEAL_BASE + i, QTableWidgetItem(deal_text))

        img_item = QTableWidgetItem()
        img_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, COL_IMG, img_item)
        if record.get("image_url"):
            self.image_fetcher.fetch(row, record["image_url"] + IMAGE_SUFFIX)

        url = links.build_detail_url(cluster_id, self.config["detail_url_template"])
        self.row_urls[row] = url
        link_item = QTableWidgetItem("打开")
        link_item.setData(Qt.UserRole, url)
        link_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, COL_LINK, link_item)

    def IsPolling(self) -> bool:
        return self.poller is not None and self.poller.isRunning()

    def SetButtonsEnabled(self, enabled: bool):
        for btn in (self.btn_import, self.btn_delete, self.btn_normalize,
                    self.btn_refresh_list, self.btn_fetch):
            btn.setEnabled(enabled)

    # ---------------- 清单写入 ----------------

    def SaveWatchlist(self):
        """按规范格式回写清单（名称取自抓取结果或缓存）。"""
        items = []
        for item in self.rows:
            values = item["values"] or {}
            record = item["record"] or {}
            name = (values.get("name") or record.get("name") or item["entry"].name or "")
            items.append((item["entry"].cluster_id, name))
        try:
            links.save_watchlist(self.watchlist_path, items)
        except OSError as err:
            QMessageBox.warning(self, "写入失败", f"写入 watchlist 出错：\n{err}")
            return False
        return True

    def OnNormalize(self):
        """手动整理清单：把链接、乱序格式统一成「clusterId | 商品名」。"""
        if self.SaveWatchlist():
            self.progress_label.setText("清单已按规范格式整理")

    def OnImport(self):
        """导入链接：追加到 watchlist.txt 末尾，并立即显示为新行。"""
        if self.IsPolling():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要导入的链接文件",
            os.path.dirname(self.watchlist_path), "Text (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            imported = links.load_links(path)
        except OSError as err:
            QMessageBox.warning(self, "导入失败", f"读取文件出错：\n{err}")
            return

        existing = {item["entry"].cluster_id for item in self.rows}
        new_entries = [e for e in imported if e.cluster_id not in existing]
        if not new_entries:
            self.progress_label.setText(f"没有新商品（文件里 {len(imported)} 条已全部在清单中）")
            return

        self.watch_entries.extend(new_entries)
        for entry in new_entries:
            self.rows.append(self.MakeRow(entry))
        self.store.sync([(e.cluster_id, e.name) for e in self.watch_entries])
        self.RenderTable()
        self.SaveWatchlist()
        self.progress_label.setText(
            f"已导入 {len(new_entries)} 条并追加到 watchlist 末尾，待抓取价格"
        )

    def OnDeleteSelected(self):
        """删除选中行：同时移出 watchlist.txt 和缓存（缓存以清单为准）。"""
        if self.IsPolling():
            return
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先在表格里选中要删除的商品。")
            return
        cluster_ids = [self.rows[r]["entry"].cluster_id for r in rows]
        answer = QMessageBox.question(
            self, "确认删除",
            f"将从 watchlist.txt 和本地缓存中删除这 {len(cluster_ids)} 件商品：\n"
            + "、".join(cluster_ids[:8])
            + ("…" if len(cluster_ids) > 8 else ""),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        keep = set(cluster_ids)
        self.rows = [item for item in self.rows if item["entry"].cluster_id not in keep]
        self.watch_entries = [e for e in self.watch_entries if e.cluster_id not in keep]
        for cluster_id in cluster_ids:
            self.store.delete_item(cluster_id)
        self.RenderTable()
        if self.SaveWatchlist():
            self.progress_label.setText(f"已删除 {len(cluster_ids)} 件商品")

    def OnRefreshList(self):
        """重新读取 watchlist.txt：新增的补成新行，删掉的移除，已抓数据保留。"""
        if self.IsPolling():
            return
        if not self.LoadWatchlist(self.watchlist_path):
            return
        self.progress_label.setText(f"清单已同步，共 {len(self.rows)} 件商品")

    # ---------------- 抓取 ----------------

    def OnFetchPrices(self):
        """抓取已有商品的价格；新商品连名称、缩略图一起抓。"""
        if self.IsPolling():
            return  # 轮询中不允许重复启动
        tasks = [(i, item["entry"]) for i, item in enumerate(self.rows)]
        if not tasks:
            QMessageBox.information(self, "提示", "watchlist 中没有商品链接。")
            return
        self.names_learned = False
        self.SetButtonsEnabled(False)
        self.poller = PollerThread(
            tasks,
            self.config["poll_interval_seconds"],
            self.config["retry_interval_seconds"],
            self.config["request_timeout_seconds"],
        )
        self.poller.result_ready.connect(self.OnResultReady)
        self.poller.progress_changed.connect(self.OnProgress)
        self.poller.finished_all.connect(self.OnPollFinished)
        self.poller.start()

    def OnProgress(self, current, total):
        self.progress_label.setText(f"查询中 {current}/{total}")

    def OnResultReady(self, row, result):
        if row >= self.table.rowCount():
            return
        item = self.rows[row]
        cluster_id = item["entry"].cluster_id

        if result["ok"]:
            # 第一次抓到的新商品写入缓存；已缓存的也顺手刷新名称/缩略图
            self.store.upsert_item(cluster_id, result["name"], result["image_url"])
            item["record"] = self.store.get_item(cluster_id)
            if result["name"] and result["name"] != item["entry"].name:
                self.names_learned = True
        item["values"] = result  # 失败的也记下来，重建表格时不会退回"待抓取"

        def text(value):
            return str(value) if value else NO_DATA_TEXT

        self.table.setItem(row, COL_PRICE, QTableWidgetItem(text(result["price"])))
        self.table.setItem(row, COL_AVG, QTableWidgetItem(text(result["avg_price"])))
        deals = result["deals"]
        for i in range(3):
            deal_text = (
                f"{deals[i]['price']} · {deals[i]['time']}"
                if i < len(deals) else NO_DATA_TEXT
            )
            self.table.setItem(row, COL_DEAL_BASE + i, QTableWidgetItem(deal_text))

        # 名称：接口返回的才是最新的；失败时如果原本没有名字，标出来而不是留个"…"
        name_item = self.table.item(row, COL_NAME)
        if result["ok"] and result["name"]:
            name_item.setText(result["name"])
            item["entry"].name = result["name"]
        elif not result["ok"]:
            name_item.setToolTip(result["error"] or "未知错误")
            if name_item.text() == PENDING_TEXT:
                name_item.setText(FAILED_TEXT)
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
        self.SetButtonsEnabled(True)
        if self.names_learned:
            # 抓到了新名称，顺手把清单刷新一次，方便用户直接在文件里管理
            if self.SaveWatchlist():
                self.progress_label.setText("完成，已把商品名写回 watchlist.txt")
            return
        self.progress_label.setText("完成")

    def OnCellClick(self, row, col):
        if col == COL_LINK:
            url = self.row_urls.get(row)
            if url:
                webbrowser.open(url)

    # ---------------- 主题 ----------------

    def InitialDark(self) -> bool:
        """启动时的主题：优先用上次手动选择的，没有则跟随系统。"""
        saved = self.store.get_setting("theme")
        if saved == "dark":
            return True
        if saved == "light":
            return False
        return theme.system_uses_dark()

    def ApplyTheme(self, dark: bool):
        self.dark = dark
        theme.apply_theme(QApplication.instance(), dark)
        self.btn_theme.setText("浅色模式" if dark else "暗色模式")
        self.btn_theme.setToolTip(
            "当前是暗色主题，点击切换为浅色" if dark else "当前是浅色主题，点击切换为暗色"
        )

    def OnToggleTheme(self):
        dark = not self.dark
        self.store.set_setting("theme", "dark" if dark else "light")
        self.ApplyTheme(dark)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    # 程序启动时：读取清单、同步缓存、显示已有数据；抓取由「抓取价格」按钮触发
    default_path = links.default_watchlist_path()
    if os.path.exists(default_path):
        window.LoadWatchlist(default_path)
    else:
        window.progress_label.setText("未找到 data/watchlist.txt")

    for warning in window.config_warnings:
        print(f"[config] {warning}")

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
