"""设置窗口：把 config.example.toml 里的项摆成三页表单，改完交给主窗口保存。

窗口只管「摆、收、校验」三件事，不碰文件、不认识 MainWindow：

- 当前值由调用方递进来（`values`，比 config 多一项 `theme`），窗口照着回填；
- 校验复用 config 的逐项规则（`config.check_value`），界面不另写一套；
- 写文件和生效（重画表格、重挂定时器）都留给主窗口（app.MainWindow）去做。

于是这个模块只依赖 config 和 theme，能单独测，出图工具也能直接把它造出来看。

配置项的**展示顺序、标签、提示**都写在这儿的 FIELDS 里；哪一项归哪一页、是不是
分组摆，也由它说了算。这几样是界面的事，不该塞进 config（那边只管值和校验）。
代价是「加一项配置」要动两处，由 tests/test_settings.py 盯着别漏。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import config
import theme

TITLE = "设置"

# 主题不在 config.toml 里：它记在缓存库的设置表里（谁用谁知道），所以它没有校验器，
# 也不参与「写回 config.toml」。放在这张表里只是因为它跟其他项一样是「一个开关」。
THEME_KEY = "theme"

BOOL = "bool"
NUMBER = "number"
TEXT = "text"
COLOR = "color"
ENUM = "enum"

# 三页签：(内部名, 标题)。顺序就是标签页从左到右的顺序
SECTIONS = (
    ("basic", "基本设置"),
    ("auto", "定时抓取"),
    ("notify", "企业微信推送"),
)

# 一行的三列：标签 / 圈「？」/ 控件。圈「？」紧挨在控件的左边（用户要求的），
# 所以它自成一列，正好也把所有行的控件左边对齐
LABEL_WIDTH = 150  # 标签列的起步宽度：够放下最长的「成交高亮时限（小时）」
BADGE_SIZE = 16


class Field:
    """一项配置在窗口里的样子。

    kind 决定用哪个控件（见 SettingsDialog._MakeEditor），hint 是输入框里的写法
    示例，tip 是那个圈「？」上的话——一行说完，别在悬停里写小作文。
    """

    __slots__ = ("key", "label", "kind", "tip", "section", "group", "hint", "choices")

    def __init__(self, key, label, kind, tip, section, group="", hint="", choices=()):
        self.key = key
        self.label = label
        self.kind = kind
        self.tip = tip
        self.section = section
        self.group = group
        self.hint = hint
        self.choices = choices


FIELDS = (
    # ---- 基本设置 ----
    # 主题排在最前：它是唯一一项不进 config.toml 的，单独归到「界面」这一组，
    # 免得跟下面几项混成「配置项」
    Field(
        THEME_KEY, "深色模式", BOOL,
        "勾上就是暗色主题，按确定后换；记在本地设置里，不写进 config.toml",
        "basic", group="界面",
    ),
    Field(
        "detail_url_template", "详情页链接模板", TEXT,
        "拼「打开」用的详情页地址，必须含 {clusterId} 占位符",
        "basic", group="抓取与高亮", hint="https://…?clusterId={clusterId}",
    ),
    Field(
        "poll_interval_seconds", "抓取间隔（秒）", NUMBER,
        "两次请求之间等多久；调小抓得快，但别太小",
        "basic", group="抓取与高亮", hint="2",
    ),
    Field(
        "retry_interval_seconds", "失败后重试间隔（秒）", NUMBER,
        "某件抓失败后等多久重试一次（只重试一次）",
        "basic", group="抓取与高亮", hint="1",
    ),
    Field(
        "request_timeout_seconds", "请求超时（秒）", NUMBER,
        "单次请求最多等多久，超了就当这次失败",
        "basic", group="抓取与高亮", hint="10",
    ),
    Field(
        "deal_highlight_within_hours", "成交高亮时限（小时）", NUMBER,
        "这么久以内的成交加粗上色；填 0 表示不高亮",
        "basic", group="抓取与高亮", hint="24",
    ),
    Field(
        "deal_highlight_color", "成交高亮颜色", COLOR,
        "支持 #rrggbb 和颜色名，如 orange",
        "basic", group="抓取与高亮", hint="#e07000",
    ),
    # ---- 定时抓取 ----
    Field(
        "auto_poll_enabled", "启用定时抓取", BOOL,
        "开着就按下面的间隔自动抓，不用手点",
        "auto",
    ),
    Field(
        "auto_poll_interval_minutes", "抓取间隔（分钟）", NUMBER,
        "从上一轮跑完算起，隔多久抓下一轮",
        "auto", hint="30",
    ),
    Field(
        "auto_poll_scope", "抓取范围", ENUM,
        "定时抓取只抓这个范围里的商品",
        "auto", choices=(("全部商品", "all"), ("仅收藏", "favorite"), ("仅售罄", "sold_out")),
    ),
    Field(
        "auto_poll_show_summary", "跑完弹总结窗口", BOOL,
        "自动抓完也弹总结；不勾就只写一句到状态栏",
        "auto",
    ),
    # ---- 企业微信推送 ----
    Field(
        "notify_enabled", "启用企业微信推送", BOOL,
        "开着才会往群里发；下面没配好照样发不出去",
        "notify",
    ),
    Field(
        "notify_wecom_webhook", "群机器人 webhook", TEXT,
        "群里「…」→ 添加群机器人拿到的地址；等于发消息的权限，别外传",
        "notify", hint="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…",
    ),
    Field(
        "notify_min_interval_seconds", "两次推送最小间隔（秒）", NUMBER,
        "隔得太近的推送先攒着，下一轮再发，不会丢",
        "notify", hint="60",
    ),
    Field(
        "notify_on_restock", "售罄 → 在售", BOOL,
        "任何商品从售罄变回在售",
        "notify", group="通知内容设置",
    ),
    Field(
        "notify_on_favorite_target", "收藏商品跌破预期价", BOOL,
        "只在刚跌破那一刻推，一直低着不会再推",
        "notify", group="通知内容设置",
    ),
    Field(
        "notify_on_favorite_lowest", "收藏商品刷新史低价", BOOL,
        "刷新记录才推；现价正好等于史低价不算",
        "notify", group="通知内容设置",
    ),
    Field(
        "notify_on_favorite_drop", "收藏商品降价", BOOL,
        "价格上下波动很常见，开着容易刷屏",
        "notify", group="通知内容设置",
    ),
    Field(
        "notify_on_any_target", "非收藏商品跌破预期价", BOOL,
        "没收藏的商品也按预期价提醒",
        "notify", group="通知内容设置",
    ),
)


class TipBadge(QLabel):
    """带圈的「？」，鼠标停上去出提示。

    提示语走 QToolTip，跟界面其他地方的提示一个口径（悬停延时由 theme 统一设）。
    颜色跟着主题走，所以有个 SetDark——主窗口换主题时窗口里的自绘部分要重上色。
    """

    def __init__(self, tip, parent=None):
        super().__init__("?", parent)
        self.setFixedSize(BADGE_SIZE, BADGE_SIZE)
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(tip)
        self.setCursor(Qt.WhatsThisCursor)
        font = self.font()
        if font.pointSize() > 9:  # 字号拿不到（像素字号）就照原样，别把「?」缩没了
            font.setPointSize(font.pointSize() - 3)
        font.setBold(True)
        self.setFont(font)
        self.SetDark(False)

    def SetDark(self, dark: bool):
        color = theme.muted_color(dark).name()
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color};"
            f" border-radius: {BADGE_SIZE // 2}px;"
        )


def number_text(value) -> str:
    """数字回填到输入框里的写法：`2` 而不是 `2.0`。"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def color_reason(text):
    """颜色认不认得出来，认不出返回一句原因，认得出返回 None。

    config 那边只查形状（#rrggbb 或纯字母），形状过关但颜色名不存在
    （"orangejuice"）时它会放行，由 theme 退回默认色——用户看到的是「填了没生效」。
    窗口里手边就有 QColor，能当场问一句，就别留到表格上让人猜。
    """
    if text and not QColor(text).isValid():
        return f"认不出这个颜色「{text}」（写 #rrggbb 或颜色名，如 orange）"
    return None


