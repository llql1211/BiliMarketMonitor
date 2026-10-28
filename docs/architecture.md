# BiliMarketMonitor 技术文档

[说明文档](../README.md) | 技术文档

面向开发/维护者的文档：代码怎么组织、数据怎么流、坑在哪、改一处该动哪几个文件。

## 设计约束

这几条决定了后面所有的取舍，改动前先认一下：

- **免登录、无 Cookie**。数据来自商品详情页前端自己调的那个接口
  `https://mall.bilibili.com/mall-search-items/items_detail/cluster_info`，
  没有账号态，也不需要签名。
- **私有接口，无文档、无频率承诺**。默认轮询间隔保守取 2 秒、依次（非并发）请求，
  间隔本身可调（`poll_interval_seconds`，商品多时调小）；字段则随时可能变，
  解析层对每条取值路径都判空，字段没了就显示 `—`，不报错。
- **网络随时可能很差**（代理、VPN、坏掉的 v6 路由——都真实发生过）。所以首屏不依赖网络：
  价格先用缓存里上一轮的，缩略图先用 `data/thumbs/` 里那份，网络回来的慢一点也不影响已经显示的东西。
  出网一律走 `net.py` 的常驻连接，不逐次重握手。
- **只监视，不下单**。不碰登录态，不碰交易。
- **非官方工具**，与哔哩哔哩官方无关。
- **许可 GPL-3.0-only**。[pyproject.toml](../pyproject.toml) 里有一行注释记着原因：
  PyQt5 是 GPL / 商业双许可（不是 LGPL），所以分发时整个程序必须 GPL-3.0。
  哪天换掉 GUI 框架，这条要重新评估。

## 目录结构

```text
BiliMarketMonitor/
├── pyproject.toml      # 依赖声明 + pixi 环境/任务 + pytest 配置
├── data/               # 数据文件（用户自己的那几个不入库）
│   ├── config.example.toml     # 配置默认值兼格式参考（随仓库分发）
│   ├── config.toml             # 个人配置（可选，不入库）
│   ├── config.toml.bak         # 设置窗口第一次写配置前留的备份，只留那一份（不入库）
│   ├── watchlist.example.txt   # 监视清单示例（模板）
│   ├── watchlist.txt           # 监视清单（用户维护，程序会规范化写回，不入库）
│   ├── watchlist.txt.bak       # 写回前留的备份，只留第一次那份（不入库）
│   ├── cache.db                # 本地缓存（首次运行自动生成，不入库）
│   ├── cache.db.bad-<时间戳>    # 缓存损坏时的备份（自动改名留档，不入库）
│   └── thumbs/                 # 缩略图磁盘缓存（自动生成，可随手删，不入库）
├── src/                # 应用代码，11 个模块（见下）
├── tests/              # pytest 测试，界面走 offscreen（见「测试」）
├── tools/              # 开发/维护工具，不随程序分发（见 tools/README.md）
├── docs/               # 本文档，以及 README 用的截图
│   └── images/
├── dev_notes/          # 开发笔记与踩坑记录，命名 YYMMDD_主题.md
└── tmp/                # 临时脚本与产物，用完即删，不入库
```

## 模块职责

模块之间是**单向**依赖：`app` 认识下面所有模块，下面谁都不认识 `app`。
`links` 借了 `parser.price_number`，是唯一的横向依赖（判「最后一段是不是价格」得跟界面同一个口径）。
`settings` 是 `app` 与 `config`/`theme` 之间的那一层 UI：窗口只管摆、收、校验，
写文件和"改完怎么生效"都留在 `app` 里（见下面「设置改完怎么生效」），所以它能单独测、单独出图。

| 模块 | 职责 | 主要出口 |
| --- | --- | --- |
| [src/app.py](../src/app.py) | PyQt5 主窗口、表格与各对话框、轮询调度、抓取总结 | `MainWindow`、`PollerThread`、`ImageFetcher`、`main()` |
| [src/client.py](../src/client.py) | 调用接口，失败统一成一个出口 | `fetch_cluster(cluster_id, timeout)`、`ApiError` |
| [src/parser.py](../src/parser.py) | 解析响应：名称/价格/均价/成交/图片，相对时间换算 | `parse_cluster(resp)`、`parse_error(err)`、`age_bounds(text)` |
| [src/links.py](../src/links.py) | 监视清单读写：解析各种写法、清洗、规范化回写 | `load_links(path)`、`save_watchlist(path, items)`、`build_detail_url(...)` |
| [src/config.py](../src/config.py) | 配置读取、逐项校验与写回 | `load_config()`、`save_config(changes)`、`check_value(key, value)`、`data_dir()`、`DEFAULTS` |
| [src/settings.py](../src/settings.py) | 设置窗口：把配置项摆成三页表单，收值与校验 | `SettingsDialog`、`FIELDS`、`changed_values(values, original)` |
| [src/net.py](../src/net.py) | 出网的唯一接缝：按线程复用 `requests.Session`，别处不再直接 import requests 发请求 | `net.get(url, timeout)`、`net.post(url, **kwargs)`、`session()` |
| [src/store.py](../src/store.py) | SQLite 缓存：随清单增删、损坏自愈 | `Store`（`sync` / `upsert_item` / `get_all` / `cached_deals`）、`default_db_path()` |
| [src/theme.py](../src/theme.py) | 深浅两套 QSS、系统深浅色判断、各处配色 | `apply_theme(app, dark)`、`qss_for(dark)`、`system_uses_dark()` |
| [src/thumbs.py](../src/thumbs.py) | 缩略图磁盘缓存：按图片地址存取、原子写、按数量上限清理 | `load(url)`、`save(url, data)`、`cache_dir()`、`path_for(url)` |
| [src/notifier.py](../src/notifier.py) | 企业微信群机器人推送：只管把一条 markdown 发出去 | `Notifier`（`send` / `sent` / `failed`）、`_post_wecom(...)` |

几点值得单独记住：

- **每个模块都自己检查输入**，不依赖调用方「保证」传进来的是合法数据。边界上（用户手写的文件、
  无文档的响应、拼出来的 URL）尤其如此。
- **异常一律降级 + 留痕**，不往上抛、不弹窗打断。`store` 和 `links` 把这次操作的异常情况攒在
  各自的 `notes` 里，`app` 收上去统一在状态栏说一句。详见「降级与留痕」。
- **出网只有一条接缝 `net.py`**。别处一律 `net.get` / `net.post`，不直接 `requests.get`：
  每次现开一条连接都要重建 TCP + TLS 握手外加一个 `SSLContext`（本机实测约 20ms CPU），
  网络不好时（代理、坏掉的 v6 路由）这 20ms 会放大成十几秒；`Session` 按线程复用一份就没这问题。
  顺带把「测试怎么拦网络」也收成一个点——补 `Session.request` 一处即可，见「测试」。
