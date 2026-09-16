"""监视清单读写：解析 watchlist.txt -> [LinkEntry]，并负责格式化写回。

清单由程序格式化维护，每行形如：

    <clusterId> | <商品名>

商品名未知时只写 clusterId。为了兼容首次导入和手工编辑，
读入时同时接受分享链接（含 share_medium、bbid 等无关参数）和纯数字 ID。

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

# 从链接中解析 clusterId，如 "...&clusterId=10000008780&..."
_CLUSTER_ID_RE = re.compile(r"clusterId=(\d+)")
# 纯数字 ID（可带 "| 名称" 后缀）
_PLAIN_ID_RE = re.compile(r"^(\d+)\s*(?:\|\s*(.*))?$")
# 商品名长度上限：清单是给人看的，超长名字只会挤爆界面
_MAX_NAME_LEN = 100

_HEADER = (
    "# 监视清单：每行格式 <clusterId> | <商品名>，名称未知时只写 clusterId",
    "# 由程序维护，手工修改后点界面上的「刷新商品列表」同步",
)


@dataclass
class LinkEntry:
    cluster_id: str
    name: str = ""  # 清单里记录的商品名，可能为空（尚未抓取过）
    raw: str = ""   # 原始行，仅用于排查问题


def parse_line(line: str):
    """解析一行，返回 (clusterId, name)；无法识别返回 (None, None)。"""
    if not isinstance(line, str):  # 调用方可能递进来 None / 数字
        return None, None
    text = line.strip()
    if not text or text.startswith("#"):  # 空行 / 注释行
        return None, None

    match = _CLUSTER_ID_RE.search(text)  # 分享链接
    if match:
        name = ""
        if "|" in text:
            name = _clean_name(text.split("|", 1)[1])
        return match.group(1), name

    match = _PLAIN_ID_RE.match(text)  # 纯数字 ID，可带 "| 名称"
    if match:
        return match.group(1), _clean_name(match.group(2) or "")

    return None, None


def load_links(path: str, notes=None):
    """读取清单，返回去重后的 LinkEntry 列表（保持文件里的顺序）。

    同名条目以第一次出现的为准；文件里已有的名字会被保留。

    notes 传一个 list 时，读入过程中的异常情况会 append 进去（编码不对、
    有认不出来的行），由调用方决定怎么提示；不传就只是静默降级。
    文件读不了（不存在 / 没权限）仍然抛 OSError，交给调用方兜底。
    """
    text, encoding_note = _read_text(path)
    if encoding_note:
        _note(notes, encoding_note)

    entries = []
    seen = set()
    unparsed = 0
    for line in text.splitlines():
        cluster_id, name = parse_line(line)
        if cluster_id is None:
            # 空行/注释行是正常的，只有"看着有内容却认不出来"才算异常
            if line.strip() and not line.strip().startswith("#"):
                unparsed += 1
            continue
        if cluster_id in seen:
            continue
        seen.add(cluster_id)
        entries.append(LinkEntry(cluster_id, name, line.strip()))

    if unparsed:
        _note(
            notes,
            f"watchlist 里有 {unparsed} 行认不出商品 ID，已跳过"
            f"（点「整理清单」可按规范格式重写）",
        )
    return entries


def save_watchlist(path: str, items, notes=None) -> int:
    """按规范格式原子写回清单，返回被跳过的条目数。

    items 为 [(clusterId, 商品名)]，顺序即写入顺序；名称沿用文件里已有的值。
    条目形状不对/ID 不是纯数字的跳过不写；名字里的 "|" 和换行会被清洗掉
    （否则会写出一行坏清单，下次读进来就全乱了）。
    写前留一份 path + ".bak" 备份（只留第一次的），并用临时文件 + 替换避免写坏。
    """
    lines = list(_HEADER) + [""]
    skipped = 0
    for item in items or []:
        cluster_id, name = _split_item(item)
        if cluster_id is None:
            skipped += 1
            continue
        name = _clean_name(name)
        lines.append(f"{cluster_id} | {name}" if name else cluster_id)

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


def _split_item(item):
    """拆开 (clusterId, 名称) 并校验 ID；形状不对返回 (None, "")。"""
    try:
        cluster_id, name = item
    except (TypeError, ValueError):
        return None, ""
    return _clean_id(cluster_id), name


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
