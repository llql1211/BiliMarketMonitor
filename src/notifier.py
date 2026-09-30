"""企业微信群机器人推送。

推送走企业微信自建群的机器人 webhook，形如：
    https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx-xxxx

发送就是一次 HTTP POST JSON，不需要登录态、不需要 access_token，跟项目
「免登录、无 Cookie」的约束一致。

这个模块只管「把一条 markdown 发出去」：什么时候发、发什么，由 app 决定
（见 app.FlushNotifications / BuildNotifyMarkdown）。没有通知要发的轮次里
它一个线程都不起，所以不需要常驻 worker。

不 import 本项目其他模块，保持 app → notifier 的单向依赖：消息里的商品名、
价格、链接都由调用方拼好再传进来。
"""

import re
import threading

import requests
from PyQt5.QtCore import QObject, pyqtSignal

WECOM_TIMEOUT = 10.0
WECOM_API_PREFIX = "https://qyapi.weixin.qq.com/"

# 成功时回给界面的一句话。文案归这儿定，app 只管显示（见 app.OnNotifySent）。
NOTIFY_OK_TEXT = "企业微信推送成功"

# 企业微信 markdown 消息体上限约 4096 字节，超了接口直接报错拒收
WECOM_MAX_BYTES = 4096

_KEY_IN_URL = re.compile(r"key=[^\s&'\"]+")


def _redact_webhook(text):
    """把夹在错误文案里的 webhook key 抹掉。

    requests 的网络异常会把整个 URL 抄进消息里（`...with url: /cgi-bin/webhook/send
    ?key=xxx`），企业微信的 errmsg 有时也回显地址；而这些文案要打到控制台、还要
    进状态栏。webhook 等同于发消息的权限，不能因为「报个错」就漏出去。
    """
    return _KEY_IN_URL.sub("key=***", text)


class NotifyError(Exception):
    """统一的推送错误出口。"""


def _post_wecom(webhook_url, markdown, timeout=WECOM_TIMEOUT) -> None:
    """向企业微信 webhook 发送一条 markdown 消息；失败抛 NotifyError。

    纯函数，不依赖 Qt，最好测。调用方负责起线程。

    企业微信的规矩是 HTTP 200 只代表消息送到了，业务上成没成要看响应体里的
    errcode，所以两道都判：状态码不是 200 报 HTTP n，errcode 不是 0 就把它
    自己的 errmsg 报出来——那种响应才是最常见的（key 写错、机器人被停用）。
    """
    if not isinstance(webhook_url, str) or not webhook_url.strip():
        raise NotifyError("webhook 为空")
    webhook_url = webhook_url.strip()
    if not webhook_url.startswith(WECOM_API_PREFIX):
        # 配置层已经校过一遍，这里再兜一道：发消息的地方不该把内容 POST 到别处去
        raise NotifyError("webhook 不是企业微信地址")
    if not isinstance(markdown, str) or not markdown.strip():
        raise NotifyError("消息内容为空")

    try:
        resp = requests.post(
            webhook_url,
            json={"msgtype": "markdown", "markdown": {"content": markdown}},
            timeout=timeout,
        )
    except requests.RequestException as err:
        raise NotifyError(f"网络请求失败: {_redact_webhook(str(err))}") from err
    except Exception as err:  # 代理层/编码之类的异常也归口成 NotifyError
        raise NotifyError(f"请求异常: {_redact_webhook(str(err))}") from err

    if resp.status_code != 200:
        raise NotifyError(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as err:
        raise NotifyError("响应不是合法 JSON") from err
    if not isinstance(data, dict):  # 接口返回列表/数字时下面的 .get 会直接崩
        raise NotifyError(f"响应不是 JSON 对象（{type(data).__name__}）")
    if data.get("errcode") != 0:
        # errmsg 由对方给，也可能把地址回显出来，一样过一遍
        raise NotifyError(
            f"webhook 返回 errcode={data.get('errcode')}, "
            f"errmsg={_redact_webhook(str(data.get('errmsg')))}"
        )


class Notifier(QObject):
    """企业微信推送：一次发送一个小 daemon 线程，结果经信号回主线程。

    仿 ImageFetcher 的模式：大多数轮次根本不发通知，独立线程天然「不发就不起
    线程」，比养一个常驻 worker 省事。

    失败不重试——推送晚到一分钟本来就没意义，报一声让人自己去看表格更实在。
    结果只发信号、不弹窗：失败由 app 记到状态栏（见 app.OnNotifyFailed）。
    """

    sent = pyqtSignal(str)  # 成功摘要
    failed = pyqtSignal(str)  # 失败原因

    def send(self, webhook_url, markdown) -> None:
        threading.Thread(
            target=self._work, args=(webhook_url, markdown), daemon=True
        ).start()

    def _work(self, webhook_url, markdown):
        try:
            _post_wecom(webhook_url, markdown)
        except NotifyError as err:
            self.failed.emit(str(err))
            return
        except Exception as err:  # 兜底：子线程里漏出去的异常没人接，连提示都没有
            self.failed.emit(f"推送异常: {err}")
            return
        self.sent.emit(NOTIFY_OK_TEXT)