- **`notifier` 不 import 本项目任何模块**，连 `config` 都不认识——webhook 地址、要发的正文
  都由 `app` 拼好传进来。这样依赖箭头只有 `app → notifier` 一条，`_post_wecom` 也能当纯函数测。
  代价是「企业微信地址前缀」在 `config._WECOM_PREFIX` 和 `notifier.WECOM_API_PREFIX` 各留了一份，
  由 `tests/test_notifier.py::test_webhook_prefix_matches_config` 盯着两边别写岔。
- **网页地址（webhook）是敏感信息**：只在 `data/config.toml` 里出现（`config.example.toml` 给的是空串），
  不入库、不写进缓存；出错文案里可能夹着它（requests 的网络异常会把整条 URL 抄进去），
  所以 `notifier._redact_webhook` 会把 `key=` 后面那段先抹掉再往外说。
- **测试面**：`parser` 和 `links` 是纯函数，最好测；`app` 有 3300 多行、逻辑最重，测试也最厚
  （`tests/test_app.py` 5500 行）；改这两处时对应测试一起改。
  `notifier` 的发送链路全 mock，测试一个字节都不出网。
  `settings` 只摆表单、不认识 `app`、不碰文件，所以能单独造出来测（`tests/test_settings.py`），
  也能被出图工具直接当控件渲染。

## 数据流

```text
data/watchlist.txt
      │  links.load_links()          解析 / 去重 / 编码回退 / 规范化
      ▼
   [LinkEntry]  ──►  store.sync()     缓存严格跟随清单：缺的补、多的删
      │                    │
      │                    ▼
      │           把已知的名字、缩略图、上次价格先摆上表格（不用等网络）
      │
      │  点「开始抓取」（或 F5）／自动抓取到点
      ▼
  PollerThread（子线程，依次、非并发）
      │  client.fetch_cluster()      发请求，失败重试 1 次
      │  parser.parse_cluster()      判空提取字段
      ▼
   信号回主线程 ──► 刷新表格 + store.upsert_item() 写缓存
                    ├► links.save_watchlist()  学到了新名字才回写清单
                    └► OnPollFinished
                         ├► 有失败的行？补抓一遍（StartRetryPass），收尾顺延到那一轮
                         └► 收尾：挂下一拍自动抓取
                              └► FlushNotifications()  攒下的通知合成一条，交给 notifier
```

- **配置读一次就存在 `MainWindow.config` 里**（`__init__` 里的 `config.load_config()`），
  之后拼详情页链接、轮询间隔、重试间隔、超时、高亮阈值与颜色都现读它。
  手改 `config.toml` 要重启程序才读得到；走设置窗口则当场重读一遍，见下面一条。
- **自动抓取和手动抓取走同一条路**：`OnAutoPoll` 只是按 `auto_poll_scope` 从 `self.rows`
  里挑出一批任务，然后调**同一个** `StartFetch`，之后的事（线程、进度、写缓存、回写清单）
  完全没有分叉。唯一的区别是 `MainWindow.fetch_source` 记着这轮是谁发起的，
  它决定状态栏的措辞（`查询中 3/12` / `自动抓取（收藏）3/12`）和跑完弹不弹总结。
- **推送在 `OnPollFinished` 里发，但和「总结」不是一回事**：总结在用户中途「停止抓取」的那一轮
  不弹（半份数据容易让人误会），而已经观察到的补货、到价都是真的——所以 `FlushNotifications()`
  挂在 `poller_stopped` 那个分支**之前**，该推的照推。
  `MainWindow.notify_queue` 只由触发逻辑填（判据见「几个关键判定 → 通知什么时候发」），本轮没攒下东西
  或总开关关着就一个线程都不起；`last_notify_at` 管限流，间隔之内不重新计时也不丢队列。
- **清单是唯一的跟踪标准**，缓存只是加速层。所以缓存丢了、坏了都能重新抓回来，
  而清单里的东西是不可再生的（尤其是预期价，那是抓不回来的用户意图）。
- **每个改动了清单的动作都会立刻写回**：添加、删除选中、拖动排序、双击改预期价、
  单击「收藏」列、「刷新列表」，以及抓取结束时学到了新商品名。写回失败会弹提示，不静默；
  界面上已经改掉的那格会跟着回滚（点亮的星变回空心），不留「界面上收藏了、文件里没有」
  这种只在下次启动才暴露的分歧。
  「暂停/停止抓取」不写回清单（抓了一半的进度只留在表格和缓存里）。
- **「刷新列表」兼管清单的规范化**：它先重读文件、再按规范格式写回，所以手工编辑
  完点它就够了，不需要一个单独的「整理清单」。但只在**全部行都认得出**时才写——
  写回是按解析结果重排的，有一行认不出就会被写没，判据是 `MainWindow.last_unparsed`。
- **缓存里还有一份「用户意图」不走抓取这条路**：`ack_*` 那几列（上次确认时的样子）跟
  `favorite` 并列，只由用户按「确认」和「第一次抓到」写，见「几个关键判定 → 累积确认的基线」。
  抓取这条流水线的每一棒都不碰它，所以它跟价格快照一样能跨轮攒、跨重启留着。
- **缩略图比价格多一层磁盘缓存**，走的是另一条支线：`SetThumbnail(row, url)` 先看内存
  `icons`，再看 `data/thumbs/`，两边都没有才排进 `ImageFetcher` 的队列；同一张图被几行共用
  只下一次（回来的 key 是地址，贴之前按 `row_images` 核对行号）。判据与上限见
  「清单与缓存 → 缩略图」。

### 设置改完怎么生效

```text
按「设置」──► settings.SettingsDialog(值, 主题, parent)    只摆、只收，不碰文件：窗口开着时
                    │                                      改什么都不生效，取消就等于没开过
                    ▼  按「确定」（校验不过就停在窗口里，翻到出错那页）
              settings.changed_values(收上来的值, self.config)   只挑改过的项
                    │
                    ├► config.save_config(changes)   原地改那几行，注释与排版不动；
                    │                                第一次写前留一份 config.toml.bak
                    ▼
              config.load_config()  ──►  MainWindow.ApplyConfig()
                    │
                    └► SwitchTheme(值里的主题)   只有勾了主题才走，换外观 + 记进缓存库
```

- **两条腿生效**：多数项是"用到的时候现读 `self.config`"，重读一遍就是新值；
  只有启动时算好存下来的那几处要 `ApplyConfig` 重算——成交高亮的秒数与画刷、
  写在提示里的两个参数（抓取间隔、详情页模板）、自动抓取的定时器与顶部状态字。
  重画表格是为了让高亮的改动看得见，选中态按商品搬回去。
- **窗口里不预览**：勾主题、改数字都只停在窗口里，等按下「确定」才由主窗口一次做完，
  「取消」不用回滚任何东西。用户要的就是这个手感——勾一下就把整个界面先变一半太晃眼。
- **正在跑的那一轮不动**：`PollerThread` 手里的任务在开跑时就定下了参数，改设置影响的是下一轮。
  这也是按钮提示里那句「正在跑的那一轮仍用它开始时的参数」的来由。