def changed_values(values, original) -> dict:
    """挑出改过的项：只有这几项会被写回 config.toml。

    跟原值一样的跳过——文件里那一行别去重写一遍（注释和排版是用户的），
    也没必要把 example 里的默认值抄进用户文件。主题不算配置项，永远不写。
    """
    return {
        key: value
        for key, value in values.items()
        if key != THEME_KEY and value != original.get(key)
    }


class SettingsDialog(QDialog):
    """三页签的设置窗口（模态，确定 / 取消）。

    只造不弹也能用：`Values()` 拿到的是校验后的值，主窗口拿它去写文件。

    窗口里改什么都不当场生效——包括主题在内，都要等按下「确定」由主窗口去做，
    「取消」就等于什么都没发生过。
    """

    def __init__(self, values, dark, parent=None):
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.current_values = dict(values)
        self.result_values = {}
        self.checkboxes = {}       # key -> QCheckBox（布尔项的取值都走它）
        self.editors = {}          # key -> QLineEdit / QComboBox
        self.badges = []
        self.placeholder_editors = []  # 有占位文字的输入框，上色时要一起过一遍
        self.section_titles = {}       # 内部名 -> 页签标题（报错文案里要用）
        self.section_index = {}        # 内部名 -> 页签序号（出错时翻页要用）

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        for index, (name, title) in enumerate(SECTIONS):
            self.tabs.addTab(self._BuildSection(name), title)
            self.section_titles[name] = title
            self.section_index[name] = index
        layout.addWidget(self.tabs)

        # 出错提示：平时藏着，占位的那点高度不碍事；写不开的行由它自己换行
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.OnAccept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.SetDark(dark)
        # 定个大小而不是 adjustSize()：三页里控件数量差得多，让 Qt 自己量的话
        # 切一页窗口就跟着跳一下
        self.resize(720, 560)

    # ---------------- 摆 ----------------

    def _LabelColumnWidth(self) -> int:
        """标签那一列统一多宽：按最长的那条标签量。

        一页里的每组各是一个 QGridLayout，列宽只由自己那几行顶出来——哪组里
        恰好有长标签，那组的控件就整体往右挪一截，跟别的组对不齐。统一按最长
        的那条来（再兜个 LABEL_WIDTH 的下限），所有行就落在同一条竖线上。

        量的是字宽而不是写死一个数：用户那边的字号、DPI 跟这里不一样，
        写死的话标签会被裁掉半个字。
        """
        metrics = self.fontMetrics()
        return max(LABEL_WIDTH,
                   max(metrics.horizontalAdvance(field.label) for field in FIELDS))

    def _BuildSection(self, section):
        """一页：按 group 分组摆行，没写 group 的项直接铺在外层。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        grids = {}
        for field in FIELDS:
            if field.section != section:
                continue
            grid = grids.get(field.group)
            if grid is None:
                if field.group:
                    box = QGroupBox(field.group)
                    grid = QGridLayout(box)
                    outer.addWidget(box)
                else:
                    grid = QGridLayout()
                    outer.addLayout(grid)
                grid.setColumnStretch(2, 1)  # 控件那列吃掉多余宽度
                grid.setColumnMinimumWidth(0, self._LabelColumnWidth())
                grid.setColumnMinimumWidth(1, BADGE_SIZE)
                grids[field.group] = grid
            self._AddRow(grid, grid.rowCount(), field)
        outer.addStretch()
        return page

    def _AddRow(self, grid, row, field):
        """一行摆三格：标签 / 圈「？」/ 控件。

        圈「？」在控件左边，是用户点名要的位置；布尔项那边标签本来就是复选框
        自己的文字，所以只摆后两格（第一格空着，控件照样跟别的行对齐）。
        """
        if field.kind == BOOL:
            box = QCheckBox(field.label)
            box.setChecked(bool(self.current_values.get(field.key)))
            self.checkboxes[field.key] = box
            grid.addWidget(box, row, 2)
        else:
            grid.addWidget(QLabel(field.label), row, 0)
            editor = self._MakeEditor(field)
            self.editors[field.key] = editor
            grid.addWidget(editor, row, 2)
        badge = TipBadge(field.tip)
        self.badges.append(badge)
        grid.addWidget(badge, row, 1)

    def _MakeEditor(self, field):
        current = self.current_values.get(field.key)
        if field.kind == ENUM:
            combo = QComboBox()
            for text, value in field.choices:
                combo.addItem(text, value)
            index = combo.findData(current)
            # 值不在选项里（配置写了个没见过的范围，已被校验拦过、退回默认）时
            # 落在第一项：界面显示的和实际生效的都是「全部商品」，对得上
            combo.setCurrentIndex(index if index >= 0 else 0)
            return combo

        text = number_text(current) if field.kind == NUMBER else str(current or "")
        editor = QLineEdit(text)
        if field.hint:
            editor.setPlaceholderText(field.hint)
            self.placeholder_editors.append(editor)
        return editor

    # ---------------- 收 ----------------

    def Collect(self):
        """把窗口里的东西收成 (values, errors)。

        一项不合格就整份不写：让用户改完再点确定，别写出一份"一半新一半旧"的
        配置。errors 里带上页签标题，出错时据此翻到那一页。
        """
        values = {THEME_KEY: self.checkboxes[THEME_KEY].isChecked()}
        errors = []
        for field in FIELDS:
            if field.key == THEME_KEY:
                continue
            value, reason = self._Read(field)
            if reason is None:
                value, reason = config.check_value(field.key, value)
            if reason is None and field.kind == COLOR:
                reason = color_reason(value)
            if reason is not None:
                errors.append((field.section, f"{field.label}：{reason}"))
                continue
            values[field.key] = value
        return values, errors

    def _Read(self, field):
        """从控件里读出一个值，返回 (值, 原因)；原因非 None 表示这个值不能用。

        校验分两段：这里只管"填的是不是个数"（输入框里可能是任意文字），
        范围、格式那些规则一律交给 config.check_value。
        """
        if field.kind == BOOL:
            return self.checkboxes[field.key].isChecked(), None
        if field.kind == ENUM:
            return self.editors[field.key].currentData(), None
        text = self.editors[field.key].text().strip()
        if field.kind != NUMBER:
            return text, None
        try:
            return float(text), None
        except ValueError:
            return None, f"得填个数字（现在写的是「{text}」）" if text else "得填个数字"

    def OnAccept(self):
        values, errors = self.Collect()
        if errors:
            self.ShowErrors(errors)
            return
        self.result_values = values
        self.accept()

    def Values(self) -> dict:
        """校验过的值（只在按下「确定」之后有意义）。"""
        return dict(self.result_values)

    def ShowErrors(self, errors):
        """把不合格的项列在窗口底部，并翻到第一项所在的那一页。"""
        self.error_label.setText("；".join(
            f"{self.section_titles.get(section, section)}：{reason}"
            for section, reason in errors
        ))
        self.error_label.setVisible(True)
        self.tabs.setCurrentIndex(self.section_index.get(errors[0][0], 0))

    # ---------------- 主题 ----------------

    def SetDark(self, dark: bool):
        """给窗口里自绘的那几处上色（圈「？」、占位灰、出错提示）。

        窗口只在造出来时按当前主题上一次色：主题要等按下「确定」才换，换了窗口
        也就关了，不会留一个半深半浅的窗口在屏幕上。
        """
        self.error_label.setStyleSheet(f"color: {theme.error_color(dark).name()};")
        for editor in self.placeholder_editors:
            theme.style_placeholder(editor, dark)
        for badge in self.badges:
            badge.SetDark(dark)
