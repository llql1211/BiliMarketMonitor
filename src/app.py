"""市集商品价格监视器 · PyQt5 主窗口 + 轮询调度。

启动流程：
1. 读取 watchlist.txt，并让缓存库严格跟随清单（缺的补、多的删）；
2. 把能显示的数据（名称、缩略图、clusterID）摆上表格；
3. 点「开始抓取」（或按 F5，两者等价）时逐个抓取价格，新商品连名称、缩略图一起抓；
   抓取过程中可以「暂停抓取」（两条商品之间生效）或「停止抓取」（已抓到的保留）。
   也可以在 config.toml 里开自动抓取：按固定间隔自己跑，范围可限到收藏或售罄的商品。

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
from PyQt5.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QCursor,
    QDrag,
    QFontMetrics,
    QIcon,
    QKeySequence,
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
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QShortcut,
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

# 双击缩略图看的大图：原图是 1280x1280、动辄 1MB 出头，720w 有 60 多 KB，
# 铺满默认大小的预览窗口（约 860）正合适，再往上放大才见糊
PREVIEW_SIZE = 720
PREVIEW_SUFFIX = f"@{PREVIEW_SIZE}w_{PREVIEW_SIZE}h_85q.webp"
PREVIEW_LOADING_TEXT = "正在加载大图…"
PREVIEW_FAILED_TEXT = "大图没下下来，稍后再双击试试"
NO_THUMBNAIL_TEXT = "这一行还没有缩略图，先抓取一次再双击查看"

# 预览窗口默认开多大：尽量摊开看，但不超出屏幕，也不小于一个能看的尺寸
PREVIEW_DEFAULT_SIDE = 860
PREVIEW_MIN_SIDE = 320
PREVIEW_BUTTON_ROW = 40  # 底下「关闭」那一行占的高度，从边长里留给它

ZOOM_STEP = 1.25  # 滚轮每滚一格的放大倍数
ZOOM_MAX = 4.0    # 最多放到铺满的 4 倍；720 的图再往上就是马赛克了

ROW_NUMBER_PADDING = 16  # 序号槽左右留白，免得数字贴着分隔线

COL_FAVORITE = 0
COL_IMG, COL_NAME, COL_CID, COL_PRICE, COL_LOW, COL_EXPECT, COL_REF, COL_AVG = (
    1, 2, 3, 4, 5, 6, 7, 8
)
COL_DEAL_BASE = 9  # 成交① 占 9/10/11 三列
COL_LINK = 12
HEADERS = ["收藏", "图片", "商品名", "ID", "现价", "史低价", "预期价", "原价",
           "近30天均价", "成交1", "成交2", "成交3", "链接"]

PENDING_TEXT = "…"      # 等待抓取
NO_DATA_TEXT = "—"      # 无数据
CLEARED_TEXT = "--"     # 抓取开始前把价格清成这个，好看出刷到哪一行了
FAILED_TEXT = "（查询失败）"
SOLD_OUT_TEXT = "已售罄"  # 现价格的前缀：售罄时那一格装的不是市集现价

# 「预期价格」列（用户自己设的参照价，存在 watchlist.txt 里）
EXPECTED_DIALOG_TITLE = "设置预期价格"
EXPECTED_DIALOG_LABEL = "只填数字就行，显示时前面会自动加上「￥」；\n" \
                        "现价低于它时这一格会变色，留空表示不设。"
EXPECTED_HEADER_TIP = (
    "自己设的目标价：现价低于它时这一格会变绿加粗\n"
    "双击任意一行的这一格即可设置或修改，留空表示不设\n"
    "输入框里只填数字，表格里显示时会自动加上「￥」\n"
    "存在 watchlist.txt 里，跟着清单一起备份"
)
SET_EXPECTED_TIP = "双击设置预期价格"
EDIT_EXPECTED_TIP = "双击修改预期价格"

# 「近30天均价」列
AVG_HEADER_TIP = (
    "接口给的近 30 天成交均价\n"
    "现价比它低时，这一格会变绿加粗——说明这会儿买比最近一个月都划算"
)

# 「收藏」列（用户自己打的标记，存在 watchlist.txt 里）
FAVORITE_ON_TEXT = "★"
FAVORITE_OFF_TEXT = "☆"
FAVORITE_ON_TIP = "点击取消收藏"
FAVORITE_OFF_TIP = "点击加入收藏"
FAVORITE_HEADER_TIP = (
    "自己打的收藏标记：单击这一格切换\n"
    "收藏的商品是实心黄星（☆ -> ★），没收藏的是空心灰星\n"
    "存在 watchlist.txt 里（行尾那段 | *），跟着清单一起备份，抓取动不了它"
)

# 自动抓取（配置项见 config.DEFAULTS 的 auto_poll_* 四项）
AUTO_SCOPE_LABELS = {"all": "全部", "favorite": "收藏", "sold_out": "售罄"}
AUTO_POLL_OFF_TEXT = "自动：关"
AUTO_POLL_LABEL_TIP = (
    "自动抓取的状态，由 config.toml 里的 auto_poll_* 四项决定\n"
    "间隔从上一轮结束算起，所以不会因为某轮抓得慢就把下一轮叠上来\n"
    "关掉时显示「自动：关」，改完配置要重启程序"
)
AUTO_POLL_EMPTY_TEXT = "自动抓取：本次范围内没有商品，已跳过"

# 「史低价」列
LOWEST_HEADER_TIP = (
    "历次抓到的现价里最低的那个\n"
    "每轮抓到更低的价就刷新，价格涨回去也不会把这个记录改高\n"
    "售罄时那一格装的是原价，不参与比较\n"
    "现价正落在史低价上（没比它贵）时，这一格会变绿加粗"
)

EXPECTED_BAD_INPUT = "预期价格得是大于 0 的数字，比如 50。"
EXPECTED_BAD_TITLE = "预期价格认不出来"

# 展示时统一补的货币符号；清单里存的是数字本身（见 expected_number）
EXPECTED_PREFIX = "￥"
# 手写进清单的货币符号：展示前先脱掉，免得叠成「￥￥50」
_CURRENCY_CHARS = "¥￥$€"

DELTA_ROLE = Qt.UserRole + 1        # 「现价」格末尾那截涨跌（形如「↓6」）
DELTA_COLOR_ROLE = Qt.UserRole + 2  # 上面那截的颜色（随主题走，重画时重算）
DELTA_GAP = " "                     # 现价与涨跌之间空一格

STOCK_ROLE = Qt.UserRole + 3        # 涨跌后面那截剩余件数（形如「 · 3件」，间隔号带在它身上）
STOCK_COLOR_ROLE = Qt.UserRole + 4  # 上面那截的颜色（弱化色，随主题走）

# 「现价」列一律左对齐：涨跌和剩余件数两截的落点都是从文本区左边算起的（见 segment_rect）
PRICE_ALIGN = Qt.AlignLeft | Qt.AlignVCenter

PAUSE_TEXT = "暂停抓取"
RESUME_TEXT = "继续抓取"
# 顶部那行「上次更新时间」：一次都没抓到过的时候显示这个（有数据时见 _cached_note）
NO_UPDATE_TEXT = "尚未抓取过"
LAST_UPDATE_PREFIX = "上次更新时间："
# 「开始抓取」的快捷键：按钮提示里的字和实际键位都从这一个常量来，改一处就够
FETCH_SHORTCUT = "F5"
ADD_PLACEHOLDER = "粘贴商品 ID 或分享链接，一行一个…"

# 一轮抓取跑完后的总结窗口（见 SummaryDialog）
SUMMARY_TITLE = "本轮抓取总结"
NO_CHANGE_TEXT = "本次没有变化"
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

# 总结窗口里那三块明细的小标题（见 summary_sections）
CHANGES_HEADING = "价格变动"
DEALS_HEADING = "新增成交"
TARGET_HEADING = "低于预期价格"
SECTION_SEPARATOR = "<br><br>"  # 块与块之间的空行：QTextEdit 的 CSS 边距靠不住
TARGET_PRICE_TIP = "已低于预期价"

# 判「同一条成交」时给年龄区间留的容限（秒）：挡两轮之间的时钟抖动，
# 以及「区间边界上差一秒」这种取整噪声（见 new_deals）
DEALS_MATCH_GRACE_SECONDS = 60


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
        return f"↑{_amount(change)}", change
    if change < 0:
        return f"↓{_amount(change)}", change
    return None


def updated_lowest(lowest, price, sold_out):
    """这次抓到的价格要不要刷新史低价；要就给这个价格（原样存），不要给 None。

    None 表示"这次不动它"——store.upsert_item 那边按 COALESCE 语义保留旧值，所以
    下面这几种情况都从这里退化成"不更新"，不必让存储层再判一遍：

    - 售罄的不参与：那格的"现价"其实是原价（见 PriceText），记进去就是一条假史低；
    - 这次的价格认不出数字就不比：宁可不动，也不要拿猜出来的数当史低；
    - 不比现存的更低就不写：史低价是一条只降不升的记录，涨回去当然不能改高，
      持平也没必要重写一遍。

    现存的值认不出数字（老缓存、手改过的库）时按"没有记录"处理，让这次的价格顶上
    ——至少它是个认得出的数字，比留一条读不出来的记录强。
    """
    if sold_out:
        return None
    current = parser.price_number(price)
    if current is None:
        return None
    stored = parser.price_number(lowest)
    if stored is not None and current >= stored:
        return None
    return price


def expected_reached(expected_price, price, sold_out) -> bool:
    """现价是不是已经跌到预期价以下了（到价）。

    判"没得比"的口径与 price_delta 一致：
    - 售罄行不比：那格的"现价"装的其实是原价（见 PriceText），拿它跟预期价
      比出来的"到价"没有意义；
    - 有一边认不出数字就不比：宁可这格不变色，也不要拿猜出来的数骗人。
    """
    if sold_out:
        return False
    target = parser.price_number(expected_price)
    current = parser.price_number(price)
    if target is None or current is None:
        return False
    return current < target


def below_average(price, avg_price, sold_out) -> bool:
    """现价是不是比近 30 天均价还低（这会儿买比最近一个月都划算）。

    判"没得比"的口径与 expected_reached 一致，理由也一样：
    - 售罄行不比：那格的"现价"装的其实是原价（见 PriceText），拿它跟均价
      比出来的"划算"没有意义；
    - 有一边认不出数字就不比：宁可这格不变色，也不要拿猜出来的数骗人。

    严格低于才算：持平说明就是均价那个水平，"比最近一个月划算"这话不成立。
    """
    if sold_out:
        return False
    average = parser.price_number(avg_price)
    current = parser.price_number(price)
    if average is None or current is None:
        return False
    return current < average


def at_lowest(price, lowest, sold_out) -> bool:
    """现价是不是正落在史低价这一档上（史上最便宜的时候，值得扫一眼）。

    判"没得比"的口径与 expected_reached / below_average 一致：
    - 售罄行不比：那格的"现价"装的其实是原价（见 PriceText）；
    - 有一边认不出数字就不比：宁可这格不变色，也不要拿猜出来的数骗人。

    不高于史低价就算命中。持平是常态——史低价本来就是历次抓到的现价里最低的那个，
    这一档多半是刚抓到的这个价自己刷出来的；真出现"比史低价还低"只可能是缓存被
    手改过，一起认下来，别为它单开一个分支。
    """
    if sold_out:
        return False
    low = parser.price_number(lowest)
    current = parser.price_number(price)
    if low is None or current is None:
        return False
    return current <= low


def expected_number(expected_price) -> str:
    """预期价里"算数"的那截：脱掉首尾空白和手写的货币符号。

    输入框回填用：用户双击时看到的就是存进清单的那个数字，框里只有一个数，
    不用先删掉一个「￥」再打字。
    """
    text = str(expected_price).strip() if expected_price else ""
    return text.lstrip(_CURRENCY_CHARS).strip() if text else ""


def expected_display(expected_price) -> str:
    """「预期价格」格的展示文本：数字前统一加上「￥」；没设给空串。

    清单里存的是数字，符号只在展示时补——存符号的话，输入框里就得先删掉它，
    还得靠"符号是不是重复了"来判断有没有存错。老清单和手写行里的「¥50」也照常
    认：先把已有的符号脱掉再加，不会叠成「￥￥50」。
    """
    number = expected_number(expected_price)
    return f"{EXPECTED_PREFIX}{number}" if number else ""


def price_change(previous, result):
    """本次抓到的结果相比缓存记录的一条变动；没变动（或没得比）就是 None。

    previous 是抓取前的缓存记录（可能为空），result 是本次解析成功的结果。
    返回的 dict 供总结窗口用：
      kind       up / down / sold_out / on_sale 四选一
      old_price  上一次的现价原文
      new_price  这一次的现价原文
      change     涨跌数字（只有 up/down 有，其余为 None）
      delta      形如「↓6」的显示文本（同上）

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