- **主题记在缓存库、不在 config.toml**：`store` 的设置表里存 `theme`，所以它由
  `SwitchTheme()` 单独记一笔（换外观 + 存一行），`changed_values()` 永远不把主题写进配置文件。
- **写回是"只改改过的"**：`config.save_config()` 是逐行替换，没动过的行（包括注释）原样留着。
  这样设置窗口不把 `config.example.toml` 的默认值抄进用户文件，也不改写用户自己的注释。
- **写不进去就一样都不生效**：`save_config` 抛 `OSError`（只读、盘满）时弹一句提示就返回，
  配置与主题都还停在原样——只改内存会让表格和文件对不上，
  和删清单/写清单失败时的回滚是同一个道理。

## 线程模型

界面永远不假死，靠的是「网络活儿全在子线程，结果只经信号回主线程」这一条。

- **`PollerThread(QThread)`**：拿到一份任务清单后**依次**请求，每条结果 emit 一个信号回主线程。
  - **暂停**在两条商品之间生效，不打断正在进行的请求；**停止**是让当前这次抓取提前收尾，
    已经抓到的结果保留在表格和缓存里。
  - 相邻请求之间按 `poll_interval_seconds` 睡，单个商品失败后按 `retry_interval_seconds`
    等一会儿重试 1 次；重试仍失败只影响这一件，其余照抓。
  - **一轮跑完，失败的那几件再补抓一遍**（`StartRetryPass`）：单条那次重试是**背靠背**
    发出去的，网络抖一下两次一起撞上就整轮挂着失败；跑完一轮再从头抓这几件，中间隔着
    几十秒起步，捞回来的概率大得多。补抓也是一个 `PollerThread`，只装失败的那几行，
    进度显示「补抓失败的商品 3/5」，收尾（挂下一拍自动抓取、发通知、弹总结）留给补抓
    跑完的那一次——`OnPollFinished` 里起跑成功就直接 return。**只补一轮**，失败数按补抓
    之后的结果现算（`run_failed_rows`），总结那句「其中 N 件查询失败」才对得上；中途
    「停止抓取」的那轮不补，用户要的就是停下。
  - 一轮正常抓完 emit 结束信号，主线程弹抓取总结：这一轮抓了多少件、花了多久，
    再加上涨跌 / 新成交 / 到价三块明细。**中途停止的那一轮不弹**
    （只抓了一半，拿半份数据当总结容易让人以为其余商品都没变）。
  - 总结里的「用时」是墙钟时间刨掉暂停的那段（`MainWindow.RunElapsed`）：暂停是用户
    自己按的，算进去会让人以为抓取本身就那么慢。暂停时长在 `OnTogglePause` 里攒，
    收尾时定格成 `run_elapsed`——总结窗口换主题要重渲染，那时不能再算一遍。
- **`ImageFetcher(QObject)`**：拉图不走轮询线程，固定 8 个常驻 daemon worker 从一个队列里取活儿，
  下好一张 emit 一张。**不是每张图起一个线程**：几百个线程各自跟同一台 CDN 重新握手，
  网络一抖就互相拖成十几秒才回来第一张（本地对照：200 个请求不限并发 1176ms，8 个 worker
  536ms，32 个反而 1056ms）。用自建线程而不是 `ThreadPoolExecutor`，因为后者的线程不是 daemon，
  退出时会把正在跑的下载等完，关窗口就卡在那儿。
  - 信号里带的是调用方自己认的 key。**缩略图用图片地址**（同一张图可能有好几行在等，地址天然
    就是它的身份；用行号的话下载期间表格一重排就会贴到别的行上），双击预览用请求号。
  - 同一个 `(key, url)` 已排队或在飞就不再排第二次——重画表格会把每张图再要一遍。
  - 下载失败发 `failed` 而不是静默丢弃——大图下不下来得跟用户说一声；缩略图那边只拿它清掉
    等待表（不清的话这张图一直挂着「有人在等」，一次闪断就永远留白），仍然不吭声。
  - `stop()` 在 `closeEvent` 里叫：收尾之后才回来的那几张不能再往已销毁的窗口发信号
    （槽里抛异常在 PyQt 下是直接 abort 进程的），排着队的那些也就此丢掉。
- **`auto_poll_timer(QTimer)`**：自动抓取的节拍器，跑在**主线程**，它不是线程、也不发网络请求，
  到点只是在主线程里把一批任务交给 `PollerThread`——网络那部分照旧在子线程。
  - **单次触发 + 每轮收尾重挂**，不用周期定时器。周期定时器每一拍都不管上一轮跑没跑完，
    撞上正在抓取的那一拍就得额外写「跳过这一拍，但别忘了挂下一拍」；单次触发的语义天然就是
    「上一轮结束到下一轮开始」，间隔因此从上一轮**结束**算起，慢轮次只会把下一轮往后推。
  - **重挂点在 `OnPollFinished`，三条收尾路径都要经过**：正常跑完、学到了新名字、用户中途
    「停止抓取」。定时器是单次触发的，这一拍用完就空了，漏挂一次自动抓取就**悄悄停了**——
    尤其是用户按「停止抓取」那次，他只是不要这一轮，不是不要整个功能。
  - **正在抓取时不重挂**：`OnAutoPoll` 撞上 `IsPolling()` 就直接 return，等正在跑的那轮收尾
    时统一挂。范围里一件商品都没有时也不发请求，写一句状态栏提示就重挂，否则会变成空转。
  - **关掉时 `ScheduleAutoPoll()` 什么都不做**，定时器就此空着，`closeEvent` 里再 `stop()`
    一次收尾。
- **`Notifier(QObject)`**：推送跟拉图一个路子，**一次发送一个 daemon 线程**，
  发完 emit `sent` / `failed` 回主线程。大多数轮次根本没通知可发，于是天然「不发就不起线程」，
  不必养一个常驻 worker。发送失败不重试（晚到一分钟的提醒本来就没意义）。
  - `send()` 只负责起线程就返回，界面不等它；`_work()` 里把异常全兜住——子线程里漏出去的
    异常没人接，连提示都没有。
  - 结果不回弹窗，只写状态栏：成功那句由 `notifier` 自己给（`NOTIFY_OK_TEXT`），
    失败由 `app.OnNotifyFailed` 加个前缀、在控制台打一行留痕。
  - **HTTP 200 不等于发出去了**：企业微信的失败是包在响应体里的（`errcode != 0`），
    所以 `_post_wecom` 必须把 `errcode` 也验一遍。
- 跨线程的信号是**排队投递**的，所以测试里光 `sleep` 等不到，得转事件循环
  （`tests/conftest.py` 的 `wait_until` / `join_thread` 两个 fixture 就是干这个的）。

## 清单与缓存

**清单 `watchlist.txt`**（`links.py`）

- 格式 `<clusterId> | <商品名> | <预期价> | <收藏>`，四段都可省。读入时兼容分享链接（含
  `share_medium`、`bbid` 之类无关参数）和纯数字 ID，纯数字之外还认 `#` 注释行与空行。
