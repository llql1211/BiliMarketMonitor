"""市集商品价格监视器 · PyQt5 主窗口 + 轮询调度。

启动流程：
1. 读取 watchlist.txt，并让缓存库严格跟随清单（缺的补、多的删）；
2. 把能显示的数据（名称、缩略图、clusterID）摆上表格；
3. 点「开始抓取」时逐个抓取价格，新商品连名称、缩略图一起抓；
   抓取过程中可以「暂停抓取」（两条商品之间生效）或「停止抓取」（已抓到的保留）。

与旧版 src/App.py 的关键区别：轮询放在 QThread 子线程里做，
通过信号槽把每条结果回传主线程刷新表格，避免界面假死。
"""

import html
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime

import requests
from PyQt5.QtCore import QObject, QPoint, QRect, QSize, Qt, QThread, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QCursor,
    QDrag,
    QFontMetrics,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
    QTextEdit,
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

# 双击缩略图看的大图：原图是 1280x1280、动辄 1MB 出头，480w 有 30 多 KB 就够看清了
PREVIEW_SIZE = 480
PREVIEW_SUFFIX = f"@{PREVIEW_SIZE}w_{PREVIEW_SIZE}h_85q.webp"
PREVIEW_LOADING_TEXT = "正在加载大图…"
PREVIEW_FAILED_TEXT = "大图没下下来，稍后再双击试试"
NO_THUMBNAIL_TEXT = "这一行还没有缩略图，先抓取一次再双击查看"

ROW_NUMBER_PADDING = 16  # 序号槽左右留白，免得数字贴着分隔线

COL_IMG, COL_NAME, COL_CID, COL_PRICE, COL_REF, COL_AVG = 0, 1, 2, 3, 4, 5
COL_DEAL_BASE = 6  # 成交① 占 6/7/8 三列
COL_LINK = 9
HEADERS = ["缩略图", "商品名", "clusterID", "现价", "原价", "近30天均价",
           "成交①", "成交②", "成交③", "链接（点击打开）"]

PENDING_TEXT = "…"      # 等待抓取
NO_DATA_TEXT = "—"      # 无数据
CLEARED_TEXT = "--"     # 抓取开始前把价格清成这个，好看出刷到哪一行了
FAILED_TEXT = "（查询失败）"
SOLD_OUT_TEXT = "已售罄"  # 现价格的前缀：售罄时那一格装的不是市集现价

DELTA_ROLE = Qt.UserRole + 1        # 「现价」格末尾那截涨跌（形如「↓ 6」）
DELTA_COLOR_ROLE = Qt.UserRole + 2  # 上面那截的颜色（随主题走，重画时重算）
DELTA_GAP = " "                     # 现价与涨跌之间空一格

# 「现价」列一律左对齐：涨跌那截的落点是从文本区左边算起的（见 delta_rect）
PRICE_ALIGN = Qt.AlignLeft | Qt.AlignVCenter

PAUSE_TEXT = "暂停抓取"
RESUME_TEXT = "继续抓取"
ADD_PLACEHOLDER = "粘贴商品 ID 或分享链接，一行一个…"

# 一轮抓取跑完后的总结窗口（见 SummaryDialog）
SUMMARY_TITLE = "本轮抓取总结"
NO_CHANGE_TEXT = "本次没有价格变动"
CHANGE_UP, CHANGE_DOWN = "up", "down"
CHANGE_SOLD_OUT, CHANGE_ON_SALE = "sold_out", "on_sale"
CHANGE_LABELS = {
    CHANGE_DOWN: "降价",
    CHANGE_UP: "涨价",
    CHANGE_SOLD_OUT: "新售罄",
    CHANGE_ON_SALE: "恢复在售",
}
# 分类在头一句话里的排列顺序：先跌后涨，再状态变化
CHANGE_KINDS = (CHANGE_DOWN, CHANGE_UP, CHANGE_SOLD_OUT, CHANGE_ON_SALE)


def _skipped_note(duplicated: int, invalid: int) -> str:
    """「添加」结果里的补充说明：有几条已存在、几行没认出来。"""
    notes = []
    if duplicated:
        notes.append(f"{duplicated} 条已存在")
    if invalid:
        notes.append(f"{invalid} 行无法识别")
    return "（已忽略：" + "、".join(notes) + "）" if notes else ""


def _deal_text(deal) -> str:
    """一条成交记录的展示文本：「¥48 · 3天前」；没有时间就只显示价格，不留个空尾巴。"""
    if not isinstance(deal, dict):  # 渲染路径上多一层保险，别让脏数据把表格卡住
        return ""
    return " · ".join(part for part in (deal.get("price"), deal.get("time")) if part)


