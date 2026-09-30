"""配置读取：优先 data/config.toml（用户自定义，不入库），无效/缺失时用
data/config.example.toml 兜底。

两份文件都在 data/ 目录；config.example.toml 随仓库分发，既是默认值也是格式参考。
注释用 TOML 原生的 #，解析时自动忽略，不需要额外的约定。

配置是用户手写的文件，什么值都可能出现：TOML 里能直接写 inf / nan，
间隔写成 inf 会让抓取线程卡死，写成 nan 会让间隔判断全部失效（高频打接口）。
所以每个值都单独校验，不合格的退回默认值并记一条 warning，不影响其他项。
"""

import math
import os
import re
import shutil
import tomllib

CONFIG_FILENAME = "config.toml"
EXAMPLE_FILENAME = "config.example.toml"

# 代码内的兜底默认值（example 文件也缺失时生效）
DEFAULTS = {
    "detail_url_template": (
        "https://mall.bilibili.com/neul-next/resell/detail.html?clusterId={clusterId}"
    ),
    "poll_interval_seconds": 2.0,
    "retry_interval_seconds": 1.0,
    "request_timeout_seconds": 10.0,
    # 成交高亮：最近一次成交在多少小时内就加粗上色（0 = 关闭）
    "deal_highlight_within_hours": 24.0,
    "deal_highlight_color": "#e07000",
    # 自动抓取：按固定间隔反复抓，间隔从上一轮结束算起；
    # 范围 all = 清单全部，favorite = 只抓收藏的，sold_out = 只抓缓存里记着售罄的
    "auto_poll_enabled": False,
    "auto_poll_interval_minutes": 30.0,
    "auto_poll_scope": "all",
    "auto_poll_show_summary": False,
    # 企业微信推送：webhook 由用户自建的企业微信群机器人提供（地址格式见 notifier.py）。
    # 头三项是总开关、地址和限流，后面五项是各自的触发类型，互相独立
    "notify_enabled": False,
    "notify_wecom_webhook": "",
    "notify_min_interval_seconds": 60.0,
    "notify_on_restock": True,
    "notify_on_favorite_target": True,
    "notify_on_favorite_lowest": False,
    "notify_on_favorite_drop": False,
    "notify_on_any_target": False,
}


