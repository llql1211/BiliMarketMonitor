"""监视清单读写：解析 watchlist.txt -> [LinkEntry]，并负责格式化写回。

清单由程序格式化维护，每行形如：

    <clusterId> | <商品名> | <预期价> | <收藏>

商品名未知时只写 clusterId；预期价和收藏都是用户自己设的，只有设过的行才写
那一段，没设的行维持老样子（三段甚至一段），老清单原样读得进来。

预期价和收藏放在清单里而不是缓存库里：它们是抓不回来的用户意图，而缓存库按
store 模块自己的说法是「丢了还能重新抓」的加速层，不该存不可再生的东西。
清单还自带 .bak 备份，也能手工批量改价、改收藏。

为了兼容首次导入和手工编辑，读入时同时接受分享链接（含 share_medium、
bbid 等无关参数）和纯数字 ID。

清单是用户会手工编辑的文件，本模块不假设里面的内容一定规范：
编码可能被记事本改成 GBK，行可能是随手粘的一坨，名字里可能带 "|" 或换行。
这些一律降级处理（跳过 / 清洗 / 记进 notes），不抛给界面——
清单读不动就进不去程序，代价太大。
"""

import locale
import os
import re
import shutil
from dataclasses import dataclass

import parser  # 借它的 price_number 判「最后一段是不是价格」，跟界面同一口径

# 从链接中解析 clusterId，如 "...&clusterId=10000008780&..."
_CLUSTER_ID_RE = re.compile(r"clusterId=(\d+)")
# 纯数字 ID（名称和预期价已在 _split_fields 里切走了）
_PLAIN_ID_RE = re.compile(r"^(\d+)$")
# 商品名长度上限：清单是给人看的，超长名字只会挤爆界面
_MAX_NAME_LEN = 100
# 预期价长度上限（"¥1,299.50" 这种写法也够用了）
_MAX_PRICE_LEN = 20
# 收藏标记：只认这一个字符，写在行尾单独一段里（见 _strip_favorite）
_FAVORITE_MARK = "*"

_HEADER = (
    "# 监视清单：每行格式 <clusterId> | <商品名> | <预期价> | <收藏>，"
    "名称未知时只写 clusterId，没设预期价的行不写第三段，没收藏的行不写第四段"
    "（收藏标记就是一个 *）",
    "# 由程序维护，手工修改后点界面上的「刷新商品列表」同步",
)


@dataclass
class LinkEntry:
    cluster_id: str
    name: str = ""            # 清单里记录的商品名，可能为空（尚未抓取过）
    expected_price: str = ""  # 用户设的预期价，空串表示没设
    raw: str = ""             # 原始行，仅用于排查问题
    favorite: bool = False    # 用户打的收藏标记


def parse_line(line: str):
    """解析一行，返回 (clusterId, name, 预期价, 收藏)；无法识别返回 (None, None, "", False)。

    后两项拿不准时给空串/False 而不是 None：调用方（界面、清单写回）都是按
    字符串和布尔处理的，混进 None 只会逼着每处都判一次空。
    """
    if not isinstance(line, str):  # 调用方可能递进来 None / 数字
        return None, None, "", False
    text = line.strip()
    if not text or text.startswith("#"):  # 空行 / 注释行
        return None, None, "", False

    head, name, expected, favorite = _split_fields(text)

    match = _CLUSTER_ID_RE.search(head)  # 分享链接
    if match:
        return match.group(1), _clean_name(name), _clean_price(expected), favorite

    match = _PLAIN_ID_RE.match(head)  # 纯数字 ID
    if match:
        return match.group(1), _clean_name(name), _clean_price(expected), favorite

    return None, None, "", False


def load_links(path: str, notes=None, stats=None):
    """读取清单，返回去重后的 LinkEntry 列表（保持文件里的顺序）。

    同名条目以第一次出现的为准；文件里已有的名字会被保留。

    notes 传一个 list 时，读入过程中的异常情况会 append 进去（编码不对、
    有认不出来的行），由调用方决定怎么提示；不传就只是静默降级。
    文件读不了（不存在 / 没权限）仍然抛 OSError，交给调用方兜底。

    stats 传一个 dict 时，回填本次读入的统计（目前只有 unparsed：认不出商品 ID
    的行数）。调用方要用这个数来判「能不能安全地把清单重写回去」——写回是按
    解析结果重排的，认不出的行会被写没，所以那个数只有调用方知道该怎么用。
    """
    text, encoding_note = _read_text(path)
    if encoding_note:
        _note(notes, encoding_note)

    entries = []
    seen = set()
    unparsed = 0
    for line in text.splitlines():
        cluster_id, name, expected_price, favorite = parse_line(line)
        if cluster_id is None:
            # 空行/注释行是正常的，只有"看着有内容却认不出来"才算异常
            if line.strip() and not line.strip().startswith("#"):
                unparsed += 1
            continue
        if cluster_id in seen:
            continue
        seen.add(cluster_id)
        entries.append(
            LinkEntry(cluster_id, name, expected_price, line.strip(), favorite)
        )

    if unparsed:
        _note(
            notes,
            f"watchlist 里有 {unparsed} 行认不出商品 ID，已跳过"
            f"（先手工改好这几行，清单才会被重写成规范格式）",
        )
    if stats is not None:
        stats["unparsed"] = unparsed
    return entries


