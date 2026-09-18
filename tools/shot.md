# shot —— 界面截图

改完对话框、表格、主题的样式之后，出一张图肉眼确认，别只读代码。

比开真窗口快：Qt 走 offscreen，不用显示器、不用人点，也不用等抓取跑完。
用法、依赖、输出、限制都在文件顶部的 docstring 里，也可以直接 `--help` 看。

## 场景

每个场景渲染两遍（深色 / 浅色），出图在 `tmp/out/`。

| 场景 | 看什么 |
| --- | --- |
| `main` | 真窗口：清单、缓存、主题、缩略图、各列宽窄 |
| `summary` | 抓取总结窗口，四类变动各一条 |
| `summary-empty` | 一条变动都没有：没有明细表，窗口该收窄 |
| `summary-failures` | 有商品没抓到：副标题得把这件事说出来 |
| `summary-many` | 40 条变动：长清单在里面滚，还是把窗口撑成竖带 |

`main` 读的是复制到 `tmp/data/` 的真实清单和缓存。缩略图要联网，第一次慢；
`--keep` 可以复用上次的沙盒，省下重新拉图。

## 加场景

写个收 `dark` 参数、返回要看的 QWidget 的函数，挂上装饰器：

```python
@scene("add-dialog")
def _add_dialog(dark):
    from app import AddDialog
    dialog = AddDialog(dark)
    dialog.resize(520, 300)
    return dialog
```

装饰器的第二个参数 `settle` 是出图前留给它转事件循环的秒数，默认 0.3 秒；
要等异步来的东西（缩略图、下载）就调大，`main` 用的是 1.5。

## 为什么要有沙盒

跑 `main` 得构造 `MainWindow`，而它一上来就会让缓存库跟随清单同步——以清单为准，
清单要是空的就把 `cache.db` 清了。这是真出过的事故（见 `tests/conftest.py` 里
`_guard_real_data` 的注释）。所以工具先把 `data/` 整个复制到 `tmp/data/`，再把程序
眼里的数据路径指过去，副本随便造。

改这段的时候记得和 `tests/conftest.py` 的 `data_files` fixture 对着看：两边是同一个
出发点的两个版本，别只改一处。

## 两个坑

- **`QApplication` 的引用得留着**。光写一句 `QApplication([])`，Python 立刻把它回收掉，
  Qt 那边跟着销毁，后面建第一个 QWidget 直接 abort——没有回溯，退出码 127。
- **样本变动真调 `price_change()` 造，不手写 dict**。哪天真改了结构，工具先炸，
  而不是拿着一张早就对不上的图糊弄人。

## 不做什么

不做像素比对测试。字体、DPI、抗锯齿跨机器不一致，这种测试会飘，和仓库里精确断言的
风格也不搭。要验交互（弹窗、悬停、动画）还是得开一次程序——出图只抓静态一帧。