def data_dir() -> str:
    """数据文件目录（项目根目录下的 data/）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data")


def user_config_path() -> str:
    return os.path.join(data_dir(), CONFIG_FILENAME)


def example_config_path() -> str:
    return os.path.join(data_dir(), EXAMPLE_FILENAME)


def load_config():
    """返回 (config, warnings)。

    优先级：代码默认值 < config.example.toml < config.toml 中通过校验的键。
    """
    config = dict(DEFAULTS)
    warnings = []
    for path, label in ((example_config_path(), "example"), (user_config_path(), "用户")):
        data, error = _read_toml(path)
        if error:
            warnings.append(f"{os.path.basename(path)} 读取失败（{error}），已忽略")
            continue
        if data is None:
            continue  # 文件不存在，静默跳过
        _merge(config, data, label, warnings)

    if not os.path.exists(user_config_path()):
        warnings.append(
            f"未找到 {CONFIG_FILENAME}，当前使用 {EXAMPLE_FILENAME} 的默认配置"
        )
    _check_notify_pair(config, warnings)
    return config, warnings


def save_config(changes, path=None):
    """把设置窗口改过的项写回 config.toml，返回实际写下去的键（按 DEFAULTS 的顺序）。

    只写传进来的这几项，别的行一个都不动——就地替换 `key = value` 那一行，
    注释、用户自己排的顺序、程序不认识的项全留着。不整份重写是因为
    config.toml 是用户手写的文件，注释是他自己写下的说明，冲掉就找不回来了；
    顺带也不会把 example 里的默认值钉进用户文件：没动过的项继续跟着 example 走。

    config.toml 不存在时拿 config.example.toml 当底稿（模板里的注释正好当说明），
    两份都没有就从零写。写前留一份 path + ".bak"（只留第一次那份），
    临时文件 + 替换避免写坏——跟 links.save_watchlist 是同一个套路，
    那边是给清单写的；各留一份，是因为 config 是纯数据模块，不该反过来认识 links。

    `changes` 里不认识的键（不在 DEFAULTS 里）直接跳过：调用方只该递配置项，
    写进去也会被 load_config 当未知项警告一遍。
    """
    path = path or user_config_path()
    wanted = [key for key in DEFAULTS if key in changes]
    if not wanted:
        return []

    lines = _config_text(path).splitlines()
    while lines and not lines[-1].strip():  # 末尾空行先摘掉，下面统一补一个换行
        lines.pop()

    written, missing = [], []
    for key in wanted:
        literal = _toml_literal(changes[key])
        if _replace_line(lines, key, literal):
            written.append(key)
        else:
            missing.append((key, literal))

    if missing:
        if lines:
            # 空一行再补：补上去的项跟上面那段（多半是别的主题的）分得开
            lines.append("")
        lines.extend(f"{key} = {literal}" for key, literal in missing)

    _write_atomic(path, "\n".join(lines) + "\n")
    return written + [key for key, _ in missing]


def _config_text(path):
    """要改的那份文本：优先 config.toml，文件不存在才退回 example。

    只有「文件不存在」才退回：读不动（权限之类）时得让 OSError 冒上去，
    不然会拿 example 当底稿，把用户那份没读成的配置覆盖掉。
    """
    for candidate in (path, example_config_path()):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            continue
    return ""


_KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)=")


def _replace_line(lines, key, literal):
    """把 `key = ...` 那一行改成新值，改了返回 True。

    只认顶格（缩进任意）的键值行：被注释掉的 `# poll_interval_seconds = 2`
    不算数——那是用户自己留的记录，动它比在末尾补一行更唐突。
    """
    for index, line in enumerate(lines):
        match = _KEY_LINE.match(line)
        if match and match.group(2) == key:
            lines[index] = f"{match.group(1)}{key} = {literal}"
            return True
    return False


def _toml_literal(value):
    """把值写成 TOML 字面量。

    数字一律当浮点写（`60.0`）：间隔、超时这些项在 DEFAULTS 里本来就是 float，
    写成 `60` 会变成整数，读回来虽然过得了校验，但跟默认值的类型对不上。
    """
    if isinstance(value, bool):  # 得排在 int 前面：bool 是 int 的子类
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value))
    text = str(value)
    # 基本字符串里只有反斜杠和引号要转义；换行、回车这类控制字符直接去掉——
    # 它们写进去就成了两个键值行，下次读回来整份配置都乱
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(ch for ch in escaped if ch == "\t" or ch >= " ")
    return f'"{escaped}"'


def _write_atomic(path, content):
    if os.path.exists(path) and not os.path.exists(path + ".bak"):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass  # 备份失败不影响主流程
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp_path, path)


def _check_notify_pair(config, warnings):
    """「开了推送却没填 webhook」单独说一声。

    逐键校验够不着这个状态：单看 notify_wecom_webhook，空串是合法的（表示还没配）；
    单看 notify_enabled，true 也合法。合起来才是「推送开着、但一条也发不出去」，
    多半是漏填了一行。提醒归提醒，配置照收——用户可能正打算回头填。
    """
    if config["notify_enabled"] and not config["notify_wecom_webhook"]:
        warnings.append(
            "已启用企业微信推送，但没填 notify_wecom_webhook，推送发不出去"
        )


def _read_toml(path):
    """读 TOML，返回 (data, error)；文件不存在返回 (None, None)。"""
    try:
        with open(path, "rb") as f:  # tomllib 只接受二进制模式
            data = tomllib.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        # UnicodeDecodeError 不是 OSError：配置文件被存成 GBK 时它才出现，
        # 漏掉就是启动即崩，所以单独列出来
        return None, str(err)
    return data, None


def _merge(config, data, label, warnings):
    if not isinstance(data, dict):  # 解析结果不是表（正常 TOML 不会出现）
        warnings.append(f"{label}配置的内容不是一个 TOML 表，已忽略")
        return
    for key, value in data.items():
        if key not in DEFAULTS:
            warnings.append(f"{label}配置里的未知项 {key} 已忽略")
            continue
        checked = _validate(key, value, label, warnings)
        if checked is not None:
            config[key] = checked


def _check_url_template(value, key, label, warnings):
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{label}配置的 {key} 不是有效字符串，已忽略")
        return None
    value = value.strip()
    if "{clusterId}" not in value:
        warnings.append(f"{label}配置的 {key} 缺少 {{clusterId}} 占位符，已忽略")
        return None
    if not value.startswith(("http://", "https://")):
        # 模板不是链接的话，点「打开」会跳到乱七八糟的地方
        warnings.append(f"{label}配置的 {key} 不是 http(s) 链接，已忽略")
        return None
    return value


_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _check_color(value, key, label, warnings):
    """颜色只校验形状（#rrggbb 或颜色名）。

    能不能真当颜色用由 theme 层的 QColor 说了算——把 QColor 拉进来校验会把
    本模块（纯数据，不依赖 Qt）绑到 GUI 上，这里放行、那边兜底更划算。
    """
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{label}配置的 {key} 不是有效字符串，已忽略")
        return None
    value = value.strip()
    if _HEX_COLOR.match(value) or value.isalpha():
        return value
    warnings.append(f"{label}配置的 {key} 不是合法颜色（#rrggbb 或颜色名），已忽略")
    return None


def _check_bool(value, key, label, warnings):
    """只认真布尔值。

    TOML 里写 `auto_poll_enabled = 1` 或 `"true"` 都拒掉：字符串 "false" 是真值
    （非空串），照它走会把「关掉」读成「打开」，不如退回默认值并说一声。
    """
    if isinstance(value, bool):
        return value
    warnings.append(f"{label}配置的 {key} 不是 true / false，已忽略")
    return None


def _check_enum(*allowed):
    """生成一个「只认这几个值」的校验器，给 auto_poll_scope 这类选项用。

    拼错的选项静默退回默认值，比照字面用要安全——写 `scope = "favarite"` 的
    本意显然是「只抓收藏」，退回"全部"只是多抓几件，按字面走则会一件都不抓。
    """

    def _check(value, key, label, warnings):
        if isinstance(value, str) and value.strip() in allowed:
            return value.strip()
        warnings.append(
            f"{label}配置的 {key} 只能是 {' / '.join(allowed)}，已忽略"
        )
        return None

    return _check


# 企业微信机器人的地址前缀。notifier.WECOM_API_PREFIX 是同一件事的另一份：
# 本模块不依赖 Qt（notifier 依赖），notifier 又不依赖项目模块，只好各留一份，
# 由 tests/test_config.py 的用例盯着两边别写岔
_WECOM_PREFIX = "https://qyapi.weixin.qq.com/"


def _check_webhook_url(value, key, label, warnings):
    """webhook 只认企业微信机器人的地址；空串放行，表示「还没配」。

    空串既是默认值，也是「先把推送开着、地址回头再填」的中间状态，不能当错误
    拒掉。非空就必须落在企业微信的域名下——地址写岔了就是把消息推给别人。
    """
    if not isinstance(value, str):
        warnings.append(f"{label}配置的 {key} 不是字符串，已忽略")
        return None
    value = value.strip()
    if not value:
        return ""
    if not value.startswith(_WECOM_PREFIX):
        warnings.append(
            f"{label}配置的 {key} 不是企业微信机器人的地址"
            f"（应以 {_WECOM_PREFIX} 开头），已忽略"
        )
        return None
    return value


def _check_positive_number(value, key, label, warnings):
    return _check_number(value, key, label, warnings, allow_zero=False)


def _check_non_negative_number(value, key, label, warnings):
    """允许 0：高亮阈值为 0 就是「关闭高亮」。"""
    return _check_number(value, key, label, warnings, allow_zero=True)


def _check_number(value, key, label, warnings, allow_zero):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"{label}配置的 {key} 不是数字，已忽略")
        return None
    if not math.isfinite(value):
        # TOML 允许写 inf / nan，直接放过会让间隔失效（卡死或高频请求）
        warnings.append(f"{label}配置的 {key} 必须是有限数字，已忽略")
        return None
    if value < 0 or (value == 0 and not allow_zero):
        bound = "不小于" if allow_zero else "大于"
        warnings.append(f"{label}配置的 {key} 必须{bound} 0，已忽略")
        return None
    return float(value)


# 每个配置项各自的校验规则（新增配置项必须在这里登记，漏登记会被 _validate 拒掉）
_VALIDATORS = {
    "detail_url_template": _check_url_template,
    "poll_interval_seconds": _check_positive_number,
    "retry_interval_seconds": _check_positive_number,
    "request_timeout_seconds": _check_positive_number,
    "deal_highlight_within_hours": _check_non_negative_number,
    "deal_highlight_color": _check_color,
    "auto_poll_enabled": _check_bool,
    "auto_poll_interval_minutes": _check_positive_number,
    "auto_poll_scope": _check_enum("all", "favorite", "sold_out"),
    "auto_poll_show_summary": _check_bool,
    "notify_enabled": _check_bool,
    "notify_wecom_webhook": _check_webhook_url,
    "notify_min_interval_seconds": _check_positive_number,
    "notify_on_restock": _check_bool,
    "notify_on_favorite_target": _check_bool,
    "notify_on_favorite_lowest": _check_bool,
    "notify_on_favorite_drop": _check_bool,
    "notify_on_any_target": _check_bool,
}


def _validate(key, value, label, warnings):
    """按 key 分发到各自的校验函数。

    以前是按默认值的类型分支（str 就套 URL 模板规则），但那条规则是给
    详情页模板量身定做的——再加一个字符串项（高亮色）就会被它一律拒掉。
    """
    checker = _VALIDATORS.get(key)
    if checker is None:  # 有默认值却没登记校验：宁可拒绝，也不放没校验过的值进来
        warnings.append(f"{label}配置的 {key} 没有校验规则，已忽略")
        return None
    return checker(value, key, label, warnings)


def check_value(key, value):
    """校验单独一个值，返回 (值, 原因)：原因非 None 时值不可用。

    给设置窗口用：用户当场填错要立刻拦住，而规则仍然是上面这一份——界面不另写
    一套判断，免得两边说法不一样。label 传空串，文案里就不会冒出「用户配置的」
    这种给文件看的说法。
    """
    warnings = []
    checked = _validate(key, value, "", warnings)
    if checked is not None:
        return checked, None
    return None, _reason(warnings[-1] if warnings else "", key)


def _reason(warning, key):
    """把校验器的警告收成一句能给用户看的原因。

    "配置的 poll_interval_seconds 必须大于 0，已忽略" -> "必须大于 0"：
    设置窗口那一行左边就写着这项的名字，键名再重复一遍是噪音；「已忽略」也得
    去掉——那儿根本没忽略，是拦住了没写。
    """
    text = warning[len("配置的 "):] if warning.startswith("配置的 ") else warning
    if text.endswith("，已忽略"):
        text = text[:-len("，已忽略")]
    return text[len(key) + 1:] if text.startswith(key + " ") else text
