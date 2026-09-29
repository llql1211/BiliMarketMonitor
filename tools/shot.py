"""把界面渲染成 PNG，用来看样式改得对不对。

用途
    对话框、表格、主题的样式改完之后出一张图肉眼确认。比开真窗口快：Qt 走
    offscreen，不用显示器、不用人点，也不用等抓取跑完。

用法
    pixi run shot                      # 全部场景 × 深浅两色 → tmp/out/
    pixi run shot summary              # 只渲染名字里含 summary 的场景
    pixi run shot --list               # 只列场景名，不出图
    pixi run python tools/shot.py --out D:/tmp/x --data D:/other/data

    加场景：写个收 dark 参数、返回要看的 QWidget 的函数，挂 @scene("名字") 即可；
    settle 是出图前留给它转事件循环的秒数（main 靠它等缩略图落上来）。

依赖
    PyQt5（项目依赖）与 src/ 下的模块。渲染本身不联网。

输出
    <out>/<场景名>-<dark|light>.png，跑完把路径打出来。out 默认 tmp/out/。

限制
    抓的是布局落定后的静态一帧——模态弹窗、动画、悬停态都不在图里，真验交互
    还是得开一次程序。跑真窗口的场景要联网拉缩略图，第一次慢，之后吃沙盒缓存。
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

# 早于 PyQt5 导入：没有显示器也要能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent  # tools/ 的上一级就是项目根
SOURCE_DATA = ROOT / "data"  # 默认拿这份真实数据去复制
SANDBOX = ROOT / "tmp" / "data"  # 复制到这儿，程序跑到这里为止
DEFAULT_OUT = ROOT / "tmp" / "out"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from PyQt5.QtWidgets import QApplication  # noqa: E402  （必须晚于上面的平台设置）

import theme  # noqa: E402

SCENES = {}


def scene(name, settle=0.3):
    """登记一个场景。settle 是出图前留给它转事件循环的秒数——等布局落定，
    也是等缩略图那种异步来的东西（main 靠它把图喂上）。"""

    def _register(factory):
        SCENES[name] = (factory, settle)
        return factory

    return _register


def use_sandbox(source, sandbox, keep):
    """把程序眼里的 data/ 整体指向沙盒副本（默认每次重新复制一份）。

    直接对着真实 data/ 构造 MainWindow 是有过事故的：缓存同步以清单为准，
    清单要是空的就把用户的 cache.db 清了。tests/conftest.py 里的 _guard_real_data
    是同一个出发点的另一个版本，两边别只改一处。
    """
    import config
    import links
    import store

    if not source.is_dir():
        raise SystemExit(f"没有可复制的数据目录：{source}（用 --data 指一个）")
    if sandbox.exists() and not keep:
        shutil.rmtree(sandbox)
    if not sandbox.exists():
        shutil.copytree(source, sandbox)

    config.data_dir = lambda: str(sandbox)
    links._data_dir = lambda: str(sandbox)
    store.default_db_path = lambda: str(sandbox / "cache.db")


def seed_expected_prices():
    """往沙盒清单里塞两行预期价，好让表格场景看出这一列的三种样子。

    只动沙盒副本，真实清单一个字都不改——出图用的数据必须是"看一眼就能丢"的。

    价格按缓存里的现价推：一行定得比现价高（到价，绿），一行定得比现价低
    （没到，灰），其余的行不设（占位符）。挑的是缓存里有价、且没售罄的商品
    ——售罄行按设计不比预期价（那格的"现价"其实是原价），挑中它就看不出绿色，
    图就白出了。按现价推而不是写死数字，是为了缓存换了也不至于整列失真。
    """
    import links
    import parser
    import store

    path = links.default_watchlist_path()
    if not os.path.exists(path):
        return
    entries = links.load_links(path)

    db = store.Store(store.default_db_path())
    try:
        priced = []
        for entry in entries:
            record = db.get_item(entry.cluster_id) or {}
            price = parser.price_number(record.get("price_text"))
            if price and not record.get("sold_out"):
                priced.append((entry, price))
            if len(priced) == 2:
                break
    finally:
        db.close()

    if len(priced) < 2:
        return  # 缓存还是空的（比如第一次跑），这次就不摆样本了
    for (entry, price), factor in zip(priced, (1.2, 0.5)):
        entry.expected_price = _amount(price * factor)

    links.save_watchlist(
        path, [(e.cluster_id, e.name, e.expected_price) for e in entries]
    )


def seed_lowest_prices(skip=3, count=4, factor=0.85):
    """往沙盒缓存里几行塞一条比现价低的史低价，好让表格场景看出这一列的样子。

    挑的是「算得出涨跌」的那些行（有价、没售罄）里、跳过前 skip 个之后的几行。
    前几个是 seed_sample_fetches 要喂新价的：那一下会把它们的史低价刷出来（图上
    是命中色）；这几行则带着一条更便宜的旧记录出现（图上是平常色）；再往后的行
    没有记录，显示占位符——三种样子一张图里都能看到。

    只动沙盒副本，真实缓存一个字都不改（跟 seed_expected_prices 同一个出发点）。
    价格写成「¥37.4」这种跟抓取结果一致的形状（真抓到时存的就是这个写法）。
    """
    import links
    import parser
    import store

    path = links.default_watchlist_path()
    if not os.path.exists(path):
        return
    entries = links.load_links(path)

    db = store.Store(store.default_db_path())
    try:
        eligible = []
        for entry in entries:
            record = db.get_item(entry.cluster_id) or {}
            price = parser.price_number(record.get("price_text"))
            if price and not record.get("sold_out"):
                eligible.append((entry.cluster_id, price))
        for cluster_id, price in eligible[skip:skip + count]:
            db.upsert_item(
                cluster_id, None, None,
                lowest_price=f"¥{_amount(price * factor)}",
            )
    finally:
        db.close()


def _amount(value) -> str:
    """60.0 -> "60"、60.5 -> "60.5"：别在清单里留一串没用的零。"""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _sample_result(name, price, stock=None, sold_out=False):
    """造一份抓取结果：走真 parser，形状跟线上一模一样。

    手写结果 dict 也能让图出来，但那样样本跟实现就各说各话了（见 _change 里
    同样的理由）——比如件数是哪来的、售罄时按钮长什么样，都得跟真响应一致。
    """
    import parser

    button = {"buttonState": 2, "buttonText": "已售罄"} if sold_out else {
        "buttonState": 1, "buttonText": f"最低价仅{stock}件",
    }
    return parser.parse_cluster(
        {
            "success": True,
            "data": {
                "clusterBasicInfoFloorVO": {"clusterName": name},
                "clusterPriceFloorVO": {"priceTag": {"firstPrice": price}},
                "clusterPurchaseButton": button,
            },
        }
    )


def seed_sample_fetches(window):
    """给头三个「算得出涨跌」的行照「刚抓完一轮」的样子回填结果。

    图上要看的那两截——涨跌和剩余件数——都得抓过才有：涨跌要跟上一轮比，
    件数是接口这次报的。只靠缓存渲染的画面里两截都是空的，这个改动就看不出
    效果了。三行分别凑出「涨跌 + 件数」「涨跌 + 件数」「只有件数」三种并排。
    挑行时跳过售罄的：它们本来就比不了价，白占一个样本位（清单前几行恰好
    是售罄的，早先按行号取，三个样本最后只画出来一个）。

    走真 OnResultReady（抓取线程回填用的就是这个入口），价格按缓存里的现价
    推：降一点就有涨跌，不动就没有。名字沿用清单里的那个，免得表格被改名。
    """
    import parser

    samples = ((0.9, 1), (0.95, 2), (1.0, 3))
    used = 0
    for row in range(len(window.rows)):
        if used >= len(samples):
            return
        record = window.rows[row]["record"] or {}
        price = parser.price_number(record.get("price_text"))
        if not price or record.get("sold_out"):
            continue  # 没有价格可比的（或售罄的）行喂进去，涨跌算不出来
        factor, stock = samples[used]
        used += 1
        window.OnResultReady(
            row,
            _sample_result(
                window.rows[row]["entry"].name, _amount(price * factor), stock
            ),
        )


# ---------------- 场景 ----------------


def _change(previous, result, name):
    """造一条变动。真调 price_change 而不是手写 dict：样本跟实现就不会各说各话，
    哪天真改了结构，这里先炸，而不是拿着一张早就对不上的图糊弄人。
    """
    from app import price_change

    change = price_change(previous, result)
    assert change is not None, f"样本造错了：{name} 这一条算不出变动"
    change["name"] = name
    return change


def _sample_changes():
    """四类各一条；第二个名字带尖括号，顺便看转义有没有漏。"""
    return [
        _change({"price_text": "50", "sold_out": False},
                {"price": "44", "sold_out": False}, "乙烯基唱片收纳箱"),
        _change({"price_text": "128", "sold_out": False},
                {"price": "135", "sold_out": False}, "复古铁皮玩具车 <限定色>"),
        _change({"price_text": "20", "sold_out": False},
                {"price": "20", "sold_out": True}, "木质拼图相框"),
        _change({"price_text": "39", "sold_out": True},
                {"price": "39", "sold_out": False}, "帆布单肩包"),
    ]


def _sample_deals():
    """新增成交那一块：一件商品一条新成交，另一件一次冒出两条。"""
    return [
        {"name": "乙烯基唱片收纳箱",
         "deals": [{"price": "¥44", "time": "2小时前"}]},
        {"name": "复古铁皮玩具车 <限定色>",
         "deals": [{"price": "¥130", "time": "9小时前"},
                   {"price": "¥135", "time": "1天前"}]},
    ]


def _sample_targets():
    """低于预期价那两块：现价该是到价色，预期价带「￥」。"""
    return [
        {"name": "木质拼图相框", "price": "¥18", "expected_price": "20"},
        {"name": "帆布单肩包", "price": "¥35", "expected_price": "¥39"},
    ]


@scene("summary")
def _summary(dark):
    """三块明细各来几条：顺便看它们之间的空行和顺序。"""
    from app import SummaryDialog

    return SummaryDialog(
        _sample_changes(), _sample_deals(), _sample_targets(), 12, 0, 21.4, dark
    )


@scene("summary-empty")
def _summary_empty(dark):
    """一条变动都没有的样子：没有明细表，窗口该收窄。"""
    from app import SummaryDialog

    return SummaryDialog([], [], [], 12, 0, 3.2, dark)


@scene("summary-failures")
def _summary_failures(dark):
    """有商品没抓到：副标题得把这件事说出来；顺便看分钟级的用时怎么写。"""
    from app import SummaryDialog

    return SummaryDialog(_sample_changes()[:2], [], [], 12, 3, 96, dark)


@scene("summary-deals-only")
def _summary_deals_only(dark):
    """只有新成交：价格没动、也没设预期价的那一轮长什么样。"""
    from app import SummaryDialog

    return SummaryDialog([], _sample_deals(), [], 12, 0, 8.0, dark)


@scene("summary-many")
def _summary_many(dark):
    """拉长到 40 条：看长清单是在里面滚，还是把窗口撑成一条竖带。"""
    from app import SummaryDialog

    changes = []
    for i in range(20):
        base = {"price_text": str(100 + i), "sold_out": False}
        changes.append(_change(base, {"price": str(100 + i - 6), "sold_out": False},
                               f"降价款 {i + 1}"))
        changes.append(_change(base, {"price": str(100 + i + 6), "sold_out": False},
                               f"涨价款 {i + 1}"))
    return SummaryDialog(changes, [], [], 40, 0, 154.6, dark)


# 场景里造出来、但只有 Qt 那边认得的对象得在这儿挂住：Python 这边一回收，
# C++ 那边跟着销毁，出图时就只剩个"wrapped C/C++ object has been deleted"
_KEEP = []


@scene("expected-dialog")
def _expected_dialog(dark):
    """双击「预期价格」弹出的输入框。

    单独看它一眼是因为这框里的单行输入框（QLineEdit）在 QSS 里没有专属规则，
    暗色下只吃到 QWidget 那条通配背景——边框、选中态对不对只有出图才知道。
    """
    import app
    import links

    window = app.MainWindow()
    window.ApplyTheme(dark)
    path = links.default_watchlist_path()
    if os.path.exists(path):
        window.LoadWatchlist(path)
    if not window.rows:
        raise SystemExit("沙盒清单是空的，这个场景得有商品才弹得出输入框")
    _KEEP.append(window)  # 输入框是它的子窗口，它一被回收图也就没了
    return window.ExpectedPriceDialog(0)


def _sample_preview_pixmap(size):
    """在程序里画一张假商品图：出图不联网，也省得等真图下回来。

    特意画了边框和居中的图形：图比窗口小的时候，留白摊得匀不匀一眼就看得出来。
    """
    from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor("#cfdcea"))
    painter = QPainter(pixmap)
    pen = QPen(QColor("#41618a"))
    pen.setWidth(max(2, size // 30))
    painter.setPen(pen)
    margin = size // 12
    painter.drawRect(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.setBrush(QColor("#7fa86b"))
    painter.drawEllipse(size // 4, size // 4, size // 2, size // 2)
    painter.end()
    return pixmap


def _preview_dialog():
    """摆一个贴好图的预览窗口，尺寸按真机来。

    offscreen 那块虚拟屏只有 800x600，_ApplyDefaultSize 会老老实实把窗口缩到
    放得下为止（440），出图就看不到真机上那个大小了。这里显式撑到常见屏幕上
    的尺寸：要看的是图怎么铺、留白匀不匀，不是小屏上的降级样子。

    先 show 出来再贴图：铺满的基准是可视区，而可视区要排完版才定得下来，不先
    摆出来的话量到的是排版前的旧值，出的图会差一圈。
    """
    import app

    dialog = app.ImagePreviewDialog("乙烯基唱片收纳箱")
    dialog.resize(app.PREVIEW_DEFAULT_SIDE, app.PREVIEW_DEFAULT_SIDE + app.PREVIEW_BUTTON_ROW)
    dialog.show()
    QApplication.processEvents()
    dialog.SetImage(_sample_preview_pixmap(app.PREVIEW_SIZE))
    QApplication.processEvents()
    return dialog


@scene("preview")
def _preview(dark):
    """双击缩略图看的大图，默认那一档：图铺满窗口，四周不该有奇怪的留白。"""
    return _preview_dialog()


@scene("preview-zoom")
def _preview_zoom(dark):
    """滚轮放大之后：图比窗口大了，该看到滚动条，也该看得出放的是局部。"""
    dialog = _preview_dialog()
    dialog.OnWheel(120 * 4)  # 往上滚四格
    return dialog


@scene("main", settle=1.5)
def _main(dark):
    """真窗口：清单和缓存都来自沙盒，缩略图靠 settle 等它落上来。

    再回填几行抓取结果，好看清「现价 + 涨跌 + 剩余件数」三截并排的样子。
    """
    import app
    import links

    window = app.MainWindow()
    window.ApplyTheme(dark)
    window.resize(1280, 720)
    path = links.default_watchlist_path()
    if os.path.exists(path):
        window.LoadWatchlist(path)
    seed_sample_fetches(window)
    window.ReportStartupNotes()
    return window


# ---------------- 渲染 ----------------

# offscreen 平台插件在 Windows 上不带字体库（QFontDatabase().families() 是空的），
# 字一个都画不出来：出图就成了"有框有线、没字"，正好把最该看的东西看没了。
# 这儿手动塞几个系统字体进去；缺哪个跳哪个，找不到就还是老样子。
FALLBACK_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑：界面正文用的就是它
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def load_fonts():
    """没字体库时补上系统字体。有字体库的平台（开真窗口那种）一个字不动。

    得在 QApplication 之后、建控件之前调——字体库是挂在 application 上的。
    """
    from PyQt5.QtGui import QFontDatabase

    if QFontDatabase().families():
        return  # 平台自带字体库，没我们什么事
    for path in FALLBACK_FONTS:
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)


def render(widget, path, dark, settle):
    app = QApplication.instance()
    theme.apply_theme(app, dark)
    widget.show()
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        app.processEvents()  # 子线程拉回来的图也是排队投递的，光 sleep 等不到
        time.sleep(0.02)
    widget.grab().save(str(path))
    widget.close()
    widget.deleteLater()
    app.processEvents()


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenes", nargs="*", help="场景名（子串匹配）；不填就是全部")
    parser.add_argument("--out", help=f"出图目录，默认 {DEFAULT_OUT}")
    parser.add_argument("--data", help=f"要复制哪份数据，默认 {SOURCE_DATA}")
    parser.add_argument("--keep", action="store_true",
                        help=f"复用已有的 {SANDBOX}，不重新复制（省下拉缩略图）")
    parser.add_argument("--list", action="store_true", help="只列场景名，不出图")
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    if args.list:
        print("\n".join(sorted(SCENES)))
        return 0

    wanted = [n for n in SCENES if not args.scenes or any(w in n for w in args.scenes)]
    if not wanted:
        print(f"没有匹配的场景。有的是：{'、'.join(sorted(SCENES))}")
        return 1

    # 这个引用必须留着：光写一句 QApplication([])，Python 立刻把它回收掉，
    # Qt 那边跟着销毁，后面建第一个 QWidget 就直接 abort（没有回溯，退 127）
    app = QApplication.instance() or QApplication([])
    load_fonts()

    out = Path(args.out) if args.out else DEFAULT_OUT
    use_sandbox(Path(args.data) if args.data else SOURCE_DATA, SANDBOX, args.keep)
    seed_expected_prices()
    seed_lowest_prices()
    out.mkdir(parents=True, exist_ok=True)

    for name in sorted(wanted):
        factory, settle = SCENES[name]
        for dark in (True, False):
            suffix = "dark" if dark else "light"
            path = out / f"{name}-{suffix}.png"
            render(factory(dark), path, dark, settle)
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