- 商品名未知时只写 ID；没设预期价的行不写那一段——免得老清单被一堆空尾巴撑开。
  收藏段追加在末尾，所以「收藏了，但没名字也没价」写出来是 `1001 | *`，读回来照样对得上。
- 收藏那一段固定写一个 `*`。认的判据是**最后一段整个就是 `*`**：名字本身以星号结尾
  （「限定版*」）不算收藏；反过来，手工在名字后面补一句 `| *` 就会被读成收藏。
  剥收藏要**先于**认预期价——`名字 | 50 | *` 剥掉末段之后，`50` 才是那个价格。
- 写回是**原子**的：先写 `.tmp` 再 `os.replace`；覆盖前留一份 `.bak`（只留第一次那份）。
- 名字和预期价里的 `|`、换行会被清洗掉，否则会写出一行坏清单，下次读进来全乱。

**缓存 `cache.db`**（`store.py`）

- `items` 表一列一件商品，列定义集中在 `ITEM_COLUMNS`——建表和「给老库补列」都从它生成，
  免得出现「新建的库有这列、老库没有」这种只在别人机器上炸的毛病。
  加字段就往这里加，老库由 `_ensure_item_columns()` 自动补上，不用手动删库。
- 成交也存（`deals_json` + `deals_updated_at`）。一份数据两个用处：下次启动**直接显示上一轮
  抓到的这几条**（免得首屏三格全是 `—`），以及抓取时**认出「哪几条是上次就有的」**，见下两节。
  存的是原始的说法（`time` 就是「9小时前」那个文本）加抓取时刻，显示时再按「抓到现在过了多久」
  重算一遍。
- **同步以清单为准**：清单里有、缓存里没有的补上，缓存里有、清单里没有的删掉。
  这条语义很硬——早期正是它把用户的 `cache.db` 清空过（清单为空时），
  所以测试专门有个看门狗 fixture 盯着真实 `data/`，见「测试」。
- `favorite`（收藏）是缓存里**两份「不来自抓取」的数据**之一（另一份是 `ack_*`，见下一条）：
  价格、均价、成交、史低价都是抓来的，它只从清单来。所以能写它的口子只有两个——`sync()`
  跟着清单整体覆盖、`set_favorite()` 改一条；
  `upsert_item()` 一个字都不碰，抓取结果回来不会把用户打的星冲掉。传进 `sync()` 的二元组
  （只给 ID 和名字的老写法）按「未收藏」算，也就是会把缓存里那颗星**取消**掉——清单是正本，
  这条没有例外，取消收藏因此不会被下一轮 sync 悄悄还原。
- `ack_*` 那七列（`ack_price_text` / `ack_price_value` / `ack_sold_out` / `ack_lowest_price` /
  `ack_deals_json` / `ack_deals_updated_at` / `ack_at`）是**第二份用户意图**，跟 `favorite` 并列：
  抓取一个字都不碰（`upsert_item()` 的列清单里没有它们），能写它们的只有两处——`acknowledge()`
  （用户按「确认」）和 `init_ack_if_empty()`（第一次抓到这件商品）。区别是确认**全局**生效：
  一次把所有行的基线刷到当前，不做逐件确认。判据见「几个关键判定 → 累积确认的基线」。
- `lowest_price`（史低价）是缓存里**两条不可再生**的记录之一（另一条是累积确认的基线）：
  价格、均价、成交都是「抓一次就回来」的快照，它却是历次抓取攒出来的。所以删库、重建、
  把商品移出清单再拖回来，都会把它丢掉且补不回来（只能从下一轮抓取重新开始攒）。
  这条在 README 的「说明」里对用户讲明了。
  落库用的是普通的 `COALESCE`——传 `None` 是「这轮没有新记录」，不是「清掉记录」，
  跟同表里价格那组「传了覆盖、传 `None` 保留」的快照语义正好对上，只是判据在调用方手里。
- 自愈三档：库文件损坏 → 改名成 `cache.db.bad-<时间戳>` 后重建空库；目录不存在 → 自己建；
  连库都开不了（权限之类）→ 退到内存库，本次运行照常可用。全都记进 `notes`。

**缩略图 `data/thumbs/`**（`thumbs.py`）

- 文件名是**图片完整地址（含 CDN 裁剪后缀）的 sha1 前 16 位** + `.img`，内容就是下载回来的原始字节。
  地址带后缀是有意的：`IMAGE_SIZE` 一改就是另一个地址，旧盘上那份自动作废，不用另写版本号。
- 三层缓存各管一段：内存里的 `icons`（表格重画）、`pending_images`（同一张图只下一次）、磁盘（**重启**）。
  第三层是为了网络抖动时「下过的图照样立刻能看见」——实测 225 行、盘里都有时，图标在
  `LoadWatchlist` 里就贴完了（约 0.2s，零网络请求），没有这层要等 2.5s。
- 写入是**先 `.part` 再 `os.replace`** 的原子写，读到坏文件（`OSError` 或解不出图）一律当没有，
  退回网络重下；空字节不落盘——失败的那次不该在盘上留个空壳，否则下次启动会一直拿着它当缓存。
- 上限 `MAX_FILES = 2000` 个，每存 100 个按 mtime 清一次最旧的（只清理 `*.img`，别的手写文件不动）。
  整个目录**可以随手删**，代价只是下次启动重下。

## 几个关键判定

**成交时间 → 年龄区间**（`parser.age_bounds` / `parse_relative_time`）

接口给的是「9小时前」这种人话，只能粗判新旧。单位表在 `parser._RELATIVE_UNITS`。
认不出的格式（绝对日期、错别字）返回 `None`：照旧显示原文，只是不高亮——
宁可少标一处，也不拿猜出来的新旧误导人。

**哪些算「新增成交」**（`app.new_deals` / `_aged_from`）

拿这次的成交跟上次的比，上次没有的才算新增。难点在于**同一条成交过一会儿就渲染成
「10小时前」了**，光比文本会把老成交一遍遍报成新的。所以判据是：

> 价格相同，**而且**它现在所在的年龄区间，跟「老记录放老一段时间后该处的区间」有重叠。

比的是**区间不是点**：「5小时前」是一小时宽的一格，隔几分钟再抓原文一个字都不会变，
拿一个数去套就会把老成交报成新的。`DEALS_MATCH_GRACE_SECONDS = 60` 是给这个比对留的宽限。
基准来自缓存里的上次成交快照，所以重启之后接着比，不会把上次那几条又报一遍。

**首屏拿缓存里的成交顶上**（`app._reaged_deals` / `_elapsed_since` / `store.cached_deals_of`）

启动时没有新数据，成交1/2/3 就先显示**上一轮缓存下来的**那三条，写法与抓到的一样。年龄是
「原文的年龄 + 抓到现在过了多久」重算的（`parser.format_age` 是 `parse_relative_time` 的反方向），
否则放一晚上的程序第二天打开还写着「9小时前」。两条规矩：

