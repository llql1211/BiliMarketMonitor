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
│   ├── watchlist.example.txt   # 监视清单示例（模板）
│   ├── watchlist.txt           # 监视清单（用户维护，程序会规范化写回，不入库）
│   ├── watchlist.txt.bak       # 写回前留的备份，只留第一次那份（不入库）
│   ├── cache.db                # 本地缓存（首次运行自动生成，不入库）
│   └── cache.db.bad-<时间戳>    # 缓存损坏时的备份（自动改名留档，不入库）
├── src/                # 应用代码，7 个模块（见下）
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

| 模块 | 职责 | 主要出口 |
| --- | --- | --- |
| [src/app.py](../src/app.py) | PyQt5 主窗口、表格与各对话框、轮询调度、抓取总结 | `MainWindow`、`PollerThread`、`ImageFetcher`、`main()` |
| [src/client.py](../src/client.py) | 调用接口，失败统一成一个出口 | `fetch_cluster(cluster_id, timeout)`、`ApiError` |
| [src/parser.py](../src/parser.py) | 解析响应：名称/价格/均价/成交/图片，相对时间换算 | `parse_cluster(resp)`、`parse_error(err)`、`age_bounds(text)` |
| [src/links.py](../src/links.py) | 监视清单读写：解析各种写法、清洗、规范化回写 | `load_links(path)`、`save_watchlist(path, items)`、`build_detail_url(...)` |
| [src/config.py](../src/config.py) | 配置读取与逐项校验 | `load_config()`、`data_dir()`、`DEFAULTS` |
| [src/store.py](../src/store.py) | SQLite 缓存：随清单增删、损坏自愈 | `Store`（`sync` / `upsert_item` / `get_all` / `cached_deals`）、`default_db_path()` |
| [src/theme.py](../src/theme.py) | 深浅两套 QSS、系统深浅色判断、各处配色 | `apply_theme(app, dark)`、`qss_for(dark)`、`system_uses_dark()` |

几点值得单独记住：

- **每个模块都自己检查输入**，不依赖调用方「保证」传进来的是合法数据。边界上（用户手写的文件、
  无文档的响应、拼出来的 URL）尤其如此。
- **异常一律降级 + 留痕**，不往上抛、不弹窗打断。`store` 和 `links` 把这次操作的异常情况攒在
  各自的 `notes` 里，`app` 收上去统一在状态栏说一句。详见「降级与留痕」。
- **测试面**：`parser` 和 `links` 是纯函数，最好测；`app` 有 2370 行、逻辑最重，测试也最厚
  （`tests/test_app.py` 3600 行）；改这两处时对应测试一起改。

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
      │  点「开始抓取」（或 F5）
      ▼
  PollerThread（子线程，依次、非并发）
      │  client.fetch_cluster()      发请求，失败重试 1 次
      │  parser.parse_cluster()      判空提取字段
      ▼
   信号回主线程 ──► 刷新表格 + store.upsert_item() 写缓存
                    └► links.save_watchlist()  学到了新名字才回写清单