def _amount(value) -> str:
    """涨跌的数字：优先整数，其次两位小数（末尾凑数的 0 去掉，别写「6.50」）。"""
    rounded = round(abs(value), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0")


def price_delta(previous_price, previous_sold_out, current_price, current_sold_out):
    """两次抓取之间现价的变化，返回 (显示文本, 变化量)；没法比就给 None。

    三种情况不给涨跌：
    - 有一边是售罄：售罄行的价格装的是原价，跟市集现价比出来的"涨跌"没有意义，
      售罄前后更是两个不同的东西；
    - 价格认不出数字：宁可这一格不显示涨跌，也不要拿猜出来的数去误导人；
    - 两边一样：没变就不占地方，格子里干干净净一个价格，别尾随个「-」。
    """
    if previous_sold_out or current_sold_out:
        return None
    old = parser.price_number(previous_price)
    new = parser.price_number(current_price)
    if old is None or new is None:
        return None
    change = round(new - old, 2)
    if change > 0:
        return f"↑ {_amount(change)}", change
    if change < 0:
        return f"↓ {_amount(change)}", change
    return None


def price_change(previous, result):
    """本次抓到的结果相比缓存记录的一条变动；没变动（或没得比）就是 None。

    previous 是抓取前的缓存记录（可能为空），result 是本次解析成功的结果。
    返回的 dict 供总结窗口用：
      kind       up / down / sold_out / on_sale 四选一
      old_price  上一次的现价原文
      new_price  这一次的现价原文
      change     涨跌数字（只有 up/down 有，其余为 None）
      delta      形如「↓ 6」的显示文本（同上）

    判"没得比"的口径与 price_delta 一致：之前没抓到过价格就没有比较的基准
    （首次抓到不算变动）。售罄前后同样不比价格——那两个数不是一回事
    （见 PriceText）；但状态本身变了是实打实的一条变化，所以单独归成
    sold_out / on_sale 报出去。
    """
    if not isinstance(previous, dict) or not previous.get("price_text"):
        return None
    old_price = previous["price_text"]
    was_sold_out = bool(previous.get("sold_out"))
    is_sold_out = bool(result.get("sold_out"))
    if was_sold_out != is_sold_out:
        return {
            "kind": CHANGE_SOLD_OUT if is_sold_out else CHANGE_ON_SALE,
            "old_price": old_price,
            "new_price": result.get("price"),
            "change": None,
            "delta": "",
        }
    delta = price_delta(old_price, was_sold_out, result.get("price"), is_sold_out)
    if delta is None:
        return None
    return {
        "kind": CHANGE_UP if delta[1] > 0 else CHANGE_DOWN,
        "old_price": old_price,
        "new_price": result.get("price"),
        "change": delta[1],
        "delta": delta[0],
    }


def split_price_text(text, delta):
    """把「¥44 ↓ 6」拆成现价和涨跌两截（前半截连分隔的空格一起留下）。

    涨跌段永远拼在末尾，按长度切比找分隔符稳——价格前面还顶着「已售罄」，
    里面也没准带空格。
    """
    if not delta or not text.endswith(delta):
        return text, ""
    return text[: len(text) - len(delta)], delta


def delta_rect(metrics, text_rect, prefix, delta):
    """涨跌那截该画在哪儿：紧接在现价右边缘，跟现价同一行；挤不下就返回 None。

    位置按「基类给文字留的矩形 + 现价量出来的宽度」算——基类画现价时用的也是
    同一块矩形，所以两截能接上。prefix 带着分隔的空格，量出来的间距就跟
    单元格文本里的一致。
    """
    left = text_rect.left() + metrics.horizontalAdvance(prefix)
    # QRect.right() 是闭区间，算宽度时要补回这 1 像素，不然右边会缺一条
    if left + metrics.horizontalAdvance(delta) > text_rect.right() + 1:
        return None
    return QRect(left, text_rect.top(), text_rect.right() + 1 - left, text_rect.height())


def _display_time(value) -> str:
    """缓存里的 ISO 时间串 →「2026-09-16 10:30:00」；认不出来就原样返回。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _cached_note(updated_at) -> str:
    """「这价格是什么时候抓的」的提示语；没有时间戳就什么都不说。

    宁可少一句提示，也不要拿当前时间凑一个，那是在骗人。
    """
    stamp = _display_time(updated_at)
    return f"上次更新时间：{stamp}" if stamp else ""


def _tips(*parts) -> str:
    """拼提示文本：只留非空的那几段，一行一句。"""
    return "\n".join(part for part in parts if part)


def _esc(value) -> str:
    """转义要放进 HTML 的文本（商品名是接口给的，什么都可能有）。"""
    return html.escape("" if value is None else str(value))


def change_headline(changes) -> str:
    """总结窗口的头一句话：共几条变动、各是哪几类；没有变动就直说。

    总数与后面的分类计数都按传进来的条数算——分类认不出来时宁可在明细里
    照常列出来，也不能让头一句话的"共 N 条"对不上。
    """
    changes = [c for c in changes or [] if isinstance(c, dict)]
    if not changes:
        return NO_CHANGE_TEXT
    counts = {}
    for change in changes:
        kind = change.get("kind")
        counts[kind] = counts.get(kind, 0) + 1
    parts = [
        f"{CHANGE_LABELS[kind]} {counts[kind]}"
        for kind in CHANGE_KINDS
        if counts.get(kind)
    ]
    unknown = len(changes) - sum(counts.get(kind, 0) for kind in CHANGE_KINDS)
    if unknown:
        parts.append(f"其他 {unknown}")
    return f"共 {len(changes)} 条变动：" + " · ".join(parts)


def change_subtitle(total, failures) -> str:
    """总结窗口的第二行：这一轮抓了多少件、几件没抓到。

    查询失败的那几件这次没有可比的价格，不说明的话，"没有变动"看起来就像
    它们也没事——失败本身得在总结里有个交代。
    """
    text = f"本次共抓取 {total} 件商品"
    if failures:
        text += f"，其中 {failures} 件查询失败（这几件看不出变动）"
    return text


def changes_html(changes, dark) -> str:
    """变动明细的 HTML 表格：序号 / 商品 / 价格 / 变动，涨跌那格上色。

    只用 QTextEdit 确定认得的几个标签（table / td / span）：这是给总结窗口
    渲染的，不是网页。商品名一律转义，名字里出现尖括号也不能把表格拆了。
    """
    muted = theme.muted_color(dark).name()

    def head_cell(title):
        # 表头用弱化色：跟下面几条拉开层次，但不跟涨跌的红绿抢眼
        return f'<td><span style="color:{muted}">{title}</span></td>'

    cells = ["<tr>" + "".join(head_cell(t) for t in ("#", "商品", "价格", "变动")) + "</tr>"]
    for i, change in enumerate(changes or [], 1):
        kind = change.get("kind")
        if kind in (CHANGE_UP, CHANGE_DOWN):
            price = f'{_esc(change.get("old_price"))} → {_esc(change.get("new_price"))}'
            detail = _esc(change.get("delta"))
            color = theme.price_delta_color(change.get("change"), dark).name()
        elif kind == CHANGE_SOLD_OUT:
            price, detail, color = "已售罄", "此前现价 " + _esc(change.get("old_price")), muted
        else:  # CHANGE_ON_SALE：状态写在价格那格，价格有就跟着报一下
            price, detail, color = "恢复在售", _join("现价", _esc(change.get("new_price"))), muted
        cells.append(
            f'<tr><td>{i}</td><td>{_esc(change.get("name"))}</td><td>{price}</td>'
            f'<td><span style="color:{color}">{detail}</span></td></tr>'
        )
    return f'<table cellspacing="0" cellpadding="6" width="100%">{"".join(cells)}</table>'


def _join(*parts) -> str:
    """拼一段文本：只留非空的那几段，中间空一格。"""
    return " ".join(str(part) for part in parts if part)


def picked_rows(sources, count):
    """从 sources 里挑出合法行号（升序去重）：非整数、越界、bool 一律忽略。

    move_rows 用它决定挪哪几行，OnRowsDropped 用它记"挪的是哪几件商品"——
    两处必须按同一个口径认行号，所以只留这一份。
    """
    if not isinstance(sources, (list, tuple, set, frozenset)):
        return []
    return sorted({
        i for i in sources
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < count
    })


def move_rows(items, sources, target):
    """把 items 里 sources 这几行整体挪到 target 处（插在该位置之前），返回新列表。

    纯函数，不碰界面：表格、清单两边的重排都走它，规则只有这一份。

    行号按"挪动之前"的下标算，越界或不是整数的忽略；被挪的行之间保持原有的
    相对顺序。target 超出末尾按挪到末尾算，入参形状不对就原样返回——
    排序是给人看的，宁可不动也不要把清单弄乱。
    """
    items = list(items)
    picked = picked_rows(sources, len(items))
    if not picked or not isinstance(target, int) or isinstance(target, bool):
        return items

    target = max(0, min(target, len(items)))
    moving = [items[i] for i in picked]
    picked_set = set(picked)
    rest = [item for i, item in enumerate(items) if i not in picked_set]
    # 落在目标之前的行被抽走后，后面的行会整体前移，插回时要减掉它们的数量。
    # 例：[A,B,C,D] 把 A 挪到 C 前面 -> rest=[B,C,D]，insert = 2-1 = 1 -> [B,A,C,D]
    insert = max(0, min(target - sum(1 for i in picked if i < target), len(rest)))
    return rest[:insert] + moving + rest[insert:]


class PollerThread(QThread):
    """依次（非并发）查询每个商品，单条失败重试 1 次后放弃。

    tasks 为 [(行号, LinkEntry)]，只轮询 watchlist 里的商品。

    pause()/resume() 在两条商品之间生效，不会打断正在进行的请求；
    stop() 直接中止本次抓取（已经 emit 出去的结果由主线程保留）。
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
        self._paused = False

    def stop(self):
        self._stop = True
        self._paused = False  # 顺手解除暂停，否则线程会卡在暂停里退不出去

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def is_paused(self) -> bool:
        return self._paused

    def run(self):
        total = len(self.tasks)
        for i, (row, entry) in enumerate(self.tasks):
            if self._stop:
                break
            self._wait_if_paused()  # 暂停时不发下一个请求
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

    def _wait_if_paused(self):
        """暂停期间原地等着，直到 resume() 或 stop()。"""
        while self._paused and not self._stop:
            time.sleep(0.1)

    def _sleep(self, seconds):
        """分片 sleep，保证 pause()/stop() 能及时生效；暂停期间不计时。"""
        remaining = seconds
        while remaining > 0 and not self._stop:
            if self._paused:
                time.sleep(0.1)  # 暂停时不扣 remaining，恢复后接着睡完
                continue
            step = min(0.1, remaining)
            time.sleep(step)
            remaining -= step


class ImageFetcher(QObject):
    """在普通子线程里拉图，通过信号回传 QPixmap（避免卡界面）。

    信号里带的是调用方自己认的 key：缩略图用行号，双击放大用预览请求号。
    下载失败时发 failed 而不是静默丢弃——大图下不下来得跟用户说一声，
    缩略图那边不接这个信号，等于照旧不吭声。
    """

    fetched = pyqtSignal(int, QPixmap)
    failed = pyqtSignal(int)

    def fetch(self, key, url):
        threading.Thread(target=self._work, args=(key, url), daemon=True).start()

    def _work(self, key, url):
        pixmap = QPixmap()
        try:
            req = requests.get(url, timeout=10)
            pixmap.loadFromData(req.content)
        except Exception:
            pass  # 拿不到就当空图，统一走下面的失败分支
        if pixmap.isNull():
            self.failed.emit(key)
        else:
            self.fetched.emit(key, pixmap)


class PriceDeltaDelegate(QStyledItemDelegate):
    """「现价」列的绘制：现价照常画，末尾那截涨跌单独上色。

    一个 QTableWidgetItem 只有一种前景色，而「¥44 ↓ 6」要两截颜色，所以这一列
    自己画：先让基类按平常的样子画（背景、隔行色、选中态、对齐都一样），只是
    交给它的文本先收窄成现价那半截，再按同一个文本矩形接着往后补上涨跌。

    文本位置由基类说了算，这里不自己摆——列宽不够时基类会加省略号，位置对不上
    的话两截会叠在一起。
    """

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.text, _ = split_price_text(option.text, index.data(DELTA_ROLE))

    def paint(self, painter, option, index):
        delta = index.data(DELTA_ROLE)
        if not delta:
            super().paint(painter, option, index)
            return

        super().paint(painter, option, index)  # 现价那半截（已由上面收窄）

        prefix, delta = split_price_text(index.data(Qt.DisplayRole) or "", delta)
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        # 基类给文字留的那块地方：界面上"现价"取的就是它的左边缘
        text_rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, style_option, option.widget
        )

        rect = delta_rect(QFontMetrics(style_option.font), text_rect, prefix, delta)
        if rect is None:
            return  # 挤不下就只留现价，完整的「¥44 ↓ 6」还在悬停提示里

        painter.save()
        painter.setFont(style_option.font)
        painter.setPen(index.data(DELTA_COLOR_ROLE) or style_option.palette.text().color())
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, delta)
        painter.restore()