- 缓存里那条成交的原文认不出年龄 → **原样显示，也不高亮**，不猜。
- 这组成交**认不出是什么时候抓的**（没有 `deals_updated_at`）→ 相对说法一律不留（留着就是错的），
  只显示价格。

这一轮真的抓到数据后，成交一律以**新抓到的**为准，即便新数据是「没有成交」也不回退到缓存——
留着上次的会让空货架看着像有货。失败那行除外，那种情况保持缓存值（跟现价同一个口径）。

高亮用的年龄是**重算后的精确秒数**，显示的文本是它的向下取整说法，所以那个说法的区间
一定盖住高亮用的值——不会出现「写着 1小时前 却没加粗」这种自相矛盾。

**顶部「上次更新时间」取自哪**（`app.UpdateUpdatedLabel`）

窗口最上面那行说的是**这屏数据有多旧**。取的是各行缓存里最新的 `price_updated_at`，
而不是「本轮从几点开始抓的」：

- 那个时间戳跟那一行的价格是一起写进缓存的，所以「中途停止」「仅抓取新商品」之后
  显示的时刻天然就是对的，不用另外记一次本轮时间。
- 抓取失败的行不写价格，也就不刷新它。
- 一屏里一件都没抓到过，或者把唯一抓过的那行删了 → 显示「尚未抓取过」：
  屏幕上确实一件数据都没有了。

这行**自己占一行**，不跟按钮挤在一起：七个按钮已经占到 1265px（深色主题因样式表的内边距
是 1393），默认窗口才 1500；把它塞进按钮行会让窗口最小宽涨到 1557，Qt 只好连窗口一起撑大。
`test_updated_label_does_not_widen_the_window` 盯着这条。

**现价那一格怎么拼**（`app.price_change` / `split_price_text` / `PriceDeltaDelegate`）

一格里有三截：价格、涨跌（`↑2.01` / `↓6`，涨红跌绿）、剩余件数（` · 3件`，灰色）。
后两截是**自绘**的（`DELTA_ROLE` / `STOCK_ROLE` 这几个 role 挂着内容与颜色，重画时按当前主题重算），
因为一格里要三种字号和颜色。窄到画不下时件数退到悬停提示里，而不是跟涨跌叠在一起。

**剩余件数从哪来**（`parser._stock_count`）

货少的时候接口会把「当前价格还剩几件」写进购买按钮的文案，形如「最低价仅1件」，正则抠出来。
同一处还会出现「当前最低价」和「已售罄」两种**没带数字**的文案，那两种都当「没有件数可报」——
不凭空编一个件数出来。

**预期价到没到**（`app.expected_reached`）

- 判「设没设价」看**显示出来的那格**：只写了个 `￥` 不算设了价。
- 售罄的不参与比较（那一格的「现价」其实是原价）。
- 比的是存着的值，不是展示文本。
- 到价用**加粗 + 颜色**（`theme.expected_reached_color`），不只靠颜色——
  单靠颜色色觉障碍的人看不出差别，截图里也容易糊成一片。近期成交高亮同理。

**现价低于近 30 天均价**（`app.below_average`）

- 判「没得比」的口径与到价一致：售罄不比（那格的「现价」是原价），有一边认不出数字不比；
  **严格低于**才算，持平不算——持平就是均价那个水平，"比最近一个月划算"不成立。
- 命中给「近30天均价」那格**绿色 + 加粗**（`theme.below_average_color`，与跌色同一个色），
  跟到价同一个路子。这两格的判据里都有「现价」，所以每次重画都要重判：抓完一轮、
  换主题、退回缓存值，都可能翻转。

**史低价怎么维护**（`app.updated_lowest` / `app.at_lowest` / `theme.lowest_color`）

一条**只降不升**的记录：每轮抓到的现价跟存着的那条比，更低就刷新，涨回去也不改高。

- 挑哪次抓取算数落在 `updated_lowest`，判据与 `price_delta` / `below_average` 一致：
  **售罄那次不算**（接口给的是原价，不是市集现价），认不出数字的不算。
- 首次观察即基线：缓存里还没这条记录时，本轮抓到的价直接成为史低价（不做任何回填）。
  升级上来的老库因此是空的，从下一轮开始攒——这是有意的，拿旧的「现价」当史低是编数据。
- 存着的那条认不出数字，当「没有记录」处理，让本轮的价接管。
- 现价**等于**史低价（没比它贵）时，「史低价」那格绿色加粗（`theme.lowest_color`，
  与跌色同一个色）。判据 `at_lowest` 同样排除售罄与认不出的数字，
  并跟到价、低于均价一样每次重画都重判。
- **抓取开始时不清它**：`--` 清的是「本轮的数」——现价、原价、均价；史低价是长期事实，
  不属于本轮，清了就等于把这行攒下来的记录抹掉。`test_clearing_prices_keeps_the_lowest` 盯着这条。
- 比较放在 `app` 而不是 `store`：`store` 只按 `COALESCE` 存递进来的值，不 import `parser`，
  维持「模块单向依赖、只有 `links` 借 `parser`」这条线（见「模块职责」）。

**通知什么时候发**（`app.NotifyTags` / `FlushNotifications`）

判定在 `OnResultReady` 里做（结果一到手就判，跟写缓存同一处），发送在 `OnPollFinished`
收尾时做。五类触发各由一个 `notify_on_*` 开关控制：

| 标签 | 判据 | 边的来源 |
| --- | --- | --- |
| `恢复在售` | `price_change` 的 `kind == CHANGE_ON_SALE` | 本身是边沿 |
| `低于预期价` | `expected_reached` 且这次是**刚**跌破：拿 `previous` 里的价现算一次上一次是否到价，只在 `上次 False → 这次 True` 时算数 | 状态，需自己比 |
| `创史低` | `updated_lowest()` 返回非 `None` 且收藏 | 本身是边沿 |
| `降价 ↓6` | `price_change` 的 `kind == CHANGE_DOWN` 且收藏 | 本身是边沿 |
| `低于预期价`（非收藏） | 同「到价」，只是商品没收藏 | 状态，需自己比 |

到价那条的开关按收藏与否分成 `notify_on_favorite_target` / `notify_on_any_target` 两个：
收藏的是你要盯的，没收藏的不该一样吵。

- **判据只认边沿，不认状态**：`at_lowest`（现价等于史低价）每轮都成立——那才是常态——
  拿它当触发就是每轮刷屏，所以创史低用 `updated_lowest`（只在刷新时返回非 `None`）。
  「一直低于预期价」同理，只在跌破那一刻推一次。
- **第一次观察不发通知**：缓存里没有上一轮的价格（`previous["price_text"]` 为空）时只记录状态。
  否则刚打开程序、刚加进清单的商品，一抓就是一串「到价」——那是记基线，不是新闻。
- **判定与写缓存共用一次计算**：`updated_lowest` 在 `OnResultReady` 里算一次，
  既递给 `store.upsert_item` 又用来判创史低；因为要跟「这次抓取之前」那份比，
  它必须排在 `upsert` 之前。