```

- **配置只在启动时读一次**（`MainWindow.__init__` 里的 `config.load_config()`），
  之后拼详情页链接、轮询间隔、重试间隔、超时、高亮阈值与颜色都以它为准。
  改配置要重启程序。
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

## 线程模型

界面永远不假死，靠的是「网络活儿全在子线程，结果只经信号回主线程」这一条。

- **`PollerThread(QThread)`**：拿到一份任务清单后**依次**请求，每条结果 emit 一个信号回主线程。
  - **暂停**在两条商品之间生效，不打断正在进行的请求；**停止**是让当前这次抓取提前收尾，
    已经抓到的结果保留在表格和缓存里。
  - 相邻请求之间按 `poll_interval_seconds` 睡，单个商品失败后按 `retry_interval_seconds`
    等一会儿重试 1 次；重试仍失败只影响这一件，其余照抓。
  - 一轮正常抓完 emit 结束信号，主线程弹抓取总结：这一轮抓了多少件、花了多久，
    再加上涨跌 / 新成交 / 到价三块明细。**中途停止的那一轮不弹**
    （只抓了一半，拿半份数据当总结容易让人以为其余商品都没变）。
  - 总结里的「用时」是墙钟时间刨掉暂停的那段（`MainWindow.RunElapsed`）：暂停是用户
    自己按的，算进去会让人以为抓取本身就那么慢。暂停时长在 `OnTogglePause` 里攒，
    收尾时定格成 `run_elapsed`——总结窗口换主题要重渲染，那时不能再算一遍。
- **`ImageFetcher(QObject)`**：拉图不走轮询线程，每张图起一个 daemon 线程，下好一张 emit 一张。
  信号里带的是调用方自己认的 key（缩略图用行号，双击预览用请求号），对不上就丢掉。
  下载失败发 `failed` 而不是静默丢弃——大图下不下来得跟用户说一声，缩略图那边不接这个信号，
  等于照旧不吭声。
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
- `favorite`（收藏）是缓存里**唯一一项不来自抓取**的数据：价格、均价、成交、史低价都是抓来的，
  它只从清单来。所以能写它的口子只有两个——`sync()` 跟着清单整体覆盖、`set_favorite()` 改一条；
  `upsert_item()` 一个字都不碰，抓取结果回来不会把用户打的星冲掉。传进 `sync()` 的二元组
  （只给 ID 和名字的老写法）按「未收藏」算，也就是会把缓存里那颗星**取消**掉——清单是正本，
  这条没有例外，取消收藏因此不会被下一轮 sync 悄悄还原。
- `lowest_price`（史低价）是缓存里**唯一一条不可再生**的记录：价格、均价、成交都是「抓一次就
  回来」的快照，它却是历次抓取攒出来的。所以删库、重建、把商品移出清单再拖回来，都会把它
  丢掉且补不回来（只能从下一轮抓取重新开始攒）。这条在 README 的「说明」里对用户讲明了。
  落库用的是普通的 `COALESCE`——传 `None` 是「这轮没有新记录」，不是「清掉记录」，
  跟同表里价格那组「传了覆盖、传 `None` 保留」的快照语义正好对上，只是判据在调用方手里。
- 自愈三档：库文件损坏 → 改名成 `cache.db.bad-<时间戳>` 后重建空库；目录不存在 → 自己建；
  连库都开不了（权限之类）→ 退到内存库，本次运行照常可用。全都记进 `notes`。

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
2. **不联网**。`requests.get/post` 一律换成抛 `ConnectionError`；要造响应就用 `fake_client`。
3. **不弹窗**。`QMessageBox` 的三个静态方法与 `webbrowser.open` 一律被拦下并记录，
   界面测试才能无人值守地跑（`msgboxes` / `opened_urls` fixture）。

界面测试跑在 Qt 的 **offscreen** 平台上，不需要显示器。关卡在 `import PyQt5` 之前落下，
所以本模块必须先于任何 PyQt5 导入执行。跨线程信号得转事件循环才收得到，
用 `wait_until` / `join_thread` 两个 fixture，别用 `sleep`。

| 测试文件 | 覆盖 |
| --- | --- |
| [tests/test_app.py](../tests/test_app.py) | 主窗口渲染与按钮流程、`PollerThread`、缩略图线程、总结窗口 |
| [tests/test_client.py](../tests/test_client.py) | 请求形态与各类失败的统一出口 |
| [tests/test_config.py](../tests/test_config.py) | 配置优先级与逐项校验 |
| [tests/test_links.py](../tests/test_links.py) | 清单解析、去重、规范化回写 |
| [tests/test_parser.py](../tests/test_parser.py) | 响应解析与全路径判空 |
| [tests/test_store.py](../tests/test_store.py) | 缓存随清单增删、设置项、损坏自愈 |
| [tests/test_theme.py](../tests/test_theme.py) | 样式表、占位文字配色、系统深浅色判断 |

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
| 抓取节奏、超时、重试、高亮阈值与颜色 | `data/config.toml`（格式见 `data/config.example.toml`，代码兜底在 `config.DEFAULTS`） |
| 缓存要多存一个字段 | `store.ITEM_COLUMNS`，老库由 `_ensure_item_columns()` 自动补列 |
| 成交判「新增」的算法 | `app.new_deals()` / `_aged_from()` |
| 史低价的只降不升规则 | `app.updated_lowest()` / `at_lowest()`；存的那一列在 `store.ITEM_COLUMNS` |
| 清单接受的写法 | `links.parse_line()` / `_split_fields()` |
| 收藏的写回、回滚与星标 | 交互在 `app.ToggleFavorite()`；清单那段在 `links.parse_line()` / `_strip_favorite()`，缓存那列由 `store.sync()` / `set_favorite()` 写，星标颜色在 `theme.favorite_color()` |
| 相对时间的单位表 | `parser._RELATIVE_UNITS` |

## 相关文档

- [README.md](../README.md) —— 面向使用者的说明（安装、用法、配置项、界面操作）
- [tools/README.md](../tools/README.md) —— 开发工具登记表；写新工具前先看这里有没有现成的