def split_price_text(text, delta, stock=""):
    """把「¥44 ↓6 · 2件」拆成现价 / 涨跌 / 剩余件数三截（现价那截留着分隔的空格）。

    涨跌和剩余件数永远照这个顺序拼在末尾，从右往左按长度切比找分隔符稳——价格
    前面还顶着「已售罄」，里面也没准带空格。件数那截自带「 · 」，切掉它不会在末尾
    留下孤儿空格，所以比涨跌时不必再 trim 一道。

    留在现价那截末尾的空格是「现价与涨跌之间」的那一个：它得留着，量出来的间距
    才跟拼文本时一致（见 segment_rect）。

    两截各认各的：对不上就返回空串，那段文本照旧留在现价那截里。宁可让它在现价
    的位置按现价的颜色原样显示出来，也不要当成另一截、挪到别处去画。
    """
    if stock and text.endswith(stock):
        text = text[: len(text) - len(stock)]
    else:
        stock = ""
    if delta and text.endswith(delta):
        text = text[: len(text) - len(delta)]
    else:
        delta = ""
    return text, delta, stock


def segment_rect(metrics, text_rect, left, text):
    """补画的那截该画在哪儿：从 left 起、跟前面几截同一行；挤不下就返回 None。

    位置按「基类给文字留的矩形 + 前面几截量出来的宽度」算——基类画现价时用的也是
    同一块矩形，所以几截能接上。left 里已经含着分隔的空格，量出来的间距就跟
    单元格文本里的一致。
    """
    # QRect.right() 是闭区间，算宽度时要补回这 1 像素，不然右边会缺一条
    if left + metrics.horizontalAdvance(text) > text_rect.right() + 1:
        return None
    return QRect(left, text_rect.top(), text_rect.right() + 1 - left, text_rect.height())


def stock_text(count) -> str:
    """「现价」格末尾那截剩余件数（形如「 · 3件」）；没有件数就给空串。

    间隔号和它两边的空格都长在这一截里，是因为这几截是各画各的（见
    PriceDeltaDelegate）：夹在涨跌和件数中间的那段空隙没人画，把它写成一截之外的
    分隔符就等于没画。间隔号用「 · 」，跟成交列（见 deal_text）同一个写法。

    完整的说法留在悬停提示里（见 StockTooltip）。

    件数由解析层归一（见 parser._stock_count）：接口只在货少时才把件数写进按钮
    文案，货还够（「当前最低价」）和已售罄（「已售罄」）都没数字可报，到这里就是
    None——格子末尾也就不多这一截。
    """
    return f" · {count}件" if count else ""


def _parse_stamp(value):
    """缓存里的 ISO 时间串 → datetime；认不出来返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_stamp(stamp) -> str:
    """datetime →「2026-09-16 10:30:00」。跟缓存里存的格式差在中间那个 T 上。"""
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _display_time(value) -> str:
    """缓存里的 ISO 时间串 →「2026-09-16 10:30:00」；认不出来就原样返回。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    stamp = _parse_stamp(value)
    return _format_stamp(stamp) if stamp else value


def _cached_note(updated_at) -> str:
    """「这价格是什么时候抓的」的提示语；没有时间戳就什么都不说。

    宁可少一句提示，也不要拿当前时间凑一个，那是在骗人。
    """
    stamp = _display_time(updated_at)
    return f"{LAST_UPDATE_PREFIX}{stamp}" if stamp else ""


def _elapsed_since(anchor):
    """缓存里的时刻串 → 到现在过了多少秒；认不出来返回 None。

    跟 MainWindow.DealsElapsed 的区别就在认不出来的时候：那边按 0 算（判"有没有
    新成交"时退化成"年龄区间一格没挪"，宁可多报一条也不漏报），这边返回 None，
    调用处据此认定"这组数据是什么时候抓的都不知道"，该闭嘴的地方就闭嘴
    （见 _reaged_deals）。
    """
    stamp = _parse_stamp(anchor)
    if stamp is None:
        return None
    return max(0.0, (datetime.now() - stamp).total_seconds())


def _reaged_deals(deals, elapsed):
    """缓存里那组成交，按「抓到它到现在又过了多久」重写成现在的说法。

    成交没有时间戳，缓存里存的是抓取那一刻接口给的人话（「1分钟前」）。原样搬到
    首屏就是在骗人：三天前抓的那条，到今天还写着「1分钟前」。抓取时刻到现在这段
    加上去，才是它此刻的年龄下界（见 parser.format_age）。

    两种认不出的情况分开处理，都往"少说一句"那边倒：
    - 认不出这条成交的年龄（绝对日期、错别字）→ 原文照旧，也不高亮。原文可能是
      个日期，照抄不算撒谎；
    - 认不出这组是什么时候抓的 → 相对说法一律不能留，只留价格。价格是抓到的值，
      它不会过期，时间会。
    """
    aged = []
    for deal in deals or []:
        if not isinstance(deal, dict) or not deal.get("price"):
            continue
        if elapsed is None:  # 不知道这组多老 → 相对说法留不得
            aged.append({"price": deal["price"], "time": "", "age_seconds": None})
            continue
        age = parser.parse_relative_time(deal.get("time"))
        if age is None:  # 认不出年龄 → 原文照旧，也不高亮
            text = deal.get("time") or ""
            aged.append({"price": deal["price"], "time": text, "age_seconds": None})
            continue
        age = int(age + elapsed)
        aged.append(
            {"price": deal["price"], "time": parser.format_age(age), "age_seconds": age}
        )
    return aged


def _tips(*parts) -> str:
    """拼提示文本：只留非空的那几段，一行一句。"""
    return "\n".join(part for part in parts if part)


def _esc(value) -> str:
    """转义要放进 HTML 的文本（商品名是接口给的，什么都可能有）。"""
    return html.escape("" if value is None else str(value))


def _dicts(items) -> list:
    """从传进来的东西里挑出成形的 dict：渲染路径上多一层保险，别让脏数据卡住重画。"""
    return [item for item in items or [] if isinstance(item, dict)]


def new_deals(deals, previous, elapsed, grace=DEALS_MATCH_GRACE_SECONDS) -> list:
    """本次抓到的成交里，上一次没出现过的那几条（见 SummaryDialog）。

    比的是「价格 + 年龄」，不比时间原文：原文是人话（「9小时前」），只给到
    精度有限的区间，同一条成交下一轮就渲染成「10小时前」了，拿它对字符串会把
    老成交一次次报成新的。所以判据是——价格完全相同，且这条现在所在的年龄格子，
    跟「老记录搁了 elapsed 秒之后该处的年龄区间」有重叠（见 _aged_from）。

    两轮的间隔由调用方按各自记录的抓取时刻算好递进来（见 OnResultReady）：
    缓存里的成交带着抓取时刻，重启后也能接着比。

    没有基准可比时（还没抓到过这件商品、或缓存里没存成交）一条都不报——
    把三条全说成「新增」是假的。宁可漏报也不误报：粗粒度下确实有分不清的时候
    （老记录「10分钟前」、两轮只隔 1 分钟，而新记录是「5分钟前」），这种按老的处理。
    """
    old = _dicts(previous)
    if not old:
        return []  # 没有基准就一条都不报，由这里挡住，别指望调用方记得判
    fresh = []
    claimed = set()  # 已经被认领走的老记录不再参与后面的比对
    for deal in _dicts(deals):
        index = _aged_from(old, deal, elapsed, claimed, grace)
        if index is None:
            fresh.append(deal)
        else:
            claimed.add(index)
    return fresh