def save_watchlist(path: str, items, notes=None) -> int:
    """按规范格式原子写回清单，返回被跳过的条目数。

    items 为 [(clusterId, 商品名[, 预期价[, 收藏]])]，顺序即写入顺序；名称沿用
    文件里已有的值，预期价和收藏省略等同于没设。条目形状不对/ID 不是纯数字的
    跳过不写；名字和预期价里的 "|"、换行会被清洗掉（否则会写出一行坏清单，
    下次读进来就全乱了）。

    没设预期价时只写前两段（甚至只有 ID），没收藏时不写最后一段，免得老清单
    被一堆空尾巴撑开。收藏标记追加在最后一段的末尾，「收藏但没名字没价」的行
    写出来就是 `1001 | *`，读回来照样对得上。
    写前留一份 path + ".bak" 备份（只留第一次的），并用临时文件 + 替换避免写坏。
    """
    lines = list(_HEADER) + [""]
    skipped = 0
    for item in items or []:
        cluster_id, name, expected_price, favorite = _split_item(item)
        if cluster_id is None:
            skipped += 1
            continue
        name = _clean_name(name)
        expected_price = _clean_price(expected_price)
        if not name and not expected_price:
            line = cluster_id
        elif not expected_price:
            line = f"{cluster_id} | {name}"
        else:
            # 名称空着也得留出中间那段：解析是按位置认预期价的
            line = f"{cluster_id} | {name} | {expected_price}"
        if favorite:
            line += f" | {_FAVORITE_MARK}"
        lines.append(line)

    if skipped:
        _note(notes, f"有 {skipped} 条记录格式不对（ID 不是纯数字），已跳过不写入")

    content = "\n".join(lines) + "\n"

    if os.path.exists(path) and not os.path.exists(path + ".bak"):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass  # 备份失败不影响主流程

    # 目录被删掉（或首次运行）时自己建出来，别让整次保存失败
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp_path, path)
    return skipped


def build_detail_url(cluster_id: str, template: str) -> str:
    """按配置模板拼出详情页链接；拼不出来返回空串（界面显示 "—"，不会点出错链接）。

    模板里没有 {clusterId} 占位符时补到查询串上：宁可链接长得怪，
    也不能悄悄丢掉商品 ID 把人带到首页去。
    """
    cluster_id = _clean_id(cluster_id)
    if cluster_id is None or not isinstance(template, str) or not template.strip():
        return ""
    template = template.strip()
    if "{clusterId}" in template:
        return template.replace("{clusterId}", cluster_id)
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}clusterId={cluster_id}"