- **合并分两层**：同一件商品一轮踩中几条，标签用 ` · ` 连成一行（`恢复在售 · 创史低`）；
  一轮里多件商品在 `FlushNotifications` 里并成**一条**消息。一条消息装一轮的全部触发，
  而不是一件一条——手机上一串单条消息比一条长消息更难读。
- **节流靠 `last_notify_at`**：没到 `notify_min_interval_seconds` 就把队列留着，
  等下一轮再判。留下的不是「过期的边沿」：消息写的是「恢复在售」，晚一轮发也仍然是真的。
- **`last_notify_at` 初值是 `-inf` 不是 `0.0`**：`time.monotonic()` 是**开机以来的秒数**，
  取 `0.0` 的话，开机头一分钟里启动程序，第一次推送会被自己的限流挡掉——而边沿只发生一次，
  挡掉就是永远错过。测试 `test_first_push_is_not_throttled_by_uptime` 盯着这条。

**上一次总结留一份**（`app.ShowSummary` / `app.OnShowLastSummary`）

一轮正常跑完弹总结时，顺手把 `run_changes` / `run_deals` / `run_targets` /
`run_total` / `run_failures` / `run_elapsed` 这一组引用整体留一份到
`MainWindow.last_summary` 里，供顶部「查看上次总结」按钮再弹出来。

- **只留一份，不做栈**：用户要的是"刚才那轮"，回溯更远的去看表格就行。
  下一轮正常跑完时会整体替换掉这一份，没有清理逻辑，也不占地方。
- **只在那条会弹窗口的路径上录**：中途「停止抓取」不弹总结（半份数据当
  总结会误导），也就**不覆盖** `last_summary`——用户之前看过的那轮还在。
- **引用而不是拷贝**：`StartFetch()` 里那几个字段是整段重新赋值的（`self.run_changes = []`），
  旧列表整体被搁置，不会因为下一轮清空字段就跟着变空。
- **主题切换跟着走**：`SwitchTheme` 里那一句 `summary_dialog.SetDark(...)`
  已经覆盖到按「查看上次总结」弹出的那个窗口，因为 `summary_dialog` 始终
  指向当前打开的那一个。

**累积确认的基线**（`app.ack_change` / `ack_new_deals` / `ack_reached` / `BuildAckSummary`）

总结窗口里那块「自上次确认以来」报的不是这一轮，而是**从上次确认攒到现在**的全部变化。
实现上没有任何"合并历史"的逻辑：`ack_*` 那几列是不动的基线，每轮都拿同一根它重新比一遍，
上一轮报过的内容自然还在——攒多久取决于用户什么时候按「确认」，不取决于跑了几轮。

- **三个判据是本轮那三个的翻版**，判据口径一字不差，只是基准换成基线：
  `ack_change` 把基线包成 `previous` 直接调 `price_change`；`ack_new_deals` 把
  `ack_deals_json` + `ack_deals_updated_at` 当基准交给 `new_deals`（`elapsed` 是**那份成交快照
  抓回来之后**到现在的秒数）；`ack_reached` 在本轮到价的基础上再要求**确认时没到价**——它跟
  通知那边一样是边沿，不然确认过一次之后它会一直报。
- **成交快照的锚点不是确认时刻**：用户完全可能隔几小时才点确认，而快照里的「刚刚」说的是
  抓取那一刻。拿 `ack_at` 算 `elapsed` 会把那几小时漏掉，老成交像变年轻了似的，下一轮又被
  报成「新增」——这正是「已读之后下一次抓取还包含这次成交」那个 bug。所以 `acknowledge()` /
  `init_ack_if_empty()` 会单独记 `ack_deals_updated_at`（就是写缓存时那个 `deals_updated_at`），
  老缓存没这列时 `ack_new_deals` 退回 `ack_at`，行为跟升级前一样。
- **售罄、认不出数字一律不参与价格比较**，跟 `price_delta` / `expected_reached` 同规矩：
  售罄行的"现价"是原价，两边售罄就不比；确认时的价认不出来就退化成"不比"，
  不拿猜出来的数当基准。
- **首次观察即基线**（`store.init_ack_if_empty`）：缓存里这行还没有 `ack_at` 时，本轮抓到的样子
  直接记成基线，**这一轮不算变化**——跟涨跌、创史低"第一次抓到不算"是同一条规矩。
  判据取 `upsert` 之前那份 `previous`（`previous` 里有 `ack_at` 就说明已经记过），
  写完再回读一遍记录，让内存里的 `record` 带上新基线。
- **首次初始化不动全局时刻**：`last_ack_at`（`settings` 表里那行）只有 `acknowledge()` 写，
  所以刚加进来的商品不会把顶部那句从「尚未确认」变成「已确认」。
- **确认一个事务写完**（`acknowledge`）：基线、成交、史低和全局时刻同进同出。写一半会留下
  "这行的基线新、那行的基线旧"，之后的累积报告就开始胡说——所以连带它自己那个时刻一起
  回滚，绝不通过 `set_setting()`（那里嵌的 `with self.conn:` 会把外层事务提前提交）。
- **通知仍走 `previous`，不要改**：累积那套只给界面看，`NotifyTags` 判边沿用的是**上一轮**缓存
  里的值。拿 `ack` 顶替它会出事——用户按一次确认，等于把攒了几轮的历史一次性推上手机
  （本来"上次没到、这次到了"才推）。`test_confirming_does_not_change_the_notification_judgment`
  盯着这条。
- **抓取中不给确认**：那一轮正在写缓存，基线这时候刷到"当前"会把中间的变化吞掉。
  `SetBusy` 把按钮置灰，`OnAcknowledge` 自己再拦一道（快捷键之类绕过去的路）。
- **顶部那句看的是两样东西**（`UpdateAckLabel`）：有未确认项就是「未确认 N 项变化」（N 来自
  `BuildAckSummary`），没有则按 `last_ack_at` 给「尚未确认」或「已确认 · 3 小时前」。
  它跟「上次更新时间」一样在每个结果回来时刷新（`OnResultReady`），不只在收尾时刷。
- **窗口只管摆、收，不认识 `MainWindow`**：`SummaryDialog.acknowledged` 信号出去、
  `MainWindow.OnAcknowledge()` 干活（刷基线、重读记录、恢复选中、清掉窗口里那块），
  跟「确认」按钮不关窗是同一件事的两面——用户可能还想接着看本轮的明细。

**拖拽排序与自动滚动**（`app.ReorderableTable`）

表格只是顺序的视图：`startDrag` 自己起一个 `QDrag`（绕开 Qt 内部 Move 会搬格子、删源行那套），
拖动中画出落点横线，松手后只发 `rows_dropped(行号, 插入位置)`，真正改顺序的是
`MainWindow.OnRowsDropped` → `move_rows()` → 写回清单。