def _aged_from(old_deals, deal, elapsed, claimed, grace):
    """在还没被认领的老记录里，找 deal 是「变老 elapsed 秒」的那一条，返回下标。

    新成交一定把老记录往下挤（列表按时间倒序、只留几条），所以「这条是老的」
    等价于「在没被认领过的老记录里找得到它」，与它现在排第几无关。

    找不到返回 None，调用方按新增成交处理。
    """
    price = str(deal.get("price") or "")
    bounds = parser.age_bounds(deal.get("time"))
    for index, candidate in enumerate(old_deals):
        if index in claimed:
            continue
        if str(candidate.get("price") or "") != price:
            continue  # 价格是精确值，对不上就不是同一条
        old_bounds = parser.age_bounds(candidate.get("time"))
        if bounds is None or old_bounds is None:
            # 时间认不出年龄（「刚刚」、绝对日期、错别字），退回原文相等比对
            if str(candidate.get("time") or "") == str(deal.get("time") or ""):
                return index
            continue
        # 老记录当时在「lo～hi 秒前」这格里，过去 elapsed 秒之后该在「lo+elapsed～
        # hi+elapsed」这段里；这条现在所在的是「lo'～hi'」这格。两段有重叠才可能是
        # 同一条（两端再各留一点容限）。
        #
        # 判「这条的下界落没落在移位后的区间里」是不行的：真实数据里格子很宽
        # （「5小时前」整整一小时宽），隔几分钟再抓一次，原文一个字都不会变，而它的
        # 下界"该"往右挪了那几分钟，一判就判成新的——一条老成交会被一轮轮报上来。
        # 重叠才是必要条件：这条当时的真实年龄既在该处的区间里，也在现在这格里。
        lower = old_bounds[0] + elapsed - grace
        upper = old_bounds[1] + elapsed + grace
        if lower <= bounds[1] and bounds[0] <= upper:
            return index
    return None


def summary_headline(changes, new_deals_by_item, targets) -> str:
    """总结窗口的头一句话：三块各报几项、合计多少；什么都没发生就直说。

    计数按"商品件数"算而不是按条数：一件商品新增两条成交，报的仍是
    「新增成交 1」——下面明细里它本来就占一行。价格变动那块的条数则
    照旧逐条数（与 CHANGES_HEADING 那块明细的行数一致），分类认不出来时宁可照常列出来，
    也不能让这行汇总跟明细对不上。
    """
    changes = _dicts(changes)
    deals = _dicts(new_deals_by_item)
    targets = _dicts(targets)
    if not (changes or deals or targets):
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
    if deals:
        parts.append(f"{DEALS_HEADING} {len(deals)}")
    if targets:
        parts.append(f"{TARGET_HEADING} {len(targets)}")
    total = len(changes) + len(deals) + len(targets)
    return f"本次 {total} 项：" + " · ".join(parts)


def format_duration(seconds) -> str:
    """把秒数写成人话：不到一分钟给「21 秒」，再往上给「1 分 36 秒」「2 小时」。

    精确到秒就够了——这个数是给人判"抓这点东西花这么久合不合理"的，不是拿来
    对账的，再细也没人看。下限取 1 秒：真跑起来总不止一秒，取整成「0 秒」看着
    像出了错。
    """
    total = max(1, int(round(max(0.0, seconds))))
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        # 整分钟不写「1 分 0 秒」，那个 0 秒没有信息量
        return f"{minutes} 分 {secs} 秒" if secs else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def summary_subtitle(total, failures, elapsed) -> str:
    """总结窗口的第二行：这一轮抓了多少件、花了多久、几件没抓到。

    查询失败的那几件这次没有可比的价格，不说明的话，"没有变化"看起来就像
    它们也没事——失败本身得在总结里有个交代。

    用时是这一轮的墙钟时间刨掉暂停的那段（见 MainWindow.RunElapsed）：暂停是
    用户自己按的，算进去会让人以为抓取本身就那么慢。
    """
    text = f"本次共抓取 {total} 件商品，用时 {format_duration(elapsed)}"
    if failures:
        text += f"，其中 {failures} 件查询失败（这几件看不出变化）"
    return text


def new_deals_html(items, dark) -> str:
    """新增成交的明细表：商品 / 成交记录。

    商品名和成交文本一律转义（都是接口给的）。同一件商品这次冒出好几条新成交
    就在一格里面排开——一件商品占一行，跟头一句话里的件数对得上。
    """
    muted = theme.muted_color(dark).name()
    rows = [
        (_esc(item.get("name")), _esc(_join(*(_deal_text(d) for d in _dicts(item.get("deals"))))))
        for item in _dicts(items)
    ]
    return _section_html(DEALS_HEADING, ("商品", "成交记录"), rows, muted)


def targets_html(items, dark) -> str:
    """低于预期价那几件商品的明细表：商品 / 现价 / 预期价。

    现价上到价色（跟表格里「预期价格」格变色用的是同一个色），一眼看出是哪几件
    跌到了用户设的价下面；预期价照旧带「￥」，跟表格里的展示一致。
    """
    muted = theme.muted_color(dark).name()
    color = theme.expected_reached_color(dark).name()
    rows = []
    for item in _dicts(items):
        price = f'<span style="color:{color}">{_esc(item.get("price"))}</span>'
        rows.append(
            (_esc(item.get("name")), price, _esc(expected_display(item.get("expected_price"))))
        )
    return _section_html(TARGET_HEADING, ("商品", "现价", "预期价"), rows, muted)


def summary_sections(changes, deals, targets, dark) -> str:
    """总结窗口正文：价格变动、新增成交、低于预期价三块，有一块写一块。

    三张表各带小标题、中间空一行隔开。没内容的那块整块省掉（标题也不留），
    所以没有明细时这里只回空串——窗口那边据此藏起这块空框。

    成交、低于预期这两块的空表会各回一个空串，拼进来正好不占位置；
    价格变动那块则由 changes_html 自己保证有条才画。
    """
    blocks = [
        changes_html(changes, dark),
        new_deals_html(deals, dark),  # 传的是 new_deals() 挑出来的那几条，不是原始成交
        targets_html(targets, dark),
    ]
    return SECTION_SEPARATOR.join(block for block in blocks if block)


def _section_html(heading, titles, rows, muted) -> str:
    """一块明细：加粗的小标题 + 一张表（几列由 titles 定）；没有内容就回空串。

    只用 QTextEdit 确定认得的标签（table / td / span）：这是给总结窗口渲染的，
    不是网页。表头用弱化色，跟下面的正文拉开层次——小标题不跟着弱化，
    它是这一块的名字，得比表头显眼。

    rows 里的格子是已经拼好的 HTML（要上色的地方各表自己带 span，文本也各自
    转义过），这里只负责摆进表格——三处明细共用同一套表格样式，改一处就够，
    不必每张表都抄一遍 cellpadding。
    """
    if not rows:
        return ""  # 空表留着一个孤零零的标题最难看，整块省掉
    cells = [f"<tr><td><b>{_esc(heading)}</b></td></tr>"]
    cells.append(
        "<tr>"
        + "".join(
            f'<td><span style="color:{muted}">{_esc(title)}</span></td>'
            for title in titles
        )
        + "</tr>"
    )
    for row in rows:
        cells.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f'<table cellspacing="0" cellpadding="6" width="100%">{"".join(cells)}</table>'


def changes_html(changes, dark) -> str:
    """变动明细的表格：商品 / 价格 / 变动，涨跌那格上色（见 _section_html）。

    表格样式走公用的 _section_html，这里只管每一格写什么。转义在这一层做完：
    商品名是接口给的，名字里出现尖括号也不能把表格拆了。
    """
    muted = theme.muted_color(dark).name()
    rows = []
    for change in _dicts(changes):
        kind = change.get("kind")
        if kind in (CHANGE_UP, CHANGE_DOWN):
            price = f'{_esc(change.get("old_price"))} → {_esc(change.get("new_price"))}'
            detail = _esc(change.get("delta"))
            color = theme.price_delta_color(change.get("change"), dark).name()
        elif kind == CHANGE_SOLD_OUT:
            price, detail, color = "已售罄", "此前现价 " + _esc(change.get("old_price")), muted
        else:  # CHANGE_ON_SALE：状态写在价格那格，价格有就跟着报一下
            price, detail, color = "恢复在售", _join("现价", _esc(change.get("new_price"))), muted
        rows.append(
            (
                _esc(change.get("name")),
                price,
                f'<span style="color:{color}">{detail}</span>',
            )
        )
    return _section_html(CHANGES_HEADING, ("商品", "价格", "变动"), rows, muted)


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
    """「现价」列的绘制：现价照常画，后面那截涨跌和剩余件数各自上色。

    一个 QTableWidgetItem 只有一种前景色，而「¥44 ↓6 · 2件」要三截颜色，所以
    这一列自己画：先让基类按平常的样子画（背景、隔行色、选中态、对齐都一样），只是
    交给它的文本先收窄成现价那半截，再按同一个文本矩形接着往后补上另两截。

    文本位置由基类说了算，这里不自己摆——列宽不够时基类会加省略号，位置对不上
    的话几截会叠在一起。
    """

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.text, _, _ = split_price_text(
            option.text, index.data(DELTA_ROLE), index.data(STOCK_ROLE)
        )

    def paint(self, painter, option, index):
        if not index.data(DELTA_ROLE) and not index.data(STOCK_ROLE):
            super().paint(painter, option, index)
            return

        super().paint(painter, option, index)  # 现价那半截（已由上面收窄）

        prefix, delta, stock = split_price_text(
            index.data(Qt.DisplayRole) or "",
            index.data(DELTA_ROLE),
            index.data(STOCK_ROLE),
        )
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        # 基类给文字留的那块地方：界面上"现价"取的就是它的左边缘
        text_rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, style_option, option.widget
        )
        metrics = QFontMetrics(style_option.font)
        left = text_rect.left() + metrics.horizontalAdvance(prefix)

        painter.save()
        painter.setFont(style_option.font)
        for text, color in (
            (delta, index.data(DELTA_COLOR_ROLE)),
            (stock, index.data(STOCK_COLOR_ROLE)),
        ):
            if not text:
                continue  # 这一截这次没有（比如价格没变就只剩件数那截）
            rect = segment_rect(metrics, text_rect, left, text)
            if rect is None:
                break  # 挤不下就收掉这截和它右边那截：叠着画比少显示一处更糟
            painter.setPen(color or style_option.palette.text().color())
            painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, text)
            left += metrics.horizontalAdvance(text)
        painter.restore()