class ImagePreviewDialog(QDialog):
    """双击缩略图弹出的大图。

    开窗不等图：大图比缩略图大几百倍，下载要一会儿，所以先把窗口摆出来，
    里面写着「正在加载大图…」，图回来了再换上（见 MainWindow.OnPreviewFetched）。
    窗口不开模态——看图的当口还想顺手点点表格，是很自然的事。
    """

    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or "商品图")
        # 固定成图那么大：换成图时不至于整窗跳一下，也省得被长文案撑变形
        self.setFixedSize(PREVIEW_SIZE + 48, PREVIEW_SIZE + 80)

        layout = QVBoxLayout(self)
        self.label = QLabel(PREVIEW_LOADING_TEXT)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        layout.addWidget(self.label, alignment=Qt.AlignCenter)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def SetImage(self, pixmap):
        """贴上大图。缩放兜个底：CDN 裁剪理论上给的就是 480，万一不是也别撑破窗口。"""
        self.label.setText("")
        self.label.setPixmap(
            pixmap.scaled(
                PREVIEW_SIZE, PREVIEW_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )

    def SetFailed(self):
        self.label.setText(PREVIEW_FAILED_TEXT)


class AddDialog(QDialog):
    """添加商品：多行输入 ID/链接（一行一个），比单行输入框多留了批量粘贴的余地。"""

    def __init__(self, dark: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加商品")
        self.resize(480, 260)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("商品 ID 或分享链接，一行一个："))

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(ADD_PLACEHOLDER)
        theme.style_placeholder(self.editor, dark)  # 灰字，深浅主题下都要看得清
        layout.addWidget(self.editor)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.editor.setFocus()

    def text(self) -> str:
        return self.editor.toPlainText()


class SummaryDialog(QDialog):
    """一轮抓取跑完后的总结窗口：先说共几条变动，再逐条列出明细。

    不开模态（和看图那个窗口同理）：总结是"看一眼"的东西，读的时候顺手点点
    表格、打开个详情页都很自然，没必要把主窗口锁住。

    没有变动时也照样弹（用户要的是每轮都有个收尾交代），此时藏掉明细那块空框。
    """

    def __init__(self, changes, total, failures, dark, parent=None):
        super().__init__(parent)
        self.changes = [c for c in changes or [] if isinstance(c, dict)]
        self.total = total
        self.failures = failures
        self.setWindowTitle(SUMMARY_TITLE)
        self.setMinimumWidth(360)  # 没有明细时窗口会收得很窄，别让按钮挤成一团

        layout = QVBoxLayout(self)
        self.headline = QLabel()
        font = self.headline.font()
        font.setBold(True)  # 头一句「共 N 条变动」要压过下面的明细
        self.headline.setFont(font)
        self.subtitle = QLabel()
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.headline)
        layout.addWidget(self.subtitle)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)  # 只读，但留着选中复制
        self.detail.setMinimumSize(520, 240)
        layout.addWidget(self.detail)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.SetDark(dark)
        # 定个大小而不是 adjustSize()：QTextEdit 的 sizeHint 跟着内容长，
        # 几十条变动时会把窗口撑成一条竖带。写死之后长清单在里面滚。
        if self.changes:
            self.resize(620, 420)
        else:
            self.resize(460, 180)  # 没有明细，矮一点就够

    def SetDark(self, dark):
        """按主题重新渲染文案与配色（主窗口切换主题时再叫一次）。

        文案本来跟主题无关，但一起重渲染最省事——改动只有一处，
        不会出现"换了主题、数字还是旧的"这种对不上的情况。
        """
        self.headline.setText(change_headline(self.changes))
        self.subtitle.setText(change_subtitle(self.total, self.failures))
        self.detail.setHtml(changes_html(self.changes, dark))
        self.detail.setVisible(bool(self.changes))  # 没有明细就别摆个空框