- **自动滚动自己做**：`setAutoScroll(False)` 关掉 Qt 自带的那套（自定义 `QDrag` 下它并不动），
  改成 `dragMoveEvent` 里按光标到上下边缘的距离算方向和速度，`QTimer` 每 30ms 滚一格。
  越靠边越快（`DRAG_SCROLL_MAX_STEP`），到顶/到底或回到中间就停；每次 tick 还用同一个光标
  位置重算落点，横线才会跟着滚动后的行一起挪。
- **停的地方要全**：`dropEvent`、`dragLeaveEvent`、`startDrag` 的 `exec_()` 返回之后都调
  `_stop_drag_scroll()`。漏一条，下一次拖到一半页面就自己滚起来。

**画格子的时机**：抓取开始时把价格清成 `--`，刷到哪行填哪行，一眼能看出进度。
`PENDING_TEXT`、`NO_DATA_TEXT`、`CLEARED_TEXT`、`FAILED_TEXT` 几个占位符在 `app.py` 顶部。

## 降级与留痕

异常一律**降级 + 留痕**，不弹窗打断。这是全项目的统一风格，加新功能时照着来。

| 情况 | 表现 |
| --- | --- |
| 接口响应结构变了（字段缺失、类型不对） | 该商品显示 `—`，鼠标悬停商品名看原因，其他商品照常 |
| `watchlist.txt` 被改成 GBK 编码 | 按本地编码读入并在状态栏说明，不是直接崩 |
| 清单里有认不出的行 | 跳过这几行并在状态栏提示，其余照常载入；这几行还在文件里，所以「刷新列表」这次不重写清单 |
| 商品名里有竖线或换行 | 写回清单前清洗，避免写出一行坏清单 |
| 缓存 `cache.db` 损坏 | 改名备份为 `cache.db.bad-<时间戳>` 后重建空库，状态栏提示；重抓即可 |
| 配置里写了 `inf` / `nan`、模板不是 http(s) 链接 | 该项退回默认值并打印一条警告，其他配置照常生效 |
| 成交时间是认不出的格式（如绝对日期、错别字） | 照旧显示原文，只是不高亮 |
| 配置里的高亮色认不出来（如 `orangejuice`） | 退回默认橙色并打印提示，不会静默变成「高亮没生效」 |
| 设置窗口里填了不合格的值（数字框里写了别的、颜色名不存在、webhook 不是企业微信的地址） | 窗口不关，底部列出「页签：原因」并翻到出问题的那一页；一项不合格就整份不写，不留半新半旧的配置 |
| 写 `config.toml` 失败（只读、盘满） | 弹一句提示，本次改的都不生效（主题也不换）；内存里的配置不动，省得跟文件对不上 |
| 推送发不出去（网络、webhook 被删、地址填错） | 控制台打一行带原因，状态栏在后面接一句「企业微信推送失败：…」；**不弹窗**，抓取结果照旧 |

留痕的路子是同一个：`store` 和 `links` 的公开操作都收一个 `notes` 列表（不传就只静默降级），
模块往里面 append，`app` 取走后拼成一句话送进状态栏。区别是 `store` 自己持有 `notes`
并在**每次公开操作时清空**，`links` 那个列表由调用方给、列表的生命周期也归调用方。

`config` 则更靠前一步：每个值单独校验，不合格的退回默认值 + 记一条 warning
（`inf` 会让抓取线程卡死，`nan` 会让间隔判断全部失效——都是能直接打爆接口的写法，
所以卡在配置这一层，不放进运行时）。

模块内的兜底值有**两层**：`config.DEFAULTS` 是「默认配置」，
`theme.DEAL_HIGHLIGHT_COLOR` 是「配置给的色认不出来时的最后一道」，各守一层，同值但别互相引用。

## 开发环境

需要 Python 3.11 或更高（读配置用到 `tomllib`）。

```bash
# 方式一：pixi，不用自己管 Python 版本
pixi run start          # 装环境 + 启动（环境过期会自动重装）
pixi run test           # 跑测试
pixi run shot           # 界面出图，见下

# 方式二：普通 Python
python -m venv .venv
source .venv/Scripts/activate     # Windows(Git Bash)；Linux/macOS 用 .venv/bin/activate
pip install -e ".[test]"          # 开发装可编辑 + 测试依赖
python src/app.py
python -m pytest
```

pixi 只是本仓库图省事选用的环境管理工具，程序本身不依赖它：`pyproject.toml` 里的
`[tool.pixi.*]` 几节删掉也不影响运行。反过来，pixi 不认 `[project.optional-dependencies]`，
所以测试依赖在 `[tool.pixi.feature.test.pypi-dependencies]` 里又写了一遍，两处要一起改。

本仓库的 Python 命令一律走 `pixi run python`（含临时脚本），不用裸 `python`。

## 测试

```bash
pixi run test           # 等价于 pixi run python -m pytest
```

测试有**三条规矩**，都落在 [tests/conftest.py](../tests/conftest.py) 里：

1. **不碰真实 `data/`**。`data_files` fixture 把程序眼里的数据路径整体指向 `tmp_path`
   （`config.data_dir` / `links._data_dir` / `store.default_db_path` 三处一起打补丁，
   必须在构造 `MainWindow` **之前**打）。另外有个会话级看门狗 `_guard_real_data`
   在整场测试前后比对真实 `data/` 的内容指纹，被改动过就 fail。
   *这条是为一次真实事故加的保险*：早期测试直接构造 `MainWindow`，
   `store.sync()` 的「以清单为准」语义把用户的 `cache.db` 清空过。
2. **不联网**。`requests.get/post` 与 `requests.Session.request` 一律换成抛 `ConnectionError`
   （后者是补的：出网改走常驻连接后，只堵前者拦不住了，见 `net.py`）；
   要造响应就用 `fake_client`，或者像 `tests/test_app.py` 那样直接换掉 `net.get`。
3. **不弹窗**。`QMessageBox` 的三个静态方法与 `webbrowser.open` 一律被拦下并记录，
   界面测试才能无人值守地跑（`msgboxes` / `opened_urls` fixture）。

界面测试跑在 Qt 的 **offscreen** 平台上，不需要显示器。关卡在 `import PyQt5` 之前落下，
所以本模块必须先于任何 PyQt5 导入执行。跨线程信号得转事件循环才收得到，
用 `wait_until` / `join_thread` 两个 fixture，别用 `sleep`。