def _read_text(path: str):
    """读清单文本，返回 (文本, 提示或 None)。

    依次认三种情况：UTF-8（含记事本另存加的 BOM）、系统本地编码（中文 Windows
    下记事本存成 ANSI 就是 GBK）、最后忽略非法字节强读。
    中间那种是必须的：硬按 UTF-8 读 GBK 文件会抛 UnicodeDecodeError，
    而它不是 OSError——调用方 except OSError 兜不住，会直接把程序带崩。
    文件读不了（不存在 / 没权限）照旧抛 OSError。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read(), None
    except UnicodeDecodeError:
        pass

    for encoding in (locale.getpreferredencoding(False), "utf-8"):
        if not encoding:
            continue
        try:
            with open(path, "r", encoding=encoding) as f:
                text = f.read()
        except (UnicodeDecodeError, LookupError):
            continue
        return text, f"watchlist 不是 UTF-8 编码，已按 {encoding} 读取"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(), "watchlist 编码无法识别，已忽略非法字节读入（个别字可能显示为 ?）"


def _split_fields(text: str):
    """把一行切成 (ID/链接, 商品名, 预期价, 收藏)，缺的段给空串/False。

    收藏先剥：它固定在行尾，剥掉之后剩下的部分才走老的那套切法。两层的顺序
    不能颠倒——"1001 | 甲 | 50 | *" 要是先切价格，末段 "*" 不像价格，整个
    "50 | *" 会被当成名字的一部分。

    预期价认的是「最后一段，且它真像个价格」，不是按位置取第三段：名字里带
    "|" 的手工行本来就存在（"甲|乙" 那条路径有测试守着），按位置切会把名字
    从竖线处剁掉一截。加一层"得是价格"的判断，至少不会冤枉 "甲|乙"。

    代价是名字真以 "| 数字" 结尾时会被认成预期价。程序写回时名字里的 "|" 早
    被 _clean_name 清掉了，只剩手工编的行有这风险；而且认错了看得见——价格列
    上会多出一个数，不是悄悄少东西。

    中间那几段粘回去当名字（不多不少正好贴着 ID 和预期价两侧），免得名字里
    的竖线被当成两段丢掉。
    """
    text, favorite = _strip_favorite(text)
    parts = text.split("|")
    head = parts[0].strip()
    if len(parts) >= 3:
        last = parts[-1].strip()
        if parser.price_number(last) is not None:
            return head, "|".join(parts[1:-1]).strip(), last, favorite
    name = "|".join(parts[1:]).strip() if len(parts) > 1 else ""
    return head, name, "", favorite


def _strip_favorite(text: str):
    """剥掉行尾的收藏标记，返回 (剩下的文本, 是否收藏)。

    认的是「最后一段整个就是 *」而不是「行尾有个 *」：名字以星号结尾的商品
    （"限定版*"）不该被认成收藏。代价跟预期价那层一样——名字真以 "| *" 结尾
    的手工行会被认成收藏，而程序写回时名字里的 "|" 早被 _clean_name 清掉了，
    只剩手编的行有这风险；认错了也看得见：表格里那颗星亮着。
    """
    body, sep, tail = text.rpartition("|")
    if sep and tail.strip() == _FAVORITE_MARK:
        return body.rstrip(), True
    return text, False


def _split_item(item):
    """拆开 (clusterId, 名称[, 预期价[, 收藏]]) 并校验 ID。

    形状不对返回 (None, "", "", False)。预期价和收藏都可以省略：只关心 ID 和
    名称的调用方不用凑一串空字符串出来。
    """
    if not isinstance(item, (tuple, list)):
        return None, "", "", False  # 字符串会被逐字拆开，正好挡在门外
    if len(item) == 2:
        cluster_id, name = item
        expected_price, favorite = "", False
    elif len(item) == 3:
        cluster_id, name, expected_price = item
        favorite = False
    elif len(item) == 4:
        cluster_id, name, expected_price, favorite = item
    else:
        return None, "", "", False
    return _clean_id(cluster_id), name, expected_price, _clean_favorite(favorite)


def _clean_id(cluster_id):
    """clusterId 只认纯数字字符串（int 也接受）；其余返回 None。"""
    if cluster_id is None or isinstance(cluster_id, bool):
        return None
    text = str(cluster_id).strip()
    return text if text.isdigit() else None


def _clean_name(name) -> str:
    """商品名清洗：去掉会破坏清单格式的字符（"|"、换行、制表），压缩空白并限长。"""
    if not isinstance(name, str):
        return ""
    text = " ".join(name.replace("|", " ").split())
    return text[:_MAX_NAME_LEN]


def _clean_price(value) -> str:
    """预期价清洗：同样只清掉会破坏清单格式的字符并限长，不校验是不是数字。

    "认不认得出这是个价格"由界面层说了算（那边有 parser.price_number）：
    这里不认识数字，认错了就会把用户手写的内容从清单里抹掉，而清单是用户
    能直接编辑的文件。界面认不出时那格显示「—」并说明原因，文件里的原值
    原样留着，等用户自己改。
    """
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("|", " ").split())
    return text[:_MAX_PRICE_LEN]


def _clean_favorite(value) -> bool:
    """收藏标记归一：只认 True 和清单里那个写法 "*"，其余一律当没收藏。

    不写 bool(value) 是有意的：那样 "0"、"no" 这种字符串会算成收藏，
    而这一列的输入来自调用方拼的元组，认宽了只会静默多写一颗星。
    """
    if isinstance(value, str):
        return value.strip() == _FAVORITE_MARK
    return value is True


def _note(notes, message: str):
    """把提示写进调用方给的 list（没给就只是降级，不留痕）。"""
    if isinstance(notes, list):
        notes.append(message)


def default_watchlist_path() -> str:
    """默认的 watchlist.txt 路径（data/ 目录，即 src/ 的上一级下的 data/）。"""
    return os.path.join(_data_dir(), "watchlist.txt")


def _data_dir() -> str:
    """数据文件目录（项目根目录下的 data/）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data")