class ReorderableTable(QTableWidget):
    """行可以拖拽排序的表格：把行拖到别处松手，顺序就变了。

    表格只是"顺序"的视图——真正的顺序在 MainWindow.rows 和 watchlist.txt 里，
    所以这里算出落点后只发信号，一行都不自己搬。

    拖拽由本类整个接管（`startDrag` 里自己起一个 QDrag），不能退回用 Qt 现成的
    InternalMove：那条路上 Qt 搬完格子还会把源行从模型里删掉，表格就和窗口那边的
    行模型对不上了（QAbstractItemViewPrivate::clearOrRemove，拖拽的最终动作是
    Move 时触发）。想靠在 dropEvent 里把动作改成 Copy 拦住它也不行——Qt 只在
    候选集里挑动作，挑不中会静默退回默认动作（实测 Move→Copy 的降级无效）。
    自己起 QDrag 就没有这一步，`drag.exec_()` 的返回值没人接，源行自然没人动。
    """

    rows_dropped = pyqtSignal(list, int)  # 被拖的行号（升序）, 目标插入位置

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 关掉"覆盖"模式（默认是开的）：这模式是为"拖一格盖一格"设计的，
        # 和这里的"插到某两行之间"不是一回事
        self.setDragDropOverwriteMode(False)
        # 落点提示自己画（见 paintEvent）：Qt 自带的只在行上沿/下沿各 2px 内
        # 显示，一行的中间那 90% 压根没提示（实测那里给的是 OnViewport），
        # 拖动时等于看不见落点在哪儿
        self.setDropIndicatorShown(False)
        self._drop_at = None  # 正在拖时：落点（插入位置）；None 表示没在拖

    def startDrag(self, actions):
        """自己起拖拽，绕开 QAbstractItemView.startDrag 的"搬格子 + 删源行"。

        拖拽内容用模型自己的 mime（application/x-qabstractitemmodeldatalist），
        免得 Qt 那边 canDrop 认不出来，拖动过程中什么都不响应。
        """
        rows = self._selected_rows()
        if not rows:
            return
        mime = self.model().mimeData([
            self.model().index(row, col)
            for row in rows
            for col in range(self.columnCount())
        ])
        if mime is None:  # 模型给不出拖拽内容就干脆不拖，别起一个空拖拽
            return

        drag = QDrag(self)
        drag.setMimeData(mime)  # 所有权随之转移给 drag，不用自己释放
        pixmap, hotspot = self._drag_pixmap(rows)
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
            drag.setHotSpot(hotspot)
        drag.exec_(Qt.MoveAction)
        drag.deleteLater()

    def dragEnterEvent(self, event):
        """只接自己拖自己；从文件管理器之类拖进来的内容一概不理。

        这一关就拦在这里：Qt 的规矩是 dragEnter 不接，后面的 dragMove / drop
        压根不会送过来，所以底下几处不用再各查一遍。
        （另外"从资源管理器拖文件进来"这类来源，source() 本来就是空的。）
        """
        if event.source() is self:
            super().dragEnterEvent(event)  # 交给 Qt：它还要置拖拽状态、启动自动滚动
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)  # 自动滚动、拖拽状态这些还得 Qt 管
        self._set_drop_hint(self._drop_target(event.pos()))
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_drop_hint(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        """接住落点，交给窗口去重排——本类自己一行都不动。"""
        rows = self._selected_rows()
        target = self._drop_target(event.pos())
        self._set_drop_hint(None)
        if not rows:
            event.ignore()
            return
        event.accept()
        self.rows_dropped.emit(rows, target)

    def paintEvent(self, event):
        """正常画表格，顺带把落点那条横线画上。"""
        super().paintEvent(event)
        if self._drop_at is None:
            return
        y = self._drop_hint_y(self._drop_at)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(self.palette().highlight().color(), 2))
        painter.drawLine(0, y, self.viewport().width(), y)

    # ---------------- 落点提示 ----------------

    def _set_drop_hint(self, target):
        """记住落点并重画那条横线；target 为 None 表示没有落点。"""
        if target != self._drop_at:
            self._drop_at = target
            self.viewport().update()

    def _drop_hint_y(self, target) -> int:
        """插入位置换算成横线的 y 坐标（行号会被滚动裁掉，所以得夹在可视区内）。"""
        viewport = self.viewport().rect()
        if target >= self.rowCount():
            rect = self.visualRect(self.model().index(self.rowCount() - 1, 0))
            y = rect.bottom() + 1 if rect.isValid() else viewport.bottom()
        else:
            rect = self.visualRect(self.model().index(target, 0))
            y = rect.top() if rect.isValid() else viewport.top()
        return max(viewport.top(), min(y, viewport.bottom()))

    # ---------------- 内部 ----------------

    def _selected_rows(self):
        """当前选中的行号（升序）。

        拖拽开始和放下时都以选区为准——拖拽期间 Qt 不会动选区，
        两边拿到的是同一批行。
        """
        return sorted({index.row() for index in self.selectedIndexes()})

    def _drop_target(self, pos) -> int:
        """落点换算成插入位置：落在某行上半格就插它前面，下半格插它后面。"""
        index = self.indexAt(pos)
        if not index.isValid():
            return self.rowCount()  # 表格下方的空白区：挪到最后
        rect = self.visualRect(index)
        return index.row() + (1 if pos.y() > rect.center().y() else 0)

    def _drag_pixmap(self, rows):
        """被拖的那几行的截图，拖拽时跟着光标走，返回 (图, 光标在图上的位置)。"""
        rect = QRect()
        for row in rows:
            rect = rect.united(self.visualRect(self.model().index(row, 0)))
        rect = rect.intersected(self.viewport().rect())
        if rect.isEmpty():
            return QPixmap(), QPoint()
        cursor = self.viewport().mapFromGlobal(QCursor.pos())
        return self.viewport().grab(rect), cursor - rect.topLeft()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # 先定主题标记：下面填单元格时要按它取颜色（「原价」的弱化色等），
        # 真正的样式在 InitUI 之后由 ApplyTheme 装上
        self.dark = False
        self.config, self.config_warnings = config.load_config()
        # 近期成交高亮：阈值换算成秒、颜色解析成画刷，启动时各算一次就够
        self.deal_highlight_seconds = self.config["deal_highlight_within_hours"] * 3600
        self.deal_highlight_brush = QBrush(
            theme.deal_highlight_color(self.config["deal_highlight_color"])
        )
        self.store = store.Store(store.default_db_path())
        # 构造阶段的提示（例如缓存库损坏已备份重建）要先留下来：
        # 后面任何一次正式操作都会清空 store.notes
        self.store_warnings = list(self.store.notes)
        self.last_note = ""      # 最近一次子模块降级提示的文案，供状态栏拼接
        self.watchlist_path = links.default_watchlist_path()
        self.watch_entries = []  # List[links.LinkEntry]，来自 watchlist.txt
        self.rows = []           # 表格行模型：见 MakeRow() 的字段说明
        self.row_urls = {}       # 行号 -> 详情页链接
        self.row_images = {}     # 行号 -> 缩略图原始地址（下载回来时用来认领）
        self.icons = {}          # 缩略图原始地址 -> QPixmap，重画表格时复用
        self.poller = None
        self.names_learned = False  # 本次抓取是否学到了新名称（决定要不要回写清单）
        self.poller_stopped = False  # 本次抓取是否被「停止抓取」中止
        self.run_changes = []    # 本次抓取的价格变动，跑完弹总结用（见 price_change）
        self.run_failures = 0    # 本次抓取查询失败的条数：失败的那几件看不出变动
        self.summary_dialog = None  # 最近一次弹的总结窗口，切主题时要跟着重画
        self.image_fetcher = ImageFetcher()  # 缩略图：key 是行号
        self.image_fetcher.fetched.connect(self.OnImageFetched)
        # 双击放大：另起一个 fetcher，因为它的 key 是预览请求号而不是行号。
        # 共用一个的话，两种号会混在一个槽里分不清谁是谁
        self.preview_fetcher = ImageFetcher()
        self.preview_fetcher.fetched.connect(self.OnPreviewFetched)
        self.preview_fetcher.failed.connect(self.OnPreviewFailed)
        self.preview_seq = 0     # 大图请求号的发号器，一次一张，回包靠它认领
        self.previews = {}       # 未回来的大图请求号 -> (预览窗口, 图片地址)
        self.preview_images = {}  # 图片地址 -> 大图，看过的不再下一遍
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
        self.btn_add = QPushButton("添加…")
        self.btn_delete = QPushButton("删除选中")
        self.btn_normalize = QPushButton("整理清单")
        self.btn_refresh_list = QPushButton("刷新商品列表")
        self.btn_fetch = QPushButton("开始抓取")
        self.btn_pause = QPushButton(PAUSE_TEXT)
        self.btn_stop = QPushButton("停止抓取")
        for btn, slot, tip in (
            (self.btn_add, self.OnAdd,
             "输入商品 ID 或分享链接（一行一个），追加写入 watchlist.txt 末尾"),
            (self.btn_delete, self.OnDeleteSelected,
             "把选中的商品从 watchlist.txt 和缓存中删除"),
            (self.btn_normalize, self.OnNormalize,
             "按「clusterId | 商品名」格式重写 watchlist.txt"),
            (self.btn_refresh_list, self.OnRefreshList,
             "重新读取 watchlist.txt（手工改动后点这里同步）"),
            (self.btn_fetch, self.OnFetchPrices,
             f"逐个抓取价格，间隔 {self.config['poll_interval_seconds']:g} 秒"),
            (self.btn_pause, self.OnTogglePause,
             "暂停 / 继续本次抓取（在两条商品之间生效，不打断正在进行的请求）"),
            (self.btn_stop, self.OnStopFetch,
             "中止本次抓取，已经抓到的结果会保留在表格和缓存里"),
        ):
            btn.setToolTip(tip)
            btn_bar.addWidget(btn)
        self.btn_add.clicked.connect(self.OnAdd)
        self.btn_delete.clicked.connect(self.OnDeleteSelected)
        self.btn_normalize.clicked.connect(self.OnNormalize)
        self.btn_refresh_list.clicked.connect(self.OnRefreshList)
        self.btn_fetch.clicked.connect(self.OnFetchPrices)
        self.btn_pause.clicked.connect(self.OnTogglePause)
        self.btn_stop.clicked.connect(self.OnStopFetch)

        btn_bar.addStretch()
        self.progress_label = QLabel("就绪")
        self.btn_theme = QPushButton()
        self.btn_theme.clicked.connect(self.OnToggleTheme)
        btn_bar.addWidget(self.progress_label)
        btn_bar.addWidget(self.btn_theme)
        layout.addLayout(btn_bar)

        # 表格
        self.table = ReorderableTable(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        # 现价列自己画：现价和涨跌要两截颜色（见 PriceDeltaDelegate）
        self.table.setItemDelegateForColumn(COL_PRICE, PriceDeltaDelegate(self.table))
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 行可拖拽排序（抓取中会被 SetBusy 关掉）
        self.table.setDragDropMode(QAbstractItemView.InternalMove)
        self.table.setAlternatingRowColors(True)
        # 序号槽（垂直表头）：排在数据列左边，横向滚动时钉在最左，不跟着列走
        row_number = self.table.verticalHeader()
        row_number.setVisible(True)
        row_number.setDefaultAlignment(Qt.AlignCenter)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setIconSize(QSize(IMAGE_SIZE, IMAGE_SIZE))
        header.resizeSection(COL_IMG, IMAGE_SIZE + 8)
        header.resizeSection(COL_PRICE, 120)  # 要放得下「¥90.50 ↓ 12.30」这种
        header.resizeSection(COL_REF, 90)
        header.resizeSection(COL_AVG, 100)
        for col in (COL_DEAL_BASE, COL_DEAL_BASE + 1, COL_DEAL_BASE + 2):
            header.resizeSection(col, 120)
        header.resizeSection(COL_CID, 100)
        header.resizeSection(COL_LINK, 60)
        # 商品名占剩余全部横向空间
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        # 模板说明挂在「链接」列头上。以前是挂在主窗口上的，但 Qt 找 tooltip 会沿
        # 父链上溯，于是每个没设自己 tooltip 的单元格都会弹它。
        self.table.horizontalHeaderItem(COL_LINK).setToolTip(
            f"点击「打开」用浏览器访问商品详情页\n链接由配置里的模板拼出：\n{template}"
        )
        self.table.cellClicked.connect(self.OnCellClick)
        self.table.cellDoubleClicked.connect(self.OnCellDoubleClick)
        self.table.rows_dropped.connect(self.OnRowsDropped)
        layout.addWidget(self.table)

        self.setCentralWidget(central)
        self.SetBusy(False)  # 空闲态：暂停/停止置灰

    # ---------------- 数据载入 ----------------

    def Note(self, *groups) -> str:
        """汇总子模块的降级提示：打印留档，返回可拼到状态栏的短句（没有则空串）。

        子模块（links/store）不抛异常，只把"这次哪里降级了"写进 notes 列表，
        统一在这里收集——留痕，但不打断用户正在做的事。
        """
        messages = [m for group in groups for m in (group or [])]
        for message in messages:
            print(f"[降级] {message}")
        return ("；" + "；".join(messages)) if messages else ""

    def LoadWatchlist(self, path, show_errors=True):
        """读取清单 -> 同步缓存 -> 重建表格。"""
        notes = []
        try:
            entries = links.load_links(path, notes)
        except OSError as err:
            if show_errors:
                QMessageBox.warning(self, "读取失败", f"读取 watchlist 出错：\n{err}")
            return False
        self.watchlist_path = path
        self.watch_entries = entries
        added, removed = self.store.sync(
            [(e.cluster_id, e.name) for e in entries]
        )
        # store.notes 每次操作都会重置，必须紧接着读；下面 RebuildRows 会查缓存
        self.last_note = self.Note(notes, self.store.notes)
        # 保留本次运行已抓到的值：手动改完清单点「刷新商品列表」时，
        # 已查到的价格不该退回「—」（值按 clusterId 对齐，重排/增删都安全）。
        # 启动时 self.rows 还是空的，这条保留对首次载入没有任何影响。
        self.RebuildRows()
        self.progress_label.setText(
            f"共 {len(self.rows)} 件商品（缓存新增 {added}，删除 {removed}）"
            f"，点「开始抓取」开始查询" + self.last_note
        )
        return True

    def MakeRow(self, entry):
        """行模型字段：

        entry          LinkEntry，来自清单（本工具只跟踪清单里的商品）
        record         缓存记录（名称、缩略图、上次抓到的价格）
        values         本次运行抓到的结果，表格重建时用来保留已显示的数据
        delta          本次抓取相比上次缓存价格的涨跌 (文本, 变化量)，没得比就是 None
        price_cleared  这一行在本次抓取里还没轮到，价格格留成「--」
        """
        return {
            "entry": entry,
            "record": self.store.get_item(entry.cluster_id),
            "values": None,
            "delta": None,
            "price_cleared": False,
        }

    def RebuildRows(self, keep_values=True):
        """按清单重建行模型；keep_values=False 时丢弃本次抓到的数据。"""
        old = {}
        if keep_values:
            old = {
                item["entry"].cluster_id: (
                    item.get("values"), item.get("delta"), item.get("price_cleared")
                )
                for item in self.rows
            }
        rows = []
        for entry in self.watch_entries:
            item = self.MakeRow(entry)
            values, delta, cleared = old.get(entry.cluster_id, (None, None, False))
            item["values"] = values
            item["delta"] = delta
            item["price_cleared"] = cleared
            rows.append(item)
        self.rows = rows
        self.RenderTable()

    def NumberRows(self):
        """重编序号。序号就是清单顺序，重排、增删、刷新后都得再编一遍。

        setRowCount 会把序号清空，所以每次重画都得跟着走。
        """
        count = len(self.rows)
        self.table.setVerticalHeaderLabels([str(i + 1) for i in range(count)])
        # 按最大序号定宽，涨到两位数、三位数时它自己会变宽；位数没变就不动，
        # 免得每重画一次就抖一下
        header = self.table.verticalHeader()
        width = header.fontMetrics().horizontalAdvance(str(max(count, 1))) + ROW_NUMBER_PADDING
        if header.width() != width:
            header.setFixedWidth(width)

    def RenderTable(self):
        """按行模型重画整张表（已抓到的数据通过 values 保留）。"""
        self.table.setRowCount(len(self.rows))
        self.NumberRows()
        self.row_urls.clear()
        self.row_images.clear()  # 行号会整体挪位，缩略图的归属得跟着重算
        for row, item in enumerate(self.rows):
            self.FillRow(row, item)

    def SetThumbnail(self, row, image_url):
        """给某行配缩略图：缓存里有就直接贴，没有才后台下载。

        拖拽排序、增删、刷新清单都会整表重画，走到这里；没有这层缓存的话，
        每重画一次就要把所有缩略图重下一遍——图标闪一下，还白发一堆请求。
        """
        if not image_url:
            return
        self.row_images[row] = image_url
        pixmap = self.icons.get(image_url)
        if pixmap is None:
            self.image_fetcher.fetch(row, image_url + IMAGE_SUFFIX)
        else:
            self.PutThumbnail(row, pixmap)

    def PutThumbnail(self, row, pixmap):
        item = self.table.item(row, COL_IMG)
        if item is not None:
            item.setIcon(QIcon(pixmap))

    def OnCellDoubleClick(self, row, col):
        """双击缩略图看大图。双击别的格子不管：那儿的双击另有用途（改列宽等）。"""
        if col != COL_IMG:
            return
        self.PreviewRow(row)

    def PreviewRow(self, row):
        """打开某行的商品大图。这一行还没有缩略图时只在状态栏说一声。"""
        url = self.row_images.get(row)
        if not url:
            self.progress_label.setText(NO_THUMBNAIL_TEXT + self.last_note)
            return None
        name = self.rows[row]["entry"].name if row < len(self.rows) else ""
        return self.OpenPreview(url, name or f"第 {row + 1} 行")

    def OpenPreview(self, image_url, title=""):
        """开一个预览窗口显示大图：看过的一张直接贴，没看过的后台去下。

        大图不并进 icons 那个缓存：那里存的是缩略图，混进 480 的大图之后，
        同一个地址下次渲染缩略图时就会拿到大图（96 的格子塞 480 的图）。
        """
        dialog = ImagePreviewDialog(title, self)
        dialog.show()

        pixmap = self.preview_images.get(image_url)
        if pixmap is not None:
            dialog.SetImage(pixmap)
            return dialog

        self.preview_seq += 1
        request = self.preview_seq
        self.previews[request] = (dialog, image_url)
        # 关窗就把请求号摘掉：回包时人已经走了，别往一个已销毁的窗口上贴图
        dialog.finished.connect(lambda _code, req=request: self.previews.pop(req, None))
        self.preview_fetcher.fetch(request, image_url + PREVIEW_SUFFIX)
        return dialog

    def OnPreviewFetched(self, request, pixmap):
        """大图到手：还在等的窗口才贴，已经关掉的就当没下过。"""
        pending = self.previews.pop(request, None)
        if pending is None:
            return
        dialog, image_url = pending
        self.preview_images[image_url] = pixmap
        dialog.SetImage(pixmap)

    def OnPreviewFailed(self, request):
        pending = self.previews.pop(request, None)
        if pending is not None:
            pending[0].SetFailed()  # 窗口还开着才提示，关掉了就当没这回事

    def MakeCell(self, text, tooltip=None, align=None, color=None):
        """建单元格。tooltip 默认为该格的完整文本——列宽不够被截断时也能看全。

        文本为空或只是占位符（—/…）时不挂，免得弹出来一句没信息量的提示。
        color 给「原价」这类次要信息用；颜色随主题走，换主题时由调用方重画。
        """
        item = QTableWidgetItem(text)
        if tooltip is None and text not in ("", NO_DATA_TEXT, PENDING_TEXT):
            tooltip = text
        if tooltip:
            item.setToolTip(tooltip)
        if align is not None:
            item.setTextAlignment(align)
        if color is not None:
            item.setForeground(QBrush(color))
        return item

    def PriceText(self, price, sold_out) -> str:
        """「现价」格的文本。

        售罄时接口给的 firstPrice 其实是原价（测过：同款在售时它等于划线价），
        直接摆进现价列会让人以为还能按这个价买到，所以前面盖一句「已售罄」。
        """
        if price and sold_out:
            return f"{SOLD_OUT_TEXT} {price}"
        return str(price) if price else NO_DATA_TEXT

    def PriceTooltip(self, sold_out):
        """售罄行的现价格要解释一句：那个数不是市集现价。"""
        return "该商品已售罄，这里的价格是原价而非市集现价" if sold_out else ""

    def ReferenceCell(self, reference_price):
        """「原价」格：弱化色显示，跟现价拉开层次。"""
        return self.MakeCell(
            str(reference_price) if reference_price else NO_DATA_TEXT,
            color=theme.muted_color(self.dark),
        )

    def ClearedCell(self):
        """待抓取中的占位格：一个「--」，不挂提示（提示里只有「--」等于没说）。"""
        return self.MakeCell(CLEARED_TEXT, tooltip="")

    def PriceCell(self, price, sold_out, note="", delta=None):
        """「现价」格：现价（售罄时带前缀）+ 涨跌，涨跌单独上色。

        对齐固定成 PRICE_ALIGN：涨跌那截的落点是按"现价从文本区左边起"算的
        （见 delta_rect），居中或右对齐就会两截叠在一起。

        note 是「这价格是什么时候抓的」那类补充说明，有它就不再退回"提示即全文"。
        """
        text = self.PriceText(price, sold_out)
        if delta:
            text += DELTA_GAP + delta[0]
        cell = self.MakeCell(
            text,
            tooltip=_tips(self.PriceTooltip(sold_out), note) or None,
            align=PRICE_ALIGN,
        )
        if delta:
            cell.setData(DELTA_ROLE, delta[0])
            cell.setData(DELTA_COLOR_ROLE, theme.price_delta_color(delta[1], self.dark))
        return cell

    def SetPriceCells(self, row, item):
        """填「现价 / 原价 / 近30天均价」三格。

        取值优先级：这次抓到的 > 缓存里上次抓到的 > 占位符。抓失败时退回缓存，
        而不是把已经看到过的价格清掉——一次网络抖动不该让人白记一遍；代价是
        得在提示里说清这个数是什么时候抓的（_cached_note）。

        首屏渲染（FillRow）和抓取回填（OnResultReady）都走这里，
        免得同一段渲染逻辑写两遍，哪天真改出不一致来。
        """
        if item.get("price_cleared"):  # 本次抓取还没轮到它，先留个空
            self.table.setItem(row, COL_PRICE, self.ClearedCell())
            self.table.setItem(row, COL_REF, self.ClearedCell())
            self.table.setItem(row, COL_AVG, self.ClearedCell())
            return

        values = item["values"] or {}
        record = item["record"] or {}
        if values.get("ok"):
            price, sold_out = values.get("price"), values.get("sold_out")
            reference, avg = values.get("reference_price"), values.get("avg_price")
            note = ""
        else:
            price, sold_out = record.get("price_text"), record.get("sold_out")
            reference, avg = record.get("reference_price"), record.get("avg_text")
            note = _cached_note(record.get("price_updated_at"))

        if sold_out and not reference:
            # 售罄时接口整组不返回 price/priceSymbol，现价格里那个数就是原价
            # （见 PriceText）。原价列照抄一份：不然同一行「现价」顶着个数字、
            # 「原价」空着，看着像这格没抓到。
            reference = price

        self.table.setItem(
            row, COL_PRICE, self.PriceCell(price, sold_out, note, item.get("delta"))
        )
        self.table.setItem(row, COL_REF, self.ReferenceCell(reference))
        self.table.setItem(
            row, COL_AVG, self.MakeCell(str(avg) if avg else NO_DATA_TEXT)
        )

    def ClearPrices(self):
        """抓取开始前把各行的价格清成「--」：从空开始涨，才看得出刷到哪一行了。

        清的是现价/原价/均价三格——它们由同一次请求一起回来，只清一个反而怪。
        标记记在行模型上（price_cleared），这样抓取中途换主题重画表格时，
        已经抓到的行照常显示新价，没轮到的还是「--」。
        """
        for row, item in enumerate(self.rows):
            item["price_cleared"] = True
            self.SetPriceCells(row, item)

    def RestorePendingPrices(self):
        """收尾：这一轮没轮到的行把价格还回来。

        那些价格只是被"清空待抓"，数据还在缓存里（或者这次抓了个失败）；
        跑完还留着一片「--」，看起来就像价格被弄丢了。
        """
        for row, item in enumerate(self.rows):
            if item.get("price_cleared"):
                item["price_cleared"] = False
                self.SetPriceCells(row, item)

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
        # 抓失败时，名称格的提示换成失败原因（比重复一遍名称有用）
        name_tip = (values.get("error") or "未知错误") if values and not values.get("ok") else None
        self.table.setItem(row, COL_NAME, self.MakeCell(name_text, tooltip=name_tip))

        self.table.setItem(row, COL_CID, self.MakeCell(cluster_id))

        self.SetPriceCells(row, item)
        self.SetDealCells(row, values.get("deals"))

        self.table.setItem(
            row, COL_IMG, self.MakeCell("", tooltip="双击查看大图", align=Qt.AlignCenter)
        )
        self.SetThumbnail(row, record.get("image_url"))

        url = links.build_detail_url(cluster_id, self.config["detail_url_template"])
        self.row_urls[row] = url
        link_item = self.MakeCell("打开", tooltip=url, align=Qt.AlignCenter)
        link_item.setData(Qt.UserRole, url)
        self.table.setItem(row, COL_LINK, link_item)

    def SetDealCells(self, row, deals):
        """填「成交①/②/③」三格，够新鲜的那几条加粗 + 高亮色。

        首屏渲染（FillRow）和抓取回填（OnResultReady）都走这里，
        免得同一段渲染逻辑写两遍，哪天真改出不一致来。
        """
        deals = deals or []
        for i in range(3):
            if i < len(deals):
                deal = deals[i]
                item = self.MakeCell(_deal_text(deal))
                if self.IsFreshDeal(deal):
                    font = item.font()
                    font.setBold(True)  # 只改粗细，字号字族照旧
                    item.setFont(font)
                    item.setForeground(self.deal_highlight_brush)
            else:
                item = self.MakeCell(NO_DATA_TEXT)  # 成交不足 3 条，多的格子留占位符
            self.table.setItem(row, COL_DEAL_BASE + i, item)

    def IsFreshDeal(self, deal) -> bool:
        """这条成交是否「够新鲜」——够新鲜才加粗上色。

        阈值为 0 表示关闭高亮；时间认不出来（age_seconds 为 None）的一律不算，
        宁可少高亮一处，也不要拿猜出来的新旧去误导人。
        """
        if self.deal_highlight_seconds <= 0 or not isinstance(deal, dict):
            return False
        age = deal.get("age_seconds")
        if isinstance(age, bool) or not isinstance(age, int):
            return False
        return age <= self.deal_highlight_seconds

    def IsPolling(self) -> bool:
        return self.poller is not None and self.poller.isRunning()

    def SetBusy(self, busy: bool):
        """抓取中：清单管理类按钮置灰、暂停/停止放开；空闲时反过来。"""
        for btn in (self.btn_add, self.btn_delete, self.btn_normalize,
                    self.btn_refresh_list, self.btn_fetch):
            btn.setEnabled(not busy)
        self.btn_pause.setEnabled(busy)
        self.btn_stop.setEnabled(busy)
        self.btn_pause.setText(PAUSE_TEXT)  # 每次进出都复位成「暂停抓取」
        # 抓取中也不许拖拽排序：轮询任务记的是"第几行"，
        # 中途插队会让已经发出的结果填到别的商品上
        self.table.setDragDropMode(
            QAbstractItemView.NoDragDrop if busy else QAbstractItemView.InternalMove
        )

    # ---------------- 清单写入 ----------------

    def SaveWatchlist(self):
        """按规范格式回写清单（名称取自抓取结果或缓存）。"""
        items = []
        for item in self.rows:
            values = item["values"] or {}
            record = item["record"] or {}
            name = (values.get("name") or record.get("name") or item["entry"].name or "")
            items.append((item["entry"].cluster_id, name))
        notes = []
        try:
            links.save_watchlist(self.watchlist_path, items, notes)
        except OSError as err:
            QMessageBox.warning(self, "写入失败", f"写入 watchlist 出错：\n{err}")
            return False
        self.last_note = self.Note(notes)
        return True

    def OnNormalize(self):
        """手动整理清单：把链接、乱序格式统一成「clusterId | 商品名」。"""
        if self.SaveWatchlist():
            self.progress_label.setText("清单已按规范格式整理" + self.last_note)

    def OnAdd(self):
        """添加商品：弹窗输入 ID/链接（一行一个），追加到 watchlist.txt 末尾并立即显示为新行。"""
        if self.IsPolling():
            return
        dialog = AddDialog(self.dark, self)
        if dialog.exec_() != QDialog.Accepted:
            return

        entries, seen, invalid = [], set(), 0
        for line in dialog.text().splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue  # 空行 / 注释行
            cluster_id, name = links.parse_line(line)
            if cluster_id is None:
                invalid += 1
                continue
            if cluster_id in seen:  # 同一批里重复的只留第一条
                continue
            seen.add(cluster_id)
            entries.append(links.LinkEntry(cluster_id, name, line.strip()))

        if not entries:
            QMessageBox.warning(
                self, "没有可添加的商品",
                "没识别到商品 ID 或链接。\n"
                "支持纯数字 ID，以及含 clusterId=… 的分享链接。",
            )
            return

        existing = {item["entry"].cluster_id for item in self.rows}
        new_entries = [e for e in entries if e.cluster_id not in existing]
        if not new_entries:
            self.progress_label.setText(
                f"没有新商品（输入的 {len(entries)} 条已全部在清单中）"
            )
            return

        self.watch_entries.extend(new_entries)
        for entry in new_entries:
            self.rows.append(self.MakeRow(entry))
        self.store.sync([(e.cluster_id, e.name) for e in self.watch_entries])
        self.RenderTable()
        self.SaveWatchlist()
        self.progress_label.setText(
            f"已添加 {len(new_entries)} 件商品到 watchlist，待抓取价格"
            + _skipped_note(len(entries) - len(new_entries), invalid)
            + self.last_note
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
            self.progress_label.setText(
                f"已删除 {len(cluster_ids)} 件商品" + self.last_note
            )

    def OnRefreshList(self):
        """重新读取 watchlist.txt：新增的补成新行，删掉的移除，已抓数据保留。"""
        if self.IsPolling():
            return
        if not self.LoadWatchlist(self.watchlist_path):
            return
        self.progress_label.setText(
            f"清单已同步，共 {len(self.rows)} 件商品" + self.last_note
        )

    def SelectItems(self, cluster_ids):
        """把选中态落到这几件商品此刻所在的行上。

        拖拽重排后必须重新落一次：Qt 的选中态只记得行号，重排后老行号上已经
        是别的商品了，不搬一下的话选中的仍是"那一行"，而不是"那件商品"。
        """
        self.table.clearSelection()
        if not cluster_ids:
            return
        last_col = self.table.columnCount() - 1
        for row, item in enumerate(self.rows):
            if item["entry"].cluster_id in cluster_ids:
                # 逐行 setRangeSelected：它不像 selectRow 那样顶掉上一次的选择
                self.table.setRangeSelected(
                    QTableWidgetSelectionRange(row, 0, row, last_col), True
                )

    def SelectedClusterIds(self):
        """当前选中的商品（按 clusterId 记，重画表格后行号会变）。"""
        return {
            self.rows[index.row()]["entry"].cluster_id
            for index in self.table.selectedIndexes()
            if index.row() < len(self.rows)
        }

    def OnRowsDropped(self, rows, target):
        """拖拽排序：按拖拽结果重排行模型，顺序即清单顺序，直接写回文件。

        清单本身就是顺序的载体（watchlist.txt 的行序 = 表格的行序），
        所以这里同步重排两张表：行模型和清单条目。
        """
        if self.IsPolling():
            return  # 抓取中不许重排，理由见 SetBusy
        before = [item["entry"].cluster_id for item in self.rows]
        # 先记下被挪的是哪几件商品：重排后行号全变了，只有 clusterId 还认得出人
        moved = {self.rows[i]["entry"].cluster_id for i in picked_rows(rows, len(self.rows))}

        self.rows = move_rows(self.rows, rows, target)
        if [item["entry"].cluster_id for item in self.rows] == before:
            return  # 原地放下（或又拖回了原位）：不重画也不写文件

        # 清单条目与行模型一一对应，直接从行模型里取，省得再对齐一次行号
        self.watch_entries = [item["entry"] for item in self.rows]
        self.RenderTable()
        self.SelectItems(moved)
        if self.SaveWatchlist():
            self.progress_label.setText(
                f"已调整顺序，共 {len(self.rows)} 件商品，已写回 watchlist.txt"
                + self.last_note
            )

    # ---------------- 抓取 ----------------

    def OnFetchPrices(self):
        """抓取已有商品的价格；新商品连名称、缩略图一起抓。"""
        if self.IsPolling():
            return  # 轮询中不允许重复启动
        tasks = [(i, item["entry"]) for i, item in enumerate(self.rows)]
        if not tasks:
            QMessageBox.information(self, "提示", "watchlist 中没有商品链接。")
            return
        self.last_note = ""  # 开始新的一轮抓取，不带着之前的降级提示
        self.names_learned = False
        self.poller_stopped = False
        self.run_changes = []  # 上一轮的变动和失败数不带到这一轮的总结里
        self.run_failures = 0
        self.ClearPrices()  # 先把价格清空，好一眼看出刷到哪一行了
        self.SetBusy(True)
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
        item["price_cleared"] = False  # 这一条有结果了（成没成另说），不再算「待抓取」

        if result["ok"]:
            # 涨跌要拿"这次抓取之前"的那个价比，所以先算再写缓存——upsert 之后
            # item["record"] 就成了新值，再比只能比出 0 来
            previous = item["record"] or {}
            item["delta"] = price_delta(
                previous.get("price_text"),
                previous.get("sold_out"),
                result["price"],
                result["sold_out"],
            )
            change = price_change(previous, result)
            if change is not None:
                # 名字取这次抓到的（学不到就退回清单里的名字），总结里好认人
                change["name"] = result["name"] or item["entry"].name or cluster_id
                self.run_changes.append(change)
            # 第一次抓到的新商品写入缓存；已缓存的也顺手刷新名称、缩略图和价格
            self.store.upsert_item(
                cluster_id,
                result["name"],
                result["image_url"],
                price_text=result["price"],
                reference_price=result["reference_price"],
                avg_text=result["avg_price"],
                sold_out=result["sold_out"],
            )
            item["record"] = self.store.get_item(cluster_id)
            if result["name"] and result["name"] != item["entry"].name:
                self.names_learned = True
        else:
            # 这次没抓到，还显示缓存里的价格，涨跌自然也就无从谈起；
            # 但得记一笔，总结里"没有变动"才不至于连失败一起含糊过去
            item["delta"] = None
            self.run_failures += 1
        item["values"] = result  # 失败的也记下来，重建表格时不会退回"待抓取"

        self.SetPriceCells(row, item)
        self.SetDealCells(row, result["deals"])

        # 名称：接口返回的才是最新的；失败时如果原本没有名字，标出来而不是留个"…"
        name_item = self.table.item(row, COL_NAME)
        if result["ok"] and result["name"]:
            name_item.setText(result["name"])
            name_item.setToolTip(result["name"])  # 跟着新名字走
            item["entry"].name = result["name"]
        elif not result["ok"]:
            name_item.setToolTip(result["error"] or "未知错误")
            if name_item.text() == PENDING_TEXT:
                name_item.setText(FAILED_TEXT)
        if result["ok"] and result["image_url"]:
            img_item = self.table.item(row, COL_IMG)
            if img_item is not None and img_item.icon().isNull():
                self.SetThumbnail(row, result["image_url"])

    def OnImageFetched(self, row, pixmap):
        if row >= self.table.rowCount():
            return
        # CDN 已按 1:1 裁剪，这里兜底缩放保证不超出单元格
        pixmap = pixmap.scaled(
            IMAGE_SIZE, IMAGE_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        image_url = self.row_images.get(row)
        if image_url:
            self.icons[image_url] = pixmap  # 存下来，下次重画表格就不必再下这一张
        self.PutThumbnail(row, pixmap)

    def OnTogglePause(self):
        """暂停 / 继续本次抓取。"""
        if not self.IsPolling():
            return
        if self.poller.is_paused():
            self.poller.resume()
            self.progress_label.setText("继续抓取…")
        else:
            self.poller.pause()
            self.progress_label.setText("已暂停（当前这条请求返回后生效）")
        # 文案跟着状态走，用户一眼能看出现在点它是「暂停」还是「继续」
        self.btn_pause.setText(RESUME_TEXT if self.poller.is_paused() else PAUSE_TEXT)

    def OnStopFetch(self):
        """中止本次抓取：已经抓到的结果留在表格和缓存里，不写回清单。"""
        if not self.IsPolling():
            return
        self.poller_stopped = True
        self.poller.stop()
        self.btn_pause.setEnabled(False)  # 已停止，暂停没意义了
        self.btn_stop.setEnabled(False)
        self.progress_label.setText("正在停止…")

    def OnPollFinished(self):
        self.SetBusy(False)
        self.RestorePendingPrices()  # 没轮到的行别一直空着（停止抓取的也一样）
        if self.poller_stopped:
            self.progress_label.setText("已停止抓取，已抓到的结果保留")
            return
        if self.names_learned:
            # 抓到了新名称，顺手把清单刷新一次，方便用户直接在文件里管理
            if self.SaveWatchlist():
                self.progress_label.setText(
                    "完成，已把商品名写回 watchlist.txt" + self.last_note
                )
            self.ShowSummary()
            return
        self.progress_label.setText("完成" + self.last_note)
        self.ShowSummary()

    def ShowSummary(self):
        """弹总结窗口，说清这一轮的涨跌（见 SummaryDialog）。

        只在正常跑完时弹：中途「停止抓取」那一轮只抓了一半，拿半份数据当
        "总结"很容易让人以为其余商品没变化——那种情况该看的是表格里的「--」。
        """
        if self.summary_dialog is not None:
            self.summary_dialog.close()
            # 上一份还没关就换掉，顺手回收；留着引用会越堆越多
            self.summary_dialog.deleteLater()
        self.summary_dialog = SummaryDialog(
            self.run_changes, len(self.rows), self.run_failures, self.dark, self
        )
        self.summary_dialog.show()

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
        app = QApplication.instance()
        theme.apply_theme(app, dark)
        # 悬停提示的延时是全局样式提示（样式表管不到），跟主题一起装。
        # 表格里格子密，默认 0.7 秒太容易顺着光标一路弹出来。
        theme.install_tooltip_delay(app)
        self.btn_theme.setText("浅色模式" if dark else "暗色模式")
        self.btn_theme.setToolTip(
            "当前是暗色主题，点击切换为浅色" if dark else "当前是浅色主题，点击切换为暗色"
        )

    def OnToggleTheme(self):
        dark = not self.dark
        self.store.set_setting("theme", "dark" if dark else "light")
        self.ApplyTheme(dark)
        # 单元格里的颜色（原价的弱化色、涨跌的红绿）都是按主题选的，换主题得
        # 重画一遍才换得掉；选中态按商品搬回去，免得切个主题就把选中的行丢了
        selected = self.SelectedClusterIds()
        self.RenderTable()
        self.SelectItems(selected)
        # 总结窗口还没关的话，涨跌那几格的颜色也得跟着换（它不在表格的重画范围内）
        if self.summary_dialog is not None and self.summary_dialog.isVisible():
            self.summary_dialog.SetDark(self.dark)

    # ---------------- 收尾 ----------------

    def ReportStartupNotes(self):
        """把启动阶段的问题（缓存重建、配置被忽略）挂到状态栏上，并打印留档。"""
        for warning in self.config_warnings:
            print(f"[config] {warning}")
        for warning in self.store_warnings:
            print(f"[cache] {warning}")
        if self.store_warnings:
            self.progress_label.setText(
                self.progress_label.text() + "；" + "；".join(self.store_warnings)
            )

    def closeEvent(self, event):
        """退出前把缓存连接关掉：SQLite 的写入要落盘，文件句柄也别留着。"""
        self.store.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    # 程序启动时：读取清单、同步缓存、显示已有数据；抓取由「开始抓取」按钮触发
    default_path = links.default_watchlist_path()
    if os.path.exists(default_path):
        window.LoadWatchlist(default_path)
    else:
        window.progress_label.setText("未找到 data/watchlist.txt")

    # 启动阶段的降级提示也得让人看见：缓存坏了重建、清单被跳过几行之类
    window.ReportStartupNotes()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