| 测试文件 | 覆盖 |
| --- | --- |
| [tests/test_app.py](../tests/test_app.py) | 主窗口渲染与按钮流程、`PollerThread`、缩略图（内存/磁盘两层缓存、同一张图合并成一次下载、失败后重试、重排后不贴错行）、总结窗口、通知触发与合并、设置窗口的保存与即时生效 |
| [tests/test_client.py](../tests/test_client.py) | 请求形态与各类失败的统一出口 |
| [tests/test_config.py](../tests/test_config.py) | 配置优先级、逐项校验、单值校验与写回（只改传进来的项、保留注释） |
| [tests/test_links.py](../tests/test_links.py) | 清单解析、去重、规范化回写 |
| [tests/test_net.py](../tests/test_net.py) | 同线程复用一份 Session、不同线程各一份、超时透传，以及「不联网」那条规矩确实盖住了这条接缝 |
| [tests/test_notifier.py](../tests/test_notifier.py) | 企业微信报文、`errcode` 判定、地址校验与脱敏、发送线程回信号 |
| [tests/test_parser.py](../tests/test_parser.py) | 响应解析与全路径判空 |
| [tests/test_settings.py](../tests/test_settings.py) | 设置窗口：字段表覆盖了每一项配置、三页签、控件种类与位置、收值与拦错、勾主题不预览 |
| [tests/test_store.py](../tests/test_store.py) | 缓存随清单增删、设置项、损坏自愈 |
| [tests/test_theme.py](../tests/test_theme.py) | 样式表、占位文字配色、系统深浅色判断 |
| [tests/test_thumbs.py](../tests/test_thumbs.py) | 缓存存取与原子写、坏目录/坏文件一律当没有、按数量上限清理时不动别的手写文件 |

## 改了界面就出图

对话框、表格、主题的样式改动，跑一条命令渲染成 PNG，**肉眼确认**，别只读代码下结论：

```bash
pixi run shot           # 出图在 tmp/out/，每个场景深色/浅色各一张
```

走 Qt offscreen，比开真窗口快，不用显示器、不用人点，也不用等抓取跑完。
现有场景清单、怎么加场景、三个坑（`QApplication` 引用要留着 / Windows offscreen 没字体 /
样本要真调 `price_change()` 造）都在 [tools/shot.md](../tools/shot.md) 和
[tools/README.md](../tools/README.md) 里。

它和测试用的是**同一个出发点**：都得先把 `data/` 复制到沙盒再改数据路径
（理由同上，缓存库以清单为准）。改了沙盒那段，记得和 `tests/conftest.py` 的
`data_files` fixture 对着看，**别只改一处**。

## 常见改动的落点

| 想改什么 | 动哪里 |
| --- | --- |
| 官方页面改版、换链接模板 | `data/config.toml` 的 `detail_url_template`，不用改代码 |
| 接口响应结构变了 | `parser.parse_cluster()` 的取值路径与 `_dig()` |
| 增删一列、改列宽 | `app.py` 顶部的 `COL_*` 常量与 `HEADERS`，以及 `FillRow` / `SetPriceCells` 等填格函数 |
| 表格、对话框的外观 | 控件在 `app.py`，配色与 QSS 在 `theme.py`；改完 `pixi run shot` 看图 |
| 抓取节奏、超时、重试、高亮阈值与颜色 | 在程序里走「设置」窗口，或手改 `data/config.toml`（格式见 `data/config.example.toml`，代码兜底在 `config.DEFAULTS`） |
| 加一项配置（带上设置窗口里那一行） | 四处：`config.DEFAULTS`（默认值）、`config._validate`（校验）、`settings.FIELDS`（标签/提示/归哪一页）、`data/config.example.toml`（给用户看的注释）；漏了字段表 `tests/test_settings.py` 会报出来 |
| 设置窗口的排版、标签、提示语 | `settings.FIELDS` / `settings.SECTIONS`；改完 `pixi run shot settings` 出图看一眼 |
| 设置改完"当场生效"的范围 | `app.ApplyConfig()`：启动时算好存下来的那几处都在里头（高亮秒数与画刷、两处提示、自动抓取定时器） |
| 缓存要多存一个字段 | `store.ITEM_COLUMNS`，老库由 `_ensure_item_columns()` 自动补列 |
| 要发一个新请求 / 改连接与超时的行为 | `net.py`（不许在别处直接 `requests.get`，否则又变回一次一张新连接）；拦网络的测试在 `tests/conftest.py` 的 `_offline` |
| 缩略图的尺寸、并发数、磁盘缓存上限 | `app.IMAGE_SIZE` / `IMAGE_SUFFIX` / `IMAGE_WORKERS`（改 `IMAGE_SIZE` 会让盘上旧缓存自动作废）、`thumbs.MAX_FILES` |
| 成交判「新增」的算法 | `app.new_deals()` / `_aged_from()` |
| 「查看上次总结」按钮、快照留多久 | `app.ShowSummary()` / `app.OnShowLastSummary()` / `app._OpenSummaryDialog()`；顶部按钮在 `InitUI()` 的 `info_bar` 里 |
| 总结里的商品名能不能点、点了跳哪儿 | 链接在 `app._name_html()`（scheme 是 `SUMMARY_ITEM_SCHEME`），信号在 `SummaryDialog.name_clicked`，跳转在 `MainWindow.OnSummaryItemClicked()` |
| 拖拽排序的落点、自动滚动速度与触发边距 | `app.ReorderableTable`（`dragMoveEvent` / `_update_drag_scroll` / `_on_drag_scroll`）；速度与边距是顶部 `DRAG_SCROLL_*` 常量 |
| 史低价的只降不升规则 | `app.updated_lowest()` / `at_lowest()`；存的那一列在 `store.ITEM_COLUMNS` |
| 累积确认（攒什么、什么时候清零、顶部那句话） | 判据 `app.ack_change()` / `ack_new_deals()` / `ack_reached()` / `BuildAckSummary()`；写入 `store.acknowledge()` / `init_ack_if_empty()`（列在 `ITEM_COLUMNS`）；按钮与那句提示在 `SummaryDialog.btn_ack` / `MainWindow.ack_label` |
| 清单接受的写法 | `links.parse_line()` / `_split_fields()` |
| 收藏的写回、回滚与星标 | 交互在 `app.ToggleFavorite()`；清单那段在 `links.parse_line()` / `_strip_favorite()`，缓存那列由 `store.sync()` / `set_favorite()` 写，星标颜色在 `theme.favorite_color()` |
| 自动抓取的间隔、范围与总结开关 | `data/config.toml` 的 `auto_poll_*` 四项，校验在 `config.py`；调度在 `app.ScheduleAutoPoll()` / `OnAutoPoll()` / `AutoPollTasks()` |
| 企业微信推送的开关、地址、限流 | `data/config.toml` 的 `notify_*` 八项，校验在 `config.py`；正文在 `app.BuildNotifyMarkdown()`，发送在 `notifier.py` |
| 推送消息长什么样 | `app.BuildNotifyMarkdown()`（分块与超长截断）+ `notifier._post_wecom()`（报文格式）；改完看 `status-notify*` 场景出图 |
| 哪类变化才推（触发条件、边沿判据、合并口径） | `app.NotifyTags()`；开关是 `config.py` 里的 `notify_on_*` 五项；测试在 `tests/test_app.py` 的「触发判定」一节 |
| 相对时间的单位表 | `parser._RELATIVE_UNITS` |

## 相关文档

- [README.md](../README.md) —— 面向使用者的说明（安装、用法、配置项、界面操作）
- [tools/README.md](../tools/README.md) —— 开发工具登记表；写新工具前先看这里有没有现成的