class PreviewArea(QScrollArea):
    """看大图用的滚动区：滚轮在这儿改缩放，按住左键还能拖着挪画面。

    别的滚动区滚轮是滚内容，可看图的当口「往下滚一屏」没什么用——放大才是。
    放大到超出窗口之后，滚动条能一根一根拨，但按住图直接拖更顺手，所以拖动和
    滚动条走的是同一套滚动位置，滚动条照旧留着。

    鼠标事件接在塞进来的控件上，不接在自己身上：真机上鼠标点的是那个控件，
    它不认鼠标事件往外冒的时候中间还隔着 viewport，接在源头最稳，省得赌 Qt
    把后续的 move 送回谁手上。
    """

    wheeled = pyqtSignal(int)  # 滚轮这一格的垂直步长（往上为正）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_from = None  # 按下时的屏幕坐标，None 表示没在拖
        self._drag_bar = (0, 0)  # 按下时两根滚动条的位置

    def setWidget(self, widget):
        """接住塞进来的控件，顺手把它的鼠标事件也接过来。"""
        old = self.widget()
        if old is not None:
            old.removeEventFilter(self)
        super().setWidget(widget)
        if widget is not None:
            widget.installEventFilter(self)

    def wheelEvent(self, event):
        self.wheeled.emit(event.angleDelta().y())
        event.accept()

    def eventFilter(self, obj, event):
        """拖动挪画面。按下的那一刻定基准，之后都按屏幕位移算，别看控件坐标。

        控件自己在滚动里挪位置，拿控件坐标算位移会跟着滚动一起漂，一拖就飞。
        """
        kind = event.type()
        if kind == QEvent.MouseButtonPress:
            return self._Press(event)
        if kind == QEvent.MouseMove and self._drag_from is not None:
            return self._Drag(event)
        if kind == QEvent.MouseButtonRelease and self._drag_from is not None:
            return self._Release(event)
        return super().eventFilter(obj, event)

    def _Press(self, event):
        if event.button() != Qt.LeftButton or not self._Pannable():
            return False  # 没超出可视区就没什么可挪的，别白拦事件
        self._drag_from = event.globalPos()
        self._drag_bar = (
            self.horizontalScrollBar().value(),
            self.verticalScrollBar().value(),
        )
        self.RefreshCursor()
        event.accept()
        return True

    def _Drag(self, event):
        delta = event.globalPos() - self._drag_from
        # 画面跟着手走：往右拖，图也该往右挪，所以滚动条要往回退
        self.horizontalScrollBar().setValue(self._drag_bar[0] - delta.x())
        self.verticalScrollBar().setValue(self._drag_bar[1] - delta.y())
        event.accept()
        return True

    def _Release(self, event):
        if event.button() != Qt.LeftButton:
            return False
        self._drag_from = None
        self.RefreshCursor()
        event.accept()
        return True

    def RefreshCursor(self):
        """超出可视区时给个张开的手，明示这儿能拖；拖起来就把手攥上。"""
        if self._drag_from is not None:
            self.viewport().setCursor(Qt.ClosedHandCursor)
        elif self._Pannable():
            self.viewport().setCursor(Qt.OpenHandCursor)
        else:
            self.viewport().unsetCursor()  # 没得拖就还回箭头，别给假希望

    def _Pannable(self):
        """图比可视区大才有的拖——这时候才有内容可挪，也才该给抓手。

        比的是控件的 minimumSize 而不是滚动条的 range：range 要等滚动区排完版
        才是新值，刚放大那一档问它还是旧的，会正好漏掉第一档。
        """
        widget = self.widget()
        if widget is None:
            return False
        viewport = self.viewport().size()
        return (
            widget.minimumWidth() > viewport.width()
            or widget.minimumHeight() > viewport.height()
        )


