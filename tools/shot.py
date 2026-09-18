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


@scene("summary")
def _summary(dark):
    from app import SummaryDialog

    return SummaryDialog(_sample_changes(), 12, 0, dark)


@scene("summary-empty")
def _summary_empty(dark):
    """一条变动都没有的样子：没有明细表，窗口该收窄。"""
    from app import SummaryDialog

    return SummaryDialog([], 12, 0, dark)


@scene("summary-failures")
def _summary_failures(dark):
    """有商品没抓到：副标题得把这件事说出来。"""
    from app import SummaryDialog

    return SummaryDialog(_sample_changes()[:2], 12, 3, dark)


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
    return SummaryDialog(changes, 40, 0, dark)


@scene("main", settle=1.5)
def _main(dark):
    """真窗口：清单和缓存都来自沙盒，缩略图靠 settle 等它落上来。"""
    import app
    import links

    window = app.MainWindow()
    window.ApplyTheme(dark)
    window.resize(1280, 720)
    path = links.default_watchlist_path()
    if os.path.exists(path):
        window.LoadWatchlist(path)
    window.ReportStartupNotes()
    return window


# ---------------- 渲染 ----------------


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

    out = Path(args.out) if args.out else DEFAULT_OUT
    use_sandbox(Path(args.data) if args.data else SOURCE_DATA, SANDBOX, args.keep)
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