class ImagePreviewDialog(QDialog):
    """双击缩略图弹出的大图。

    开窗不等图：大图比缩略图大几百倍，下载要一会儿，所以先把窗口摆出来，
    里面写着「正在加载大图…」，图回来了再换上（见 MainWindow.OnPreviewFetched）。
    窗口不开模态——看图的当口还想顺手点点表格，是很自然的事。

    窗口尺寸不写死：默认就开得比较大，还能接着往大拖，图始终铺满可视区。
    在这之上滚轮继续放大（以「铺满」为 1 倍，最多 ZOOM_MAX 倍），放过头了
    滚动条自己会出来，按住图拖着挪或者拨滚动条都行。
    """

    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or "商品图")
        self.pixmap = None
        self.zoom = 1.0  # 相对「铺满」的倍数，滚轮调的就是这个

        self.label = QLabel(PREVIEW_LOADING_TEXT)
        self.label.setAlignment(Qt.AlignCenter)  # 图比可视区小的时候居中，文字也是

        self.area = PreviewArea()
        self.area.setWidget(self.label)
        # 控件跟着可视区长大：图小的时候由 label 的居中把留白摊匀，
        # 图大的时候靠 label 的 minimumSize 把滚动条撑出来
        self.area.setWidgetResizable(True)
        self.area.setAlignment(Qt.AlignCenter)
        self.area.wheeled.connect(self.OnWheel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.area)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._ApplyDefaultSize()

    def _ApplyDefaultSize(self):
        """开窗尺寸：尽量摊开，但别把标题栏和关闭按钮顶到屏幕外去。"""
        side = PREVIEW_DEFAULT_SIDE
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            side = min(side, avail.width() - 80, avail.height() - 160)
        side = max(side, PREVIEW_MIN_SIDE)
        self.setMinimumSize(PREVIEW_MIN_SIDE, PREVIEW_MIN_SIDE)
        self.resize(side, side + PREVIEW_BUTTON_ROW)

    def SetImage(self, pixmap):
        """贴上大图。缩放兜个底：CDN 裁剪理论上给的就是 PREVIEW_SIZE，万一不是也别撑破窗口。"""
        self.pixmap = pixmap
        self.label.setText("")
        self._Update()

    def SetFailed(self):
        self.label.setText(PREVIEW_FAILED_TEXT)

    def OnWheel(self, delta):
        """滚轮缩放：往上滚放大，往下滚最多缩回铺满，到顶就不再涨。"""
        if self.pixmap is None or not delta:
            return
        self.zoom = min(ZOOM_MAX, max(1.0, self.zoom * ZOOM_STEP ** (delta / 120)))
        self._Update()

    def resizeEvent(self, event):
        """窗口一变大变小就把图重排一遍——铺满的基准跟着可视区走。"""
        super().resizeEvent(event)
        self._Update()

    def _Update(self):
        """按当前缩放把图摆好：1 倍正好铺满可视区，再乘上滚轮给的倍数。

        尺寸取整往下走（int 而不是 round）：铺满那一档要是反过来把图撑出可视区
        一个像素，滚动条就会自己冒出来，看着像没铺满。
        """
        if self.pixmap is None:
            return
        viewport = self.area.viewport().size()
        fit = min(
            viewport.width() / self.pixmap.width(),
            viewport.height() / self.pixmap.height(),
        )
        size = QSize(
            max(1, int(self.pixmap.width() * fit * self.zoom)),
            max(1, int(self.pixmap.height() * fit * self.zoom)),
        )
        self.label.setMinimumSize(size)
        self.label.setPixmap(
            self.pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        # 拉到正中：放大后要看的多半是中间那块，不这么做每滚一格都从左上角看起，
        # 想往中间看还得自己拖回去。图没超出可视区时 range 是 0，这一步等于没做。
        #
        # 这里读到的 maximum 有时候还是上一档的（滚动条的 range 要等滚动区排完版
        # 才是最终值），居中会差几个像素；下一档就准了，看着不碍事，不值得为它去
        # 转一圈事件循环。
        for bar in (self.area.horizontalScrollBar(), self.area.verticalScrollBar()):
            bar.setValue(bar.maximum() // 2)
        # 能不能拖跟着尺寸走：刚放大到超出可视区就该换上抓手
        self.area.RefreshCursor()


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
    """一轮抓取跑完后的总结窗口：先说这一轮有几项、抓了多少件、花了多久，
    再分块列出明细。

    明细有三块（各一张小表，见 summary_sections）：价格变动、新增成交、
    低于预期价格。

    不开模态（和看图那个窗口同理）：总结是"看一眼"的东西，读的时候顺手点点
    表格、打开个详情页都很自然，没必要把主窗口锁住。

    什么都没有时也照样弹（用户要的是每轮都有个收尾交代），此时藏掉明细那块空框。
    """

    def __init__(
        self, changes, deals, targets, total, failures, elapsed, dark, parent=None
    ):
        super().__init__(parent)
        self.changes = _dicts(changes)
        self.deals = _dicts(deals)
        self.targets = _dicts(targets)
        self.total = total
        self.failures = failures
        self.elapsed = elapsed  # 本轮实际耗时（秒，刨掉暂停），见 MainWindow.RunElapsed
        self.setWindowTitle(SUMMARY_TITLE)
        self.setMinimumWidth(360)  # 没有明细时窗口会收得很窄，别让按钮挤成一团

        layout = QVBoxLayout(self)
        self.headline = QLabel()
        font = self.headline.font()
        font.setBold(True)  # 头一句「本次 N 项」要压过下面的明细
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
        if self.HasDetail():
            self.resize(620, 460)  # 三块明细都可能有，比只有变动时高一点
        else:
            self.resize(460, 180)  # 没有明细，矮一点就够

    def HasDetail(self) -> bool:
        """有没有要列的明细（决定正文区露不露、窗口开多大）。"""
        return bool(self.changes or self.deals or self.targets)

    def SetDark(self, dark):
        """按主题重新渲染文案与配色（主窗口切换主题时再叫一次）。

        文案本来跟主题无关，但一起重渲染最省事——改动只有一处，
        不会出现"换了主题、数字还是旧的"这种对不上的情况。
        """
        self.headline.setText(summary_headline(self.changes, self.deals, self.targets))
        self.subtitle.setText(
            summary_subtitle(self.total, self.failures, self.elapsed)
        )
        self.detail.setHtml(
            summary_sections(self.changes, self.deals, self.targets, dark)
        )
        self.detail.setVisible(self.HasDetail())  # 没有明细就别摆个空框


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
        self.last_unparsed = 0   # 上次读清单时认不出的行数（见 OnRefreshList 的重写判据）
        self.run_changes = []    # 本次抓取的价格变动，跑完弹总结用（见 price_change）
        self.run_deals = []      # 本次抓到的成交里新出现的几条（见 new_deals），按商品归拢
        self.run_targets = []    # 本次现价低于预期价的商品（见 SummaryDialog）
        self.run_failures = 0    # 本次抓取查询失败的条数：失败的那几件看不出变动
        self.run_total = 0       # 本次抓取的目标件数：总结里「共抓取 N 件」用它而不是
                                 # 清单总件数——「仅抓取新添加商品」只抓其中几件
        # 本轮的计时：起点、已暂停的累计秒数、当前这次暂停的起点（见 RunElapsed）
        # 起点先落在建窗那一刻：没起过一轮就收尾（只有测试会这样）也不会崩
        self.run_started_at = time.monotonic()
        self.run_paused_seconds = 0.0
        self.run_paused_at = None
        self.run_elapsed = 0.0   # 收尾时算好存下来：总结窗口换主题重渲染时要拿它
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
        # 自动抓取：单次触发的定时器，每轮收尾时重挂（见 ScheduleAutoPoll）。
        # 不用周期定时器，是因为某一拍正撞上抓取时要额外写「跳过但别忘挂下一拍」，
        # 而单次触发天然就是「上一轮结束到下一轮开始」
        self.fetch_source = "manual"  # 这一轮是谁发起的：manual / auto
        self.auto_poll_timer = QTimer(self)
        self.auto_poll_timer.setSingleShot(True)
        self.auto_poll_timer.timeout.connect(self.OnAutoPoll)
        self.InitUI()
        self.ApplyTheme(self.InitialDark())
        self.ScheduleAutoPoll()  # 开了自动抓取就挂上第一拍；没开就什么都不做

    # ---------------- 界面 ----------------

    def InitUI(self):
        template = self.config["detail_url_template"]
        self.setWindowTitle("市集商品价格监视器")
        self.resize(1500, 900)

        central = QWidget()
        layout = QVBoxLayout(central)

        # 顶部第一行：「上次更新时间」。它说的是"这屏数据有多旧"，跟按钮不是一类东西；
        # 而且挤进按钮行会把窗口顶宽——七个按钮已经占到 1265px，默认窗口才 1500，
        # 这行字再加进去（约 290px）就溢出了。文字由 UpdateUpdatedLabel() 填。
        self.updated_label = QLabel()
        self.updated_label.setToolTip(
            "缓存里最新一次成功抓到价格的时间\n"
            "抓取失败的行不刷新它；一件都没抓到过时显示「尚未抓取过」"
        )
        # 清单都没读进来（比如没有 watchlist.txt）时也得有句话，空着比不显示还费解
        self.UpdateUpdatedLabel()
        # 自动抓取的状态跟它同一行、右对齐：也是「看一眼」的信息。本来打算搁在按钮行
        # 的主题按钮旁，但那一行加上它就超出了默认窗口宽度（见
        # test_updated_label_does_not_widen_the_window），这一行空着呢
        self.auto_poll_label = QLabel()
        self.auto_poll_label.setToolTip(AUTO_POLL_LABEL_TIP)
        self.UpdateAutoPollLabel()
        info_bar = QHBoxLayout()
        info_bar.addWidget(self.updated_label)
        info_bar.addStretch()
        info_bar.addWidget(self.auto_poll_label)
        layout.addLayout(info_bar)

        # 顶部按钮行
        btn_bar = QHBoxLayout()
        self.btn_add = QPushButton("添加…")
        self.btn_delete = QPushButton("删除选中")
        self.btn_refresh_list = QPushButton("刷新列表")
        self.btn_fetch = QPushButton("开始抓取")
        self.btn_fetch_new = QPushButton("仅抓取新添加商品")
        self.btn_pause = QPushButton(PAUSE_TEXT)
        self.btn_stop = QPushButton("停止抓取")
        for btn, slot, tip in (
            (self.btn_add, self.OnAdd,
             "输入商品 ID 或分享链接（一行一个），追加写入 watchlist.txt 末尾"),
            (self.btn_delete, self.OnDeleteSelected,
             "把选中的商品从 watchlist.txt 和缓存中删除"),
            (self.btn_refresh_list, self.OnRefreshList,
             "重新读取 watchlist.txt（手工改动后点这里同步）\n"
             "全部行都认得出时，会顺手按规范格式重写一遍"),
            (self.btn_fetch, self.OnFetchPrices,
             f"逐个抓取价格，间隔 {self.config['poll_interval_seconds']:g} 秒\n"
             f"快捷键 {FETCH_SHORTCUT}"),
            (self.btn_fetch_new, self.OnFetchNew,
             "只抓还没有价格的商品（新添加的，以及一直没抓成功的），\n"
             "其他行的价格不动"),
            (self.btn_pause, self.OnTogglePause,
             "暂停 / 继续本次抓取（在两条商品之间生效，不打断正在进行的请求）"),
            (self.btn_stop, self.OnStopFetch,
             "中止本次抓取，已经抓到的结果会保留在表格和缓存里"),
        ):
            btn.setToolTip(tip)
            btn_bar.addWidget(btn)
        self.btn_add.clicked.connect(self.OnAdd)
        self.btn_delete.clicked.connect(self.OnDeleteSelected)
        self.btn_refresh_list.clicked.connect(self.OnRefreshList)
        self.btn_fetch.clicked.connect(self.OnFetchPrices)
        self.btn_fetch_new.clicked.connect(self.OnFetchNew)
        self.btn_pause.clicked.connect(self.OnTogglePause)
        self.btn_stop.clicked.connect(self.OnStopFetch)

        # F5 = 开始抓取。挂成窗口级快捷键，而不是给按钮 setShortcut：光标多半在
        # 表格里，按键先到的是表格，按钮收不到。
        self.shortcut_fetch = QShortcut(QKeySequence(FETCH_SHORTCUT), self)
        self.shortcut_fetch.setContext(Qt.WindowShortcut)
        self.shortcut_fetch.activated.connect(self.OnFetchPrices)

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
        # 收藏那列只放一颗星：30 够「★」加左右留白了，宽了也是白占商品名的地
        header.resizeSection(COL_FAVORITE, 30)
        header.resizeSection(COL_IMG, IMAGE_SIZE + 8)
        # 要放得下「¥6.56 ↓0.73 · 1件」这种：现价、涨跌、剩余件数三截并排。
        # 190 是照更长的旧写法量的（那时最长的两种量到 173 和 203）；现在的写法去掉
        # 了箭头后的空格和件数前的「余」，每格都短了一截，所以眼下有富余。真放不下
        # 时（见 PriceDeltaDelegate）件数那截退到悬停提示里，不跟涨跌叠着画。
        header.resizeSection(COL_PRICE, 140)
        # 史低价跟原价/均价是一类参照值，宽度也照它们来；多出来的这 80 是从
        # 「商品名」那列（Stretch）身上抠的，其余列的宽度一个没动
        header.resizeSection(COL_LOW, 80)
        header.resizeSection(COL_EXPECT, 80)
        header.resizeSection(COL_REF, 80)
        header.resizeSection(COL_AVG, 90)
        # 成交三列只放个位数到五位数的成交量，100 就够；腾出来的 30 给「现价」列，
        # 免得去挤商品名——那列是 Stretch，加宽「现价」只会从它身上抠。
        for col in (COL_DEAL_BASE, COL_DEAL_BASE + 1, COL_DEAL_BASE + 2):
            header.resizeSection(col, 110)
        header.resizeSection(COL_CID, 100)
        header.resizeSection(COL_LINK, 60)
        # 商品名占剩余全部横向空间
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        # 模板说明挂在「链接」列头上。以前是挂在主窗口上的，但 Qt 找 tooltip 会沿
        # 父链上溯，于是每个没设自己 tooltip 的单元格都会弹它。
        self.table.horizontalHeaderItem(COL_LINK).setToolTip(
            f"点击「打开」用浏览器访问商品详情页\n链接由配置里的模板拼出：\n{template}"
        )
        self.table.horizontalHeaderItem(COL_FAVORITE).setToolTip(FAVORITE_HEADER_TIP)
        self.table.horizontalHeaderItem(COL_LOW).setToolTip(LOWEST_HEADER_TIP)
        self.table.horizontalHeaderItem(COL_EXPECT).setToolTip(EXPECTED_HEADER_TIP)
        self.table.horizontalHeaderItem(COL_AVG).setToolTip(AVG_HEADER_TIP)
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
        stats = {}
        try:
            entries = links.load_links(path, notes, stats)
        except OSError as err:
            if show_errors:
                QMessageBox.warning(self, "读取失败", f"读取 watchlist 出错：\n{err}")
            return False
        self.watchlist_path = path
        self.watch_entries = entries
        # 记下认不出的行数：>0 时清单不能重写（重写是按解析结果重排的，
        # 那几行会被写没），见 OnRefreshList
        self.last_unparsed = stats.get("unparsed", 0)
        added, removed = self.store.sync(
            [(e.cluster_id, e.name, e.favorite) for e in entries]
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

        收藏以清单为准（entry.favorite），缓存里那份（record["favorite"]）只是
        备份——sync 已经把清单的收藏推进缓存了，两边平时一致；不一致时（手改过
        库、缓存坏了重建）该信清单，它是用户意图的家。
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
        self.UpdateUpdatedLabel()

    def UpdateUpdatedLabel(self):
        """刷新顶部那行「上次更新时间」。

        扫的是各行缓存记录里的 price_updated_at，取最新那个——它跟那行的价格是一起
        写的，所以"中途停止""只抓新商品"这些都自然算得对，不用另外记一次本轮时间。
        跟表格描述的是同一屏数据，所以删掉唯一抓过的那行之后也该回到「尚未抓取过」：
        屏幕上确实一件数据都没有了。
        """
        newest = None
        for item in self.rows:
            stamp = _parse_stamp((item["record"] or {}).get("price_updated_at"))
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
        self.updated_label.setText(
            f"{LAST_UPDATE_PREFIX}{_format_stamp(newest)}" if newest else NO_UPDATE_TEXT
        )

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
        """双击缩略图看大图，双击「预期价格」改预期价。

        双击别的格子不管：那儿的双击另有用途（改列宽等）。
        """
        if col == COL_IMG:
            self.PreviewRow(row)
        elif col == COL_EXPECT:
            self.EditExpectedPrice(row)

    def ExpectedPriceDialog(self, row):
        """构造「设置预期价格」对话框（只造不弹）。

        跟 exec_ 拆开是为了让测试和截图工具能直接拿到它：模态弹窗得有人点，
        而这两个地方都不该被卡住。
        """
        entry = self.rows[row]["entry"]
        dialog = QInputDialog(self)
        dialog.setWindowTitle(EXPECTED_DIALOG_TITLE)
        dialog.setInputMode(QInputDialog.TextInput)
        dialog.setLabelText(f"{entry.name or entry.cluster_id} 的预期价格\n"
                            f"{EXPECTED_DIALOG_LABEL}")
        dialog.setTextValue(expected_number(entry.expected_price))
        # 按钮文字跟着项目里其他对话框走，别冒出一对英文
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        return dialog

    def EditExpectedPrice(self, row):
        """双击「预期价格」格：弹输入框改这一行的预期价，确定后立刻写回清单。"""
        if not 0 <= row < len(self.rows):
            return
        dialog = self.ExpectedPriceDialog(row)
        if dialog.exec_() != QDialog.Accepted:
            return

        # 输入框里要的是数字，但手快连着符号一起粘进来也认（price_number 会脱掉它）
        text = expected_number(dialog.textValue())
        if text:
            value = parser.price_number(text)
            # 认不出或不是正数就拦下来：存进去也永远不会到价，不如当场说清楚
            if value is None or value <= 0:
                QMessageBox.warning(self, EXPECTED_BAD_TITLE, EXPECTED_BAD_INPUT)
                return
        self.SetExpectedPrice(row, text)

    def SetExpectedPrice(self, row, text):
        """把预期价写进行模型并落盘；写文件失败就回滚。

        不回滚的话会出现"表格里绿着、清单里其实没写"这种对不上的状态，
        下次刷新清单一开，用户设的价就凭空没了。
        """
        entry = self.rows[row]["entry"]
        before = entry.expected_price
        entry.expected_price = text
        self.SetPriceCells(row, self.rows[row])  # 只这一行要重画

        if not self.SaveWatchlist():
            entry.expected_price = before
            self.SetPriceCells(row, self.rows[row])
            return False

        what = f"预期价 {expected_display(text)}" if text else "预期价（已清除）"
        self.progress_label.setText(
            f"已设置「{entry.name or entry.cluster_id}」的{what}，已写回 watchlist.txt"
            + self.last_note
        )
        return True

    def ToggleFavorite(self, row):
        """单击收藏格：切换这一行的收藏标记，写缓存、写清单；写文件失败就回滚。

        抓取中也允许点（SetBusy 不动）：收藏只是 entry 上的一个 bool，
        PollerThread 手里的任务排的是"第几行抓哪件商品"，跟它没有关系；
        写清单又是原子的（见 links.save_watchlist），不会写出一行半截的清单。

        回滚的路子跟 SetExpectedPrice 一样，两边都退回去：缓存先写了一笔，
        文件没写成的话也一并撤掉，免得留一份"缓存说收藏、清单说没有"的分叉
        ——清单才是收藏的家，下次 sync 本来也会把缓存掰回来，但那之前显示的是
        哪一份就得靠人去猜了。
        """
        if not 0 <= row < len(self.rows):
            return False
        entry = self.rows[row]["entry"]
        before = entry.favorite
        entry.favorite = not before
        self.SetFavoriteCell(row, self.rows[row])

        self.store.set_favorite(entry.cluster_id, entry.favorite)
        if not self.SaveWatchlist():
            entry.favorite = before
            self.store.set_favorite(entry.cluster_id, before)
            self.SetFavoriteCell(row, self.rows[row])
            return False

        what = "已加入收藏" if entry.favorite else "已取消收藏"
        self.progress_label.setText(
            f"「{entry.name or entry.cluster_id}」{what}，已写回 watchlist.txt"
            + self.last_note
        )
        return True

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

    def FavoriteCell(self, favorite):
        """「收藏」格：一颗实心（收藏）/ 空心（没收藏）的星，单击切换。

        星是当文本画的，不是图标：一个字符跟着字体走，深浅两套主题也不必各备
        一张图。颜色分两档（见 theme.favorite_color）——实心那颗是黄星，空心
        那颗用弱化色，免得一屏的空心星比商品名还抢眼。

        提示跟着状态走：这一格能点，得让人知道点下去是加还是删。
        """
        return self.MakeCell(
            FAVORITE_ON_TEXT if favorite else FAVORITE_OFF_TEXT,
            tooltip=FAVORITE_ON_TIP if favorite else FAVORITE_OFF_TIP,
            align=Qt.AlignCenter,
            color=theme.favorite_color(self.dark, bool(favorite)),
        )

    def SetFavoriteCell(self, row, item):
        """只重画这一行的收藏格（点了星、或写文件失败回滚时用）。"""
        self.table.setItem(
            row, COL_FAVORITE, self.FavoriteCell(item["entry"].favorite)
        )

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

    def StockTooltip(self, stock):
        """剩余件数那句完整的话：格子窄到画不下这一截时，还能从提示里看到。

        格子里那截是「 · 3件」（间隔号是排版用的，见 stock_text），提示里把它脱掉
        再补成一句话：「当前价格还3件」。
        """
        return f"当前价格还{stock.lstrip(' ·')}" if stock else ""

    def ReferenceCell(self, reference_price):
        """「原价」格：弱化色显示，跟现价拉开层次。"""
        return self.MakeCell(
            str(reference_price) if reference_price else NO_DATA_TEXT,
            color=theme.muted_color(self.dark),
        )

    def AvgCell(self, avg, price, sold_out):
        """「近30天均价」格：现价比它低时绿色加粗，其余照常显示。

        低于均价意味着这会儿买比最近一个月都划算，这是这一格唯一值得扫一眼的
        信息；没到那个份上它只是个参照值，跟原价一样"有就行、别抢眼"，所以不给
        弱化色、也不加粗，就是平常的字色。

        加粗跟「预期价格」到价、近期成交高亮同一个路子：单靠颜色，色觉障碍的人
        看不出差别，截图里也容易糊成一片。
        """
        if not avg:
            return self.MakeCell(NO_DATA_TEXT)
        cheaper = below_average(price, avg, sold_out)
        cell = self.MakeCell(
            str(avg),
            # 不变绿时给 None，让 MakeCell 照旧把整格文本当提示
            tooltip=f"现价 {price} 低于近30天均价 {avg}" if cheaper else None,
            color=theme.below_average_color(self.dark) if cheaper else None,
        )
        if cheaper:
            font = cell.font()
            font.setBold(True)
            cell.setFont(font)
        return cell

    def LowestCell(self, lowest, price, sold_out):
        """「史低价」格：历次抓到的最低价，现价正落在这个档位上时绿色加粗。

        跟「近30天均价」一个路子：它是拿现价跟一条参照值比出来的"这会儿买划不划算"，
        命中说明从记到过的最低价看，现在就是最便宜的一档。没命中的时候它只是个参照
        值（"有就行、别抢眼"），平常字色显示，跟均价那格一致。

        没记过史低价的行（新添加、一直失败、以及这一版之前就抓到的）显示占位符——
        宁可空着，也不要拿现价凑一个"史低价"出来，那个数没有历史可言。
        """
        if not lowest:
            return self.MakeCell(NO_DATA_TEXT)
        at_low = at_lowest(price, lowest, sold_out)
        cell = self.MakeCell(
            str(lowest),
            # 没命中时给 None，让 MakeCell 照旧把整格文本当提示
            tooltip=f"现价 {price} 就是史低价，从没记到过比这更低的价格"
            if at_low
            else None,
            color=theme.lowest_color(self.dark) if at_low else None,
        )
        if at_low:
            font = cell.font()
            font.setBold(True)
            cell.setFont(font)
        return cell

    def ClearedCell(self):
        """待抓取中的占位格：一个「--」，不挂提示（提示里只有「--」等于没说）。"""
        return self.MakeCell(CLEARED_TEXT, tooltip="")

    def PriceCell(self, price, sold_out, note="", delta=None, stock=""):
        """「现价」格：现价（售罄时带前缀）+ 涨跌 + 剩余件数，后两截各自上色。

        对齐固定成 PRICE_ALIGN：涨跌和剩余件数两截的落点都是按"现价从文本区
        左边起"算的（见 segment_rect），居中或右对齐就会几截叠在一起。

        delta 是 (文本, 涨跌数值)，stock 是 stock_text() 拼好的件数文本（自带
        「 · 」间隔号，所以中间不再插分隔）。两者都排在末尾，顺序定了就别改——
        拆几截是靠从右往左按长度切的（见 split_price_text）。

        note 是「这价格是什么时候抓的」那类补充说明，有它就不再退回"提示即全文"。
        """
        text = self.PriceText(price, sold_out)
        if delta:
            text += DELTA_GAP + delta[0]
        if stock:
            text += stock
        cell = self.MakeCell(
            text,
            tooltip=_tips(
                self.PriceTooltip(sold_out), self.StockTooltip(stock), note
            ) or None,
            align=PRICE_ALIGN,
        )
        if delta:
            cell.setData(DELTA_ROLE, delta[0])
            cell.setData(DELTA_COLOR_ROLE, theme.price_delta_color(delta[1], self.dark))
        if stock:
            cell.setData(STOCK_ROLE, stock)
            cell.setData(STOCK_COLOR_ROLE, theme.muted_color(self.dark))
        return cell

    def ExpectedCell(self, expected_price, price, sold_out):
        """「预期价格」格：到价了才变色，没到价就弱化显示。

        没设 → 占位符 + 弱化色；设了没到价 → 「￥数字」+ 弱化色（它是用户设的
        参照值，跟「原价」一样属于"有就行、别抢眼"）；到价 → 同样加「￥」再加粗 +
        到价色，扫一眼就看得见。这里不用判"值是不是个价格"：能进到 expected_price
        的值要么是输入框校验过的，要么是 links 解析时认过数字的（认不出的那段会被
        当成名字的一部分，见 _split_fields）。

        展示统一走 expected_display：清单里存的是数字，符号只在格子里补，手写的
        「¥50」也不会叠出两个符号来。

        加粗跟近期成交那个高亮同一个路子：单靠颜色，色觉障碍的人看不出差别，
        截图里也容易糊成一片。
        """
        text = expected_display(expected_price)
        if not text:
            return self.MakeCell(
                NO_DATA_TEXT, tooltip=SET_EXPECTED_TIP,
                color=theme.muted_color(self.dark),
            )

        reached = expected_reached(expected_price, price, sold_out)  # 比的是存着的值，不是展示文本
        cell = self.MakeCell(
            text,
            tooltip=_tips(
                f"现价 {price} 已低于预期价 {text}" if reached else "",
                EDIT_EXPECTED_TIP,
            ),
            color=(theme.expected_reached_color(self.dark) if reached
                   else theme.muted_color(self.dark)),
        )
        if reached:
            font = cell.font()
            font.setBold(True)
            cell.setFont(font)
        return cell

    def SetPriceCells(self, row, item):
        """填「现价 / 史低价 / 原价 / 近30天均价」四格，外加跟着它们走的「预期价格」格。

        取值优先级：这次抓到的 > 缓存里上次抓到的 > 占位符。抓失败时退回缓存，
        而不是把已经看到过的价格清掉——一次网络抖动不该让人白记一遍；代价是
        得在提示里说清这个数是什么时候抓的（_cached_note）。

        史低价只从缓存里取：它不是"这次抓到的"，而是历次抓下来攒在库里的一条记录
        （写入口见 OnResultReady），upsert 之后的那份 record 里就是最新值。

        预期价是用户设的不是抓来的，但它变不变色要看现价，所以跟着一起重画：
        不然抓完一轮，到价的行还挂着上一轮的旧颜色。

        首屏渲染（FillRow）和抓取回填（OnResultReady）都走这里，
        免得同一段渲染逻辑写两遍，哪天真改出不一致来。
        """
        record = item["record"] or {}
        lowest = record.get("lowest_price")
        if item.get("price_cleared"):  # 本次抓取还没轮到它，先留个空
            self.table.setItem(row, COL_PRICE, self.ClearedCell())
            self.table.setItem(row, COL_REF, self.ClearedCell())
            self.table.setItem(row, COL_AVG, self.ClearedCell())
            # 预期价不跟着清：它不是抓来的，没有"还没轮到"这回事，
            # 只是暂时没得比，按没到价的样子摆着。史低价同理：它是以前抓到过的事，
            # 这轮刷到它只是可能更低，清成「--」等于把已经知道的事盖掉了
            price, sold_out, stock = None, None, ""
        else:
            values = item["values"] or {}
            if values.get("ok"):
                price, sold_out = values.get("price"), values.get("sold_out")
                reference, avg = values.get("reference_price"), values.get("avg_price")
                stock = stock_text(values.get("stock_count"))
                note = ""
            else:
                price, sold_out = record.get("price_text"), record.get("sold_out")
                reference, avg = record.get("reference_price"), record.get("avg_text")
                stock = stock_text(record.get("stock_count"))
                note = _cached_note(record.get("price_updated_at"))

            if sold_out and not reference:
                # 售罄时接口整组不返回 price/priceSymbol，现价格里那个数就是原价
                # （见 PriceText）。原价列照抄一份：不然同一行「现价」顶着个数字、
                # 「原价」空着，看着像这格没抓到。
                reference = price

            self.table.setItem(
                row, COL_PRICE,
                self.PriceCell(price, sold_out, note, item.get("delta"), stock),
            )
            self.table.setItem(row, COL_REF, self.ReferenceCell(reference))
            self.table.setItem(row, COL_AVG, self.AvgCell(avg, price, sold_out))

        self.table.setItem(row, COL_LOW, self.LowestCell(lowest, price, sold_out))
        self.table.setItem(
            row, COL_EXPECT,
            self.ExpectedCell(item["entry"].expected_price, price, sold_out),
        )

    def ClearPrices(self, rows=None):
        """抓取开始前把各行的价格清成「--」：从空开始涨，才看得出刷到哪一行了。

        清的是现价/原价/均价三格——它们由同一次请求一起回来，只清一个反而怪。
        「预期价格」不在此列：那是用户设的，跟这次抓没抓到没关系（见 SetPriceCells）。
        标记记在行模型上（price_cleared），这样抓取中途换主题重画表格时，
        已经抓到的行照常显示新价，没轮到的还是「--」。

        rows 给一组行号时只清那几行（「仅抓取新添加商品」只抓其中一部分，
        其余行的价格不该跟着空一整轮），不传就是所有行都清一遍。
        """
        for row in range(len(self.rows)) if rows is None else rows:
            item = self.rows[row]
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

        self.SetFavoriteCell(row, item)
        self.SetPriceCells(row, item)
        self.SetDealCells(row, item)

        self.table.setItem(
            row, COL_IMG, self.MakeCell("", tooltip="双击查看大图", align=Qt.AlignCenter)
        )
        self.SetThumbnail(row, record.get("image_url"))

        url = links.build_detail_url(cluster_id, self.config["detail_url_template"])
        self.row_urls[row] = url
        link_item = self.MakeCell("打开", tooltip=url, align=Qt.AlignCenter)
        link_item.setData(Qt.UserRole, url)
        self.table.setItem(row, COL_LINK, link_item)

    def SetDealCells(self, row, item):
        """填「成交①/②/③」三格，够新鲜的那几条加粗 + 高亮色。

        取值跟 SetPriceCells 一个口径：本次抓到的优先，抓失败就退回缓存里上一轮
        那组，年龄按它的抓取时刻重算（见 _reaged_deals）。刚打开程序时三格都不空
        着——缓存里有上一轮抓到的成交，没道理等到再抓一轮才显示；抓失败时也留着
        上次那几条，那是真抓到过的成交，不会因为这次没抓到就变成不存在。

        首屏渲染（FillRow）和抓取回填（OnResultReady）都走这里，
        免得同一段渲染逻辑写两遍，哪天真改出不一致来。
        """
        values = item["values"] or {}
        if values.get("ok"):
            deals = values.get("deals") or []
        else:
            cached, anchor = store.cached_deals_of(item["record"] or {})
            deals = _reaged_deals(cached, _elapsed_since(anchor))
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
        for btn in (self.btn_add, self.btn_delete, self.btn_refresh_list,
                    self.btn_fetch, self.btn_fetch_new):
            btn.setEnabled(not busy)
        # 快捷键跟着「开始抓取」按钮一起开关：F5 的语义就是点那个按钮，
        # 按钮灰着的时候它也该照样没反应
        self.shortcut_fetch.setEnabled(not busy)
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
            # 预期价和收藏跟着行走：清单是它们的家（见 links 模块的说明），
            # 这两段都只在用户设过时才写出去（见 links.save_watchlist）
            items.append(
                (
                    item["entry"].cluster_id,
                    name,
                    item["entry"].expected_price,
                    item["entry"].favorite,
                )
            )
        notes = []
        try:
            links.save_watchlist(self.watchlist_path, items, notes)
        except OSError as err:
            QMessageBox.warning(self, "写入失败", f"写入 watchlist 出错：\n{err}")
            return False
        self.last_note = self.Note(notes)
        return True

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
            cluster_id, name, expected_price, favorite = links.parse_line(line)
            if cluster_id is None:
                invalid += 1
                continue
            if cluster_id in seen:  # 同一批里重复的只留第一条
                continue
            seen.add(cluster_id)
            # 粘进来的行里带了后两段（预期价、收藏）就一并收下，跟清单里是同一套格式
            entries.append(
                links.LinkEntry(
                    cluster_id, name, expected_price, line.strip(), favorite
                )
            )

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
        self.store.sync(
            [(e.cluster_id, e.name, e.favorite) for e in self.watch_entries]
        )
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
        """重新读取 watchlist.txt：新增的补成新行，删掉的移除，已抓数据保留。

        读进来顺手按规范格式写回一遍——手工编辑（粘链接、剪切粘贴换顺序）之后就
        靠这一步把文件理顺，不然得等下一次添加/删除/拖动才被顺手整理到。

        但必须先重读、且只认得出全部行时才写：重写是按解析结果重排的，
        但凡有一行认不出，那一行就会被写没——宁可这次不整理，也不删人家手写的行。
        """
        if self.IsPolling():
            return
        if not self.LoadWatchlist(self.watchlist_path):
            return
        # SaveWatchlist 会覆盖 last_note，先把这次读入的降级提示留一份
        load_note = self.last_note
        status = f"清单已同步，共 {len(self.rows)} 件商品"
        if self.last_unparsed:
            self.progress_label.setText(
                status + "，清单未重写（先修好认不出的行）" + load_note
            )
            return
        if self.SaveWatchlist():
            self.progress_label.setText(
                status + "，已按规范格式重写" + load_note + self.last_note
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
        self.StartFetch(tasks)

    @staticmethod
    def IsNewItem(item) -> bool:
        """这件商品还没被成功抓到过价格（新添加的，以及一直抓失败的）。

        判据取缓存里的 price_text：它跟 price_updated_at 是一起写的
        （见 store.upsert_item），所以"没有价格"就等于"从没抓到过价"，
        跟本次运行里加了什么无关，重启后依旧准确。
        """
        return not (item["record"] or {}).get("price_text")

    def OnFetchNew(self):
        """只抓还没有价格的商品，不动其他行。"""
        if self.IsPolling():
            return
        rows = [i for i, item in enumerate(self.rows) if self.IsNewItem(item)]
        if not rows:
            self.progress_label.setText("没有新添加的商品：每一件都已经抓到过价格")
            return
        self.StartFetch([(i, self.rows[i]["entry"]) for i in rows])

    # ---------------- 自动抓取 ----------------

    def UpdateAutoPollLabel(self):
        """刷新顶部那截自动抓取状态（关掉时只有「自动：关」）。"""
        if not self.config["auto_poll_enabled"]:
            self.auto_poll_label.setText(AUTO_POLL_OFF_TEXT)
            return
        minutes = self.config["auto_poll_interval_minutes"]
        scope = AUTO_SCOPE_LABELS.get(self.config["auto_poll_scope"], "全部")
        self.auto_poll_label.setText(f"自动：{minutes:g} 分钟 / {scope}")

    def ScheduleAutoPoll(self):
        """挂下一拍；没开自动抓取就什么都不做（定时器就此空着）。

        单次触发 + 每轮收尾重挂，所以间隔指的是「上一轮结束到下一轮开始」，
        某轮抓得慢只会让下一轮顺延，不会叠上来。
        """
        if not self.config["auto_poll_enabled"]:
            return
        # 至少 1 毫秒：config 只要求间隔大于 0，写成 0.0001 分钟的话 int() 会算成 0，
        # 而 Qt 里 0 毫秒的意思是「尽快触发」，就成了空转
        milliseconds = max(1, int(self.config["auto_poll_interval_minutes"] * 60_000))
        self.auto_poll_timer.start(milliseconds)

    def OnAutoPoll(self):
        """自动抓取的那一拍：正在抓就跳过，没商品可抓就报一声再挂下一拍。"""
        if self.IsPolling():
            # 手动在抓，这一拍跳过。不在这儿重挂——正在跑的那轮收尾时会挂，
            # 两边都挂的话定时器上会挂出两拍来
            return
        tasks = self.AutoPollTasks()
        if not tasks:
            self.progress_label.setText(AUTO_POLL_EMPTY_TEXT)
            self.ScheduleAutoPoll()
            return
        self.StartFetch(tasks, source="auto")

    def AutoPollTasks(self):
        """本次自动抓取该抓哪几行：按 auto_poll_scope 从清单里筛。

        售罄的判据取自缓存里上一轮的结果，刚加进来还没抓过的商品不算售罄，
        所以「仅售罄」的范围要等抓过至少一轮才铺得开。
        """
        scope = self.config["auto_poll_scope"]
        rows = []
        for i, item in enumerate(self.rows):
            if scope == "favorite" and not item["entry"].favorite:
                continue
            if scope == "sold_out" and not (item["record"] or {}).get("sold_out"):
                continue
            rows.append(i)
        return [(i, self.rows[i]["entry"]) for i in rows]

    def FetchProgressPrefix(self) -> str:
        """状态栏那行字的开头：手动是「查询中」，自动则点明是自动、抓的是哪个范围。"""
        if self.fetch_source != "auto":
            return "查询中"
        scope = self.config["auto_poll_scope"]
        if scope == "all":
            return "自动抓取"
        return f"自动抓取（{AUTO_SCOPE_LABELS.get(scope, scope)}）"

    def StartFetch(self, tasks, source="manual"):
        """两个抓取入口共用的起跑：复位本轮状态、清价、建轮询线程。

        tasks 为 [(行号, LinkEntry)]，可以只覆盖清单里的一部分（见 OnFetchNew）。
        source 记下这轮是谁发起的（manual / auto），只影响状态栏文案与要不要弹总结。
        """
        self.last_note = ""  # 开始新的一轮抓取，不带着之前的降级提示
        self.fetch_source = source
        self.names_learned = False
        self.poller_stopped = False
        self.run_changes = []  # 上一轮的变动和失败数不带到这一轮的总结里
        self.run_deals = []
        self.run_targets = []
        self.run_failures = 0
        self.run_total = len(tasks)
        self.run_started_at = time.monotonic()  # 计时起点；暂停另记，见 RunElapsed
        self.run_paused_seconds = 0.0
        self.run_paused_at = None
        self.run_elapsed = 0.0
        # 只清这次要抓的那几行，好一眼看出刷到哪一行；部分抓取时其余行的
        # 价格不该跟着空一整轮
        self.ClearPrices([row for row, _ in tasks])
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
        prefix = self.FetchProgressPrefix()
        # 开头带范围时后面不空格：「自动抓取（收藏）3/12」连着写更像一句话，
        # 空一格会让人以为括号里那截是独立的一句
        gap = "" if prefix.endswith("）") else " "
        self.progress_label.setText(f"{prefix}{gap}{current}/{total}")

    @staticmethod
    def DealsElapsed(anchor) -> float:
        """缓存里那组成交抓到之后过了多少秒（判「有没有新成交」要用，见 new_deals）。

        认不出这个时刻（老缓存没这列、文本坏了）就按 0 算：判定退化成"年龄区间
        一格没挪"，认得出同一条的照样认得出，只是更容易把老的算成新的。
        """
        elapsed = _elapsed_since(anchor)
        return 0.0 if elapsed is None else elapsed

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
            name = result["name"] or item["entry"].name or cluster_id  # 总结里好认人
            change = price_change(previous, result)
            if change is not None:
                change["name"] = name
                self.run_changes.append(change)
            # 成交的基准取自缓存里上一轮那组（带着它的抓取时刻），重启也还在；
            # 同样要在 upsert 之前取，不然拿到的是刚写进去的这一轮
            seen_deals, seen_anchor = self.store.cached_deals(cluster_id)
            if seen_deals:
                fresh = new_deals(
                    result["deals"], seen_deals, self.DealsElapsed(seen_anchor)
                )
                if fresh:
                    self.run_deals.append({"name": name, "deals": fresh})
            # 到价每轮都报：漏看一轮也不会错过（口径见 expected_reached）。
            # 判「设没设」用展示文本那个口径：光有符号的「¥」不算设了价
            if expected_display(item["entry"].expected_price) and expected_reached(
                item["entry"].expected_price, result["price"], result["sold_out"]
            ):
                self.run_targets.append(
                    {
                        "name": name,
                        "price": result["price"],
                        "expected_price": item["entry"].expected_price,
                    }
                )
            # 第一次抓到的新商品写入缓存；已缓存的也顺手刷新名称、缩略图和价格。
            # 史低价要跟"这次抓取之前"那份比（跟涨跌同理），所以也在 upsert 之前算：
            # 不刷新时递 None 进去，存储层按 COALESCE 保留旧值
            self.store.upsert_item(
                cluster_id,
                result["name"],
                result["image_url"],
                price_text=result["price"],
                reference_price=result["reference_price"],
                avg_text=result["avg_price"],
                sold_out=result["sold_out"],
                stock_count=result["stock_count"],
                lowest_price=updated_lowest(
                    previous.get("lowest_price"), result["price"], result["sold_out"]
                ),
                deals=result["deals"],
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
        self.SetDealCells(row, item)
        # 这一行的价格刚写进缓存，顶部那个时间得跟着往前走
        self.UpdateUpdatedLabel()

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
            # 攒下这一段暂停的时长：总结里的「用时」要把它刨掉（见 RunElapsed）
            if self.run_paused_at is not None:
                self.run_paused_seconds += time.monotonic() - self.run_paused_at
                self.run_paused_at = None
            self.progress_label.setText("继续抓取…")
        else:
            self.poller.pause()
            self.run_paused_at = time.monotonic()
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

    def RunElapsed(self) -> float:
        """本轮从起跑到收尾实际花掉的秒数：墙钟时间刨掉暂停的那段。

        暂停是用户自己按的，算进「用时」会让人以为抓取本身就那么慢。暂停时长在
        OnTogglePause 里攒；要是收尾时还暂停着（正常不会，暂停中跑不完），那段也
        一并刨掉。起点在 StartFetch 里定，没起过一轮就用建窗那一刻（见 __init__）。
        """
        idle = self.run_paused_seconds
        if self.run_paused_at is not None:
            idle += time.monotonic() - self.run_paused_at
        return max(0.0, time.monotonic() - self.run_started_at - idle)

    def OnPollFinished(self):
        self.SetBusy(False)
        self.RestorePendingPrices()  # 没轮到的行别一直空着（停止抓取的也一样）
        # 下一拍在这儿挂，三条收尾路径都要经过它：定时器是单次触发的，这一拍用完
        # 就空了，漏挂一次自动抓取就悄悄停了——尤其是用户中途「停止抓取」那次，
        # 他只是不想要这一轮，不是要把自动抓取关掉
        self.ScheduleAutoPoll()
        if self.poller_stopped:
            self.progress_label.setText("已停止抓取，已抓到的结果保留")
            return
        # 先定格用时：总结窗口换主题重渲染时要从这儿取，不能再算一遍
        self.run_elapsed = self.RunElapsed()
        if self.names_learned:
            # 抓到了新名称，顺手把清单刷新一次，方便用户直接在文件里管理
            if self.SaveWatchlist():
                self.progress_label.setText(
                    "完成，已把商品名写回 watchlist.txt" + self.last_note
                )
            self.MaybeShowSummary()
            return
        self.progress_label.setText("完成" + self.last_note)
        self.MaybeShowSummary()

    def MaybeShowSummary(self):
        """这一轮该不该弹总结：手动抓取照旧弹，自动抓取按配置（默认不弹）。

        自动抓取是没人看着的时候跑的，每半小时弹一个窗口只会把桌面糊满；
        想看结果随时看表格就行，所以默认只有手动抓取才弹。
        """
        if self.fetch_source == "auto" and not self.config["auto_poll_show_summary"]:
            return
        self.ShowSummary()

    def ShowSummary(self):
        """弹总结窗口，说清这一轮的涨跌、新成交和到价（见 SummaryDialog）。

        只在正常跑完时弹：中途「停止抓取」那一轮只抓了一半，拿半份数据当
        "总结"很容易让人以为其余商品没变化——那种情况该看的是表格里的「--」。
        """
        if self.summary_dialog is not None:
            self.summary_dialog.close()
            # 上一份还没关就换掉，顺手回收；留着引用会越堆越多
            self.summary_dialog.deleteLater()
        self.summary_dialog = SummaryDialog(
            self.run_changes,
            self.run_deals,
            self.run_targets,
            self.run_total,
            self.run_failures,
            self.run_elapsed,
            self.dark,
            self,
        )
        self.summary_dialog.show()

    def OnCellClick(self, row, col):
        """单击：收藏格切换收藏，「链接」格开浏览器，其余格子（选中、拖拽）不理会。"""
        if col == COL_LINK:
            url = self.row_urls.get(row)
            if url:
                webbrowser.open(url)
        elif col == COL_FAVORITE:
            self.ToggleFavorite(row)

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
        # 次要信息跟着主题弱化（跟「原价」那格同一个色），别跟按钮抢眼
        self.updated_label.setStyleSheet(f"color: {theme.muted_color(dark).name()};")
        self.auto_poll_label.setStyleSheet(f"color: {theme.muted_color(dark).name()};")
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
        """退出前把自动抓取停掉、缓存连接关掉：SQLite 的写入要落盘，文件句柄也别留着。"""
        self.auto_poll_timer.stop()
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
