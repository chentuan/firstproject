# firstproject

三个互不相干的 Python 小工具。都只用标准库，都自带离线自检。

| 目录 | 是什么 |
| --- | --- |
| `calculator.py` | 桌面计算器，tkinter 图形界面，单文件 |
| `websnap/` | 按菜单抓取 Web 后台数据的通用工具（命令行 + tkinter 图形界面） |
| `qdama_daily.py` | 钱大妈智慧中台「日常订购」数据导出，账号密码换 token（见文末） |
| `qdama_gui.py` | 上面那个工具的图形界面，照着网页版复刻（见文末） |
| `fetch_orders.py` | 早期写的一次性脚本，抓订单中心的。功能已被 `websnap` 覆盖，留着当参照 |
| `profiles/` | `websnap` 的站点配置示例 |
| `tests/` | `websnap` 的单元测试 |

需要 **Python 3.11 或更高版本**（`websnap` 用标准库 `tomllib` 读配置）。计算器本身 3.9 就够。

---

# calculator.py — 桌面计算器

单文件、零第三方依赖，跑起来就一个窗口。

## 运行

```bash
python3 calculator.py
```

需要 tkinter 可用（macOS 官方的 python.org 安装包默认带）。

## 自检

```bash
python3 calculator.py --selftest   # 求值器测试，28 项
python3 calculator.py --smoke      # 构建窗口 + 按键状态机测试，10 项，跑完即退
```

`--selftest` 覆盖正常计算和 10 种错误输入；`--smoke` 会在真实窗口里模拟一串按键序列，验证输入状态机（连按运算符、除零、结果后续算等）。两个都返回退出码 0/1，可以挂到 CI 上。

## 键盘快捷键

| 按键 | 作用 |
| --- | --- |
| `0`–`9` `.` | 输入数字 |
| `+` `-` `*` `/` | 运算符 |
| `(` `)` | 括号 |
| `%` | 百分号 |
| `Enter` | 计算 |
| `Backspace` | 退格 |
| `Esc` / `Delete` | 清空 |

## 设计上的两个取舍

**求值器是手写的，没有用 `eval()`。** `eval()` 版计算器写起来只要三行，代价是给机器开了个任意代码执行的口子。这里换成四层递归下降解析器：

```
expr    := term (('+' | '-') term)*
term    := unary (('*' | '/') unary)*
unary   := ('+' | '-') unary | postfix
postfix := primary '%'*
primary := NUMBER | '(' expr ')'
```

**按钮是自绘的，没用 `tk.Button`。** macOS 的 aqua 主题会直接忽略 `tk.Button` 的 `bg` 参数——设深色它照样画浅灰。所以按钮用 `tk.Frame` + `tk.Label` 加鼠标事件自己拼。

## 已知限制

- 不支持幂运算、三角函数、对数。
- `±` 作用在 `5-3` 上会得到 `5--3`，数学上是 5−(−3)=8 没错，但显示出来绕。
- 结果统一保留 12 位有效数字，拿来算钱别指望它替你做财务级舍入。
- 输入长度上限 30 个字符。

---

# websnap — 按菜单抓后台数据

输入「系统地址 + 账号密码 + 指定菜单」，把那个菜单页面的数据抓下来。

```bash
python3 -m websnap
```

零依赖、零配置起步：登录表单、菜单列表、页面结构全是自动认出来的。配置只在自动探测不够用时才需要写。

## 快速开始

```bash
# 图形界面：填地址、点连接、选菜单、看表格、导出
python3 -m websnap.gui

# 交互问答：地址 → 账号 → 密码（不回显）→ 菜单列表里选
python3 -m websnap

# 全参数模式，适合脚本和定时任务
WEBSNAP_PASSWORD=xxx python3 -m websnap \
    --url http://localhost:8080 --username admin --menu 订单中心

# 只看菜单，不抓数据
python3 -m websnap --url http://localhost:8080 --username admin --list-menus

# 抓多个菜单，一个菜单一个 CSV 文件（-o 给目录）
python3 -m websnap --profile profiles/qiandama.toml --menu 订单中心,门店管理 -o out/

# 多菜单合成一个 JSON
python3 -m websnap --profile qiandama --menu 订单中心,门店管理 --format json -o all.json
```

密码推荐走环境变量 `WEBSNAP_PASSWORD` 或交互输入。写在 `--password` 上会留在 shell 历史和 `ps` 输出里，程序会就此提醒你。

## 它是怎么做到"通用"的

关键在**先识别结构类型，再取数据**，而不是给每个菜单写一份解析规则。

- **登录**：读登录页，找到带 `type="password"` 的那个表单；然后把表单里**所有 hidden 字段连同原值一起提交**。CSRF token、nonce、隐藏的 tenant id 全都自动带上——所以不需要为 Spring Security / Django / Laravel 各写一套策略。
- **菜单**：先按语义选择器找导航区（`nav`、`aside`、`[role=navigation]`、`.sidebar`…），找不到就按"链接文本占容器总文本的比例"兜底。导航区密度接近 1，正文区低得多。
- **数据结构**：`thead` 就是列名，一行一条记录；没有表头的两列表判为键值对；`ul`/`ol` 里有 3 个以上同构 `li` 判为列表；同构子元素容器判为卡片；都不成立才退化成纯文本。**列名从页面读，所以九个列数各不相同的菜单能共用一套代码。**
- **单元格内的链接**收进该行的 `_links` 字段，不污染列名。

自动探测的结果可以固化下来：

```bash
python3 -m websnap --url http://localhost:8080 --username admin \
    --save-profile profiles/qiandama.toml
```

生成的配置里**不含密码**——配置文件会进版本库，凭据不该进版本库。

## 命令行参考

```
--url URL              系统地址
--username U           登录账号
--password P           登录密码（不推荐，见上文）
--password-env VAR     从哪个环境变量读密码，默认 WEBSNAP_PASSWORD
--profile PATH         站点配置，路径或 profiles/ 下的名字
--menu M               要抓的菜单：序号、名字或路径，可重复、可逗号分隔
--list-menus           只列出发现的菜单
--format {table,csv,json}
-o, --out PATH         输出文件；结尾带 / 或以已存在目录给出时，视为输出目录
--timeout S            单次请求超时
--insecure             跳过 TLS 证书校验（内网自签证书）
--mode MODE            提取模式覆盖：auto|table|list|cards|kv|text
--selector CSS         限定提取范围，例如 main 或 #content
--save-profile PATH    把本次生效的设置写成 TOML
--gui                  打开图形界面
--selftest             跑单元测试
```

给 `-o` 一个带 `.json` / `.csv` 后缀的路径就能自动推断格式。

### HTTPS 与证书

不用特意管证书。工具在建立 TLS 连接时会检查本机根证书库，**发现是空的就自动改用系统证书库**（macOS 的 `/etc/ssl/cert.pem`）。

为什么要有这层：macOS 上用官方安装包装的 Python，ssl 默认一张根证书都没有，`create_default_context()` 拿到的是空集合——此时访问**任何**合法 HTTPS 站点都会报 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`。报错看着像地址写错了，其实是本机环境缺东西。同一台机器上系统自带 Python 和 Homebrew Python 常常一个有证书一个没有，这种"换个解释器就翻脸"的问题最容易耗时间。

要指到自己的证书库，设 `WEBSNAP_CA_BUNDLE=/path/to/ca.pem`。

只有当对方用**自签证书**时才需要 `--insecure` 跳过校验——那是对方的证书确实不被信任，跟本机证书库无关。

## 图形界面

```bash
python3 -m websnap.gui        # 或者 python3 -m websnap --gui
```

上方填地址、账号、密码，点「连接并探测菜单」；左侧列出识别到的菜单（默认全选）；右侧每个菜单占一个页签，表格可滚可看；底部导出 CSV（按菜单拆文件）或 JSON（合并成一个）。

几个行为值得说明：

- **网络请求全跑在后台线程**，界面不会假死。事件经队列回主线程处理——tkinter 不是线程安全的，从别的线程直接碰控件不会立刻报错，而是在某个随机时刻崩掉。
- **密码框不回显，也不落盘。**「载入配置…」只带出地址、账号和抓取规则，密码每次手输。
- **单个菜单抓失败不会中断整批。** 记一条进度就跳过，剩下的照抓——一个坏页面不该毁掉整轮。
- 表格列名和列宽**按页面实际表头生成**，所以列数 6~8 各不相同的菜单可以并排看。

自检入口（不需要人盯着看）：

```bash
# 只构建窗口，验证界面能起来
python3 -m websnap.gui --smoke

# 连真站点抓一遍：抓完从控件里把行数读回来核对
WEBSNAP_PASSWORD=xxx python3 -m websnap.gui \
    --url http://localhost:8080 --username admin --auto --dump out/
```

`--auto` 会真的连站点、真抓数据、真填进控件，然后**从 Treeview 里读回行数和列名**打印出来。这个区别很重要：断言"结果存进了变量"和断言"结果真的显示在界面上了"是两回事，前者骗得过自己。

## 站点配置

```toml
[site]
name = "钱大妈后台"
base_url = "http://localhost:8080"
timeout = 15.0
verify = true

[auth]
mode = "auto"              # auto = 自动探测表单；none = 不需要登录
login_path = "/login"
username = "admin"
username_field = "username"
password_field = "password"

[menus]
nav_selectors = ["aside"]  # 留空 = 用内置语义选择器自动找
exclude = []               # 留空 = 用内置排除词（logout/退出/注销…）
min_links = 3

[extract]
mode = "auto"
selector = ""
```

写错字段名会**告警而不是静默失效**——配置里拼错一个字母然后对着空结果猜半天，是最难查的一类问题。

## 设计取舍

**为什么配置用 TOML 而不是 YAML。** PyYAML 不是标准库，为了保住"零依赖"去手写 YAML 解析器不划算；JSON 是标准库但不能写注释——配置文件不能写注释，等于逼着下一个接手的人去读代码猜字段含义。TOML 两头都占，`tomllib` 从 Python 3.11 起就在标准库里。

**为什么要自己写 DOM 和选择器。** 标准库只有流式的 `HTMLParser`，没有树、没有选择器。而通用抓取恰恰依赖这两样：有了树才能谈"这个容器里有几个同构子元素"，有了选择器才能让配置表达"限定在 `main` 里"。所以 `websnap/dom.py` 实现了一个宽容的建树器（模拟浏览器的隐式闭合规则，`<li>`/`<td>` 不写 `</td>` 也能正确嵌套）和一个选择器子集（`tag` `.class` `#id` `[attr=value]` + 后代/子元素组合）。不支持 `:nth-child` 和兄弟选择器，抓后台数据用不上。

**为什么空表头会变成"操作"列。** 后台表格最后一列常常是"编辑/删除"按钮，`<th>` 里什么都不写。如果这一列的数据里全是链接，就说明它是操作列——给它个像样的名字，比让它变成 `列7` 有用。

## 已知边界

这套东西覆盖的是**服务端渲染的传统 Web 后台**。以下是明确做不到的，说清楚比含糊过去好：

- **纯前端渲染的系统（Vue/React SPA）**：HTML 里只有一个空壳，数据要等 JS 跑完才出现，抓不到——得换 headless 浏览器去点，或者逆向它的 XHR 接口。**但工具认得出这种情况**：body 几乎没内容、又引用了 webpack/Vite 打包产物时，它会直接说明"数据不在 HTML 里"，而不是甩个 0 条让你去反复调 selector。
  - 这类系统的**登录页往往同样是 JS 渲染的**，于是卡在更前面一步：连登录表单都找不到。特征是"试过的每个路径都返回同一份 HTML，且这份 HTML 是空壳"——路由在前端，服务端对任何路径都吐同一个入口文件。工具会点明原因并给出可行路线，**不会再建议你去改 `login_path`**，因为那不是问题所在。
- **密码在浏览器里被 JS 加密过的系统**（RSA、MD5 加盐）：服务端收到的是密文，配置救不了，得把那段前端逻辑逆出来写成适配器。
- **图形验证码、短信 OTP、扫码 SSO**：需要人工介入。
- **分页**：目前是"一页拿全"的实现。后台开始分页后只会抓到第一页，需要扩展翻页逻辑。
- **CSS 选择器只支持子集**：`tag`、`.class`、`#id`、`[attr]`、`[attr=value]`，以及空格和 `>` 组合。
- **图形界面不适合挂定时任务**：要定时、要批量、要接进流水线，用命令行模式。界面的价值在于把数据当场摊在你面前，不在于自动化。

## 测试

84 项单元测试，全部离线，不依赖任何服务：

```bash
python3 -m websnap --selftest
# 或者
python3 -m unittest discover -s tests -t .
```

覆盖 HTML 隐式闭合、选择器引擎、各类表格变体（合并单元格、空表头、列数不符、重复列名）、键值对/列表/卡片/文本识别、菜单发现与过滤、登录表单解析、CJK 对齐、CSV BOM、配置读写、**多菜单输出的完整性**（终端必须打印每一个菜单，多菜单禁止写进同一个 CSV 文件），以及**网络故障的分类准确性**（证书不信任、端口拒绝、域名解析失败、超时必须各说各话）、**前端渲染页面的识别**，和**登录失败的诊断信息**（必须列出试过哪些地址；整站返回同一个前端空壳时要指出这是 SPA，而不是建议用户去改配置）。

`tests/test_gui.py` 里的 9 项测图形界面背后那层逻辑（登录探测、菜单发现、抓取、错误翻译）。它**自己起一个真的 HTTP 服务当靶子**，而不是把 `WebSession` mock 掉——cookie、302 跟随、CSRF 隐藏字段、登出链接过滤这些真实行为会被一起验到，同时依然完全离线。

**断言的是一行行具体的期望值，不是"能跑通"。**

---

# qdama_daily.py — 钱大妈智慧中台数据导出

把「日常订购」页面的数据拉成 CSV。账号密码登一次，之后一条命令出数。

```bash
# ① 登一次。密码交互输入，不回显、不落盘、不进 shell 历史
python3 qdama_daily.py login --user 13800138000

# ② 以后每次就这一条
python3 qdama_daily.py fetch --days 7 --shop A25V -o ~/Downloads/daily.csv
```

零第三方依赖，纯标准库。目标系统（`olpadmin.qdama.cn`）是 Vue SPA，HTML 里是个空壳，抓不到——所以接口是从前端打包产物里逆出来的：接口路径、签名算法、RSA 密码加密，全在脚本里。这也让它顺带成了一套完整的「绕开前端直接调接口」的样例，方法论记在逆向笔记里。

## 六个子命令

| 子命令 | 干什么 |
| --- | --- |
| `login` | 账号密码换 token，写盘并顺手验一次可用性 |
| `fetch` | 拉数据出 CSV（单天 / 最近 N 天 / 区间） |
| `probe` | 只打一次接口，打印完整 URL、请求头、原始响应（排查用） |
| `checkperm` | 问服务端某个权限点开没开 |
| `calibrate` | 用一条真实 URL 离线反推签名拼法 |
| `selftest` | 离线自检，27 项，不发网络请求 |

`python3 qdama_daily.py <子命令> --help` 看任意一条的完整参数。

## login — 账号密码换 token

```bash
# 交互输密码（推荐）
python3 qdama_daily.py login --user 13800138000

# 定时任务：密码走环境变量
QDAMA_PASSWORD='xxx' python3 qdama_daily.py login --user 13800138000

# 密码从文件读首行
python3 qdama_daily.py login --user 13800138000 --password-file ~/.qdama_pw

# 只想验证账号密码对不对，不写任何文件
python3 qdama_daily.py login --user 13800138000 --no-save

# 看服务端返回了什么（token 自动脱敏）
python3 qdama_daily.py login --user 13800138000 --show-json

# 平台端账号（默认是门店端 Shop）
python3 qdama_daily.py login --user xxx --role-type Tenant
```

| 参数 | 说明 |
| --- | --- |
| `--user` | **必给**。登录账号（手机号 / 工号） |
| `--password-file` | 密码文件，读首行；不给就交互输入 |
| `--role-type` | `Shop`（默认，门店端）/ `Tenant`（平台端） |
| `--save-token` | token 写到哪，默认 `~/Downloads/qdama-token.txt` |
| `--no-save` | 不写任何文件，只验证 |
| `--show-json` | 打印服务端返回的完整 data |
| `--tenant` | 租户 ID，默认 `0210000001` |
| `--domain` | 目标环境，默认生产 `olpadmin.qdama.cn` |

**没有 `--password` 参数，这是故意的。** 命令行参数会进 shell 历史，同机 `ps` 也能看到。三条路按推荐序：交互输入 → `--password-file` → `QDAMA_PASSWORD`。

登录成功后落两个文件（权限都 600）：

- **`~/Downloads/qdama-token.txt`** —— 正是 `fetch` 自动探测的路径，所以下一步不用传 token
- **`~/.qdama_session.json`** —— 存 `shop_id` / `sys_user_id` / `tenant` / 门店列表，所以 `fetch` 不用再手输 `--shop-id 205082 --sys-user-id 136387`

写完还会拿新 token 打一次需要登录态的接口，当场说「能」还是「不能」——免得废 token 存了盘，你下次跑 fetch 才发现。

⚠️ **连错 5 次密码会锁号**（服务端提示原文如此）。脚本里没有任何自动重试，撞上 `100002` 就停手核对。

## fetch — 拉数据

日期三种给法，**互斥，只能用一种**：

```bash
# 单天
python3 qdama_daily.py fetch --date 20260923 --shop A25V -o daily.csv

# 最近 7 天（含今天）
python3 qdama_daily.py fetch --days 7 --shop A25V -o last7.csv

# 区间
python3 qdama_daily.py fetch --from 20260901 --to 20260923 --shop A25V -o sept.csv
```

多天会逐天打接口、合成一个 CSV，**首列 `__date` 区分日期**；不同天字段不一致时按并集展开列（并集只在多天模式加 `__date`）。某天失败不中断整轮，最后汇总几成几败。

| 参数 | 说明 |
| --- | --- |
| `--date` | 单天，`yyyyMMdd` |
| `--days` | 最近 N 天（含今天） |
| `--from` / `--to` | 区间，`--to` 默认今天 |
| `--shop` | **必给**。门店 SAP 编码（`sapshopid`，如 `A25V`） |
| `-o, --out` | CSV 输出路径；不给就打到终端 |
| `--sleep` | 多天之间的间隔秒数，默认 0.3 |
| `--token` / `--token-file` | 手动指定 token，默认自动探测 |
| `--shop-id` | `X-QDM-Shop-Id` 头，**login 之后自动带上** |
| `--sys-user-id` | 同上，login 之后自动带上 |
| `--tenant` / `--domain` / `--api-env` | 环境相关，一般不用碰 |

**`--shop` 是唯一每次都要写的业务参数。** 它是 SAP 门店码，接口按它过滤；而 session 里存的是另一个编码 `shopid`（`205082` 这种），两者不通用，所以没法自动填。

**不给 `--shop-id` 会怎样**：脚本拿 `--shop` 顶替并在 stderr 提示。但前端这里填的是 `shopObj.shopid`，填成 `A25V` 有被判越权的风险——所以先 `login` 一次把 session 建起来是最省心的。

## probe / checkperm — 出问题时

```bash
# 打一枪看原始响应：URL、签名、请求头、body 全打印
python3 qdama_daily.py probe --date 20260923 --shop A25V

# 问服务端权限（这条能一句话分清「真没权限」和「参数不对」）
python3 qdama_daily.py checkperm
python3 qdama_daily.py checkperm --perm scn:purchase:everydayexport --perm 想查的权限点
```

`probe` 允许不带 token 跑——就是要让你看到 `100043 token为空` 这类中间态。输出里 `B2B-Authorization` 只打长度，不打印内容。

## 凭证与环境变量

| 环境变量 | 作用 |
| --- | --- |
| `QDAMA_TOKEN` | 直接给 token |
| `QDAMA_TOKEN_FILE` | 给 token 文件路径 |
| `QDAMA_PASSWORD` | login 的密码（定时任务用） |
| `QDAMA_CA_BUNDLE` | 指定 CA 证书文件，默认自动借系统证书库 |

token 查找优先级：`--token` → `--token-file` / `$QDAMA_TOKEN_FILE` → `$QDAMA_TOKEN` → 约定路径自动探测。

自动探测三个路径，**按顺序命中即止**：

1. `~/.qdama_token`
2. `~/Downloads/.qdama_token`
3. `~/Downloads/qdama-token.txt` ← `login` 写的是这个

⚠️ 前两个排在前面。要是你曾手动往 `~/.qdama_token` 放过旧 token，`login` 写的新 token 会被它盖住，表现是「刚登录完就报 100031」。**统一只用第 3 个**，或者换 token 后把旧文件删掉。脚本会把实际用的来源打出来，报 `100031` 时先看这一行。

## 常见错误码

| 码 | 含义 | 怎么办 |
| --- | --- | --- |
| `100002` | 用户名或密码错误 | 核对。**别连试 5 次，会锁号** |
| `100029` | 系统签名错误 | 请求头 `v` 没带或参数拼错，属脚本 bug，报我 |
| `100031` | 登录失效 | token 过期，重跑 `login` |
| `100043` | token 为空 | 没找到 token，查上面的探测路径 |
| `100006` | 没有此功能权限 | 先跑 `checkperm` 确认；也常是 `X-QDM-Shop-Id` 填错 |
| `300030` / `300031` | 首次登录，要求先改密码 | 去网页端登一次改掉 |
| `54003` | 登录设备变更，要求确认绑定 | 去网页端登一次完成确认 |

## 自检

```bash
python3 qdama_daily.py selftest      # 27 项，全离线
```

覆盖签名算法的**三条真实样本**（query 分支 ×2 + POST body 分支 ×1，逐字节比对）、JSON 序列化、请求形状、请求头与 cookie、DER 公钥解析、RSA PKCS#1 v1.5 往返加密、超长密码报错边界。签名这类东西改坏了不会立刻报错，只会静默返回空数据——所以改完顺手跑一下。

## 已知边界

- **登录接口目前没有图形验证码和短信验证**，账号密码自动登录这条路才走得通。服务端哪天上了风控（回 `300030` / `54003` 这类码），这条路就断，只能退回浏览器导 token。
- 只覆盖「日常订购」一个页面。同系统其他页面要另接（签名方式和请求形状完全一样，换个 `path` 和业务参数即可）。
- 明文密码只存在于 `login` 进程的内存里，加密后立即发走，脚本不缓存密码。

---

# qdama_gui.py — 图形界面

把上面那套接口调用搬进窗口：输账号密码登录，选门店和到店日期，按条件筛，结果分页看。

```bash
python3 qdama_gui.py            # 开窗体
python3 qdama_gui.py --smoke    # 只构建窗口然后退出（自检）
```

零依赖，界面只用标准库 tkinter。

## 先登录，才看得到列表

启动**永远停在登录页**，不会偷偷拿本机 token 直接进主界面。顺序是硬性的：

```
填账号密码 → POST 换 token → token 落盘成功 → 才切到列表页（并自动拉一次数据）
```

token 写盘失败就停在登录页并说明原因 —— 也就是说，**"看到列表页"这件事本身就等于
"凭证已经安全落地了"**。

登录页照网页版的样子做：白卡片 + 品牌红头 + 账号/密码 + 「记住账号」（只存账号，
密码任何时候都不落盘）。底下还有个小的逃生口「用本地已保存的 token 进入」——
它**不是无脑放行**，会先拿 token 打一次需要登录态的接口验证有效才进。

> ⚠️ **token 有效期很短，实测约 45 分钟**（22:04 登录拿到的，22:50 就回 `100031`）。
> 所以每次用之前登录一下是常态，不是哪里配错了。那个逃生口只在 token 还新鲜时有用。

## 配色是钉死的浅色，不跟随系统深色模式

**这条是踩出来的。** 上一版表格在某些行上完全看不清：

- tkinter 的 `aqua` 主题把表格配色指向系统色（`systemTextBackgroundColor` /
  `systemTextColor`）。**系统处于深色模式时，文字色解析成白色。**
- 而"隔行浅色"当时只设了背景 `#f7f7f7`、没设前景 → 白字压浅灰底，整行糊掉。

现在：主题切到 `clam`（aqua 在 macOS 上会忽略部分属性），并给每个控件显式指定十六进制
颜色；表格的**每一行都打标签**，标签里**背景和前景成对给**。不打标签的行会回落到主题
默认色，那正是变白字的原因。`--auto` 里有 4 项专门守这条，连 `Style` 解析出来的**实际
生效值**一起断言（只断言"我给了什么值"不够，主题可能在渲染时把它换掉）。

## 界面是照着网页版复刻的

**复刻的是结构和文案，不是像素** —— Element UI 的圆角阴影跟着来没有意义。但下面这些一字不差：

- **13 列表格**：列名、顺序、宽度全抄自页面打包产物里的 `el-table-column` 定义
- **6 个查询条件 + 三级分类**：中文标签、下拉选项（含「全部」）与页面一致
- **订单状态三档**：全部 / 已提交 / 已删除
- **底部的「合计品项 / 订购数量」**，以及每页 10 条

```
查询门店[A3VP▾] 权限门店[A3VP 上海申江豪城▾] 到店日期[2026-09-23] [今天][昨天] [刷新数据]
────────────────────────────────────────────────────────────────────────────────
查询条件
  订单类型[全部▾]  订购来源[全部▾]  处理类型[全部▾]
  商品编码[______] 商品名称[______] 销售方式[全部▾]
  大分类[全部▾]    中分类[全部▾]    小分类[全部▾]        [查询] [重置] [导出 CSV…]
────────────────────────────────────────────────────────────────────────────────
到店日期│商品编码│商品名称│销售方式│小分类│订购数量│…│组合套餐│备注
────────────────────────────────────────────────────────────────────────────────
合计品项：219   订购数量：626.55   每页[10▾] ‹ › 第1/23页   订单状态 (全部)(已提交)(已删除)
```

## 查询是本地做的，所以点「查询」是瞬间响应

页面上的查询条件**不发给服务端**：接口只收「日期 + 门店」，一次返回当天全量，那些下拉框
是在浏览器里对这批数据做本地过滤。这里照做——点「查询」就是对一个列表做 filter，不打网络。

换来的是瞬时响应，代价是：**看到的是"点刷新那一刻"的快照**。要最新的就再点一次「刷新数据」。

## 门店有两个，别搞混

顶部有「查询门店」和「权限门店」，因为它们真的不是一回事：

| | 是什么 | 能否随便填 |
| --- | --- | --- |
| **查询门店** | 接口参数 `sapshopid`，决定**查哪个门店的数据** | 可以，任意门店 |
| **权限门店** | 请求头 `X-QDM-Shop-Id`，决定**以哪个门店的身份查** | **不行**，必须是账号有权访问的门店 |

权限门店一旦填了没权限的门店，服务端回：

```
code=100006 您好，您暂时没有此功能使用权限，请联系管理员开通，谢谢-[scn:purchase:view]
```

**这句话有误导性** —— 它说"功能没开"，实际原因是"门店不对"。所以权限门店做成下拉，
只列登录返回的门店（`shopselects`），不给自由填的机会。

实测（`sapshopid` 固定为 `A25V`）：

| 权限门店 | 结果 |
| --- | --- |
| `A3VP` / `205082`（账号有权限） | ✅ 正常返回 334 行 |
| `A25V`（就是查询门店自己） | ❌ `100006 ...-[scn:purchase:view]` |
| 随便编一个 | ❌ 同上 |

**换句话说：查 A25V 的数据，要用 A3VP 的身份去查。** 这也是脚本不能把 `X-QDM-Shop-Id`
默认成 `--shop` 的原因 —— 那个"贴心"的回退正好踩这个坑。

## 参数

```
--shop SAP码       预填查询门店（如 A3VP / A25V）
--shop-id ID       预填权限门店（X-QDM-Shop-Id）
--date 日期        预填到店日期，yyyy-MM-dd
--user 账号        预填登录账号
--config 路径      界面偏好文件，默认 ~/.qdama_gui.json
--smoke            只构建窗口然后退出
--auto             用假数据跑完整条界面链路，从表格里读回来核对（离线）
--live             用真实 token 拉真数据，核对界面渲染与接口返回是否一致
```

界面偏好（门店码、每页条数、上次的账号）写在 `~/.qdama_gui.json`，**不含密码**。
不勾「记住账号」时连账号也不写（`remember: false`）。

## 自检

```bash
python3 qdama_gui.py --smoke                                 # 窗口能不能建起来
python3 qdama_gui.py --auto                                  # 22 项，全离线
python3 qdama_gui.py --live --shop A3VP --date 2026-09-23    # 真实数据核对
```

后两个都**从真实的 Treeview 里把行读回来**再断言，而不是检查某个中间变量 ——
"结果塞进了变量"和"结果真显示出来了"是两件事，前者骗得过自己。

`--live` 最有说服力：它验证本地按页面口径重算的合计，与服务端返回的 `totalskuqty` /
`totalorderqty` 是否**逐位一致**。对不上就说明复刻的口径抄错了。

## 已知边界

- **复刻的是结构与文案，不是像素。** tkinter 没有圆角、阴影、动画，也不打算有。
- **列头排序没做。** 页面「订购数量」列可以点表头排序，这里没有。
- **分类联动与页面略有差异**：页面是 `el-cascader` 级联、选项来自分类树接口；这里是三个
  联动下拉、选项从**已加载的数据**里现取。好处是少打一个接口、也不会选出一堆当天没货的分类；
  代价是和页面的分类树不完全等价。
- **token 存活时间很短**（实测约 45 分钟），过期后所有取数都回 `100031`。
  界面遇到它会自动切回登录页，重新登一次即可。
- **界面配色写死为浅色**，不跟随 macOS 深色模式（原因见上文）。想要深色版得另外做一套。
- **需要 tkinter**：macOS 官方安装包自带的 Python 都有，精简发行版可能没有。
- **别用 `~/.workbuddy/binaries/` 下那个解释器跑界面** —— 它的 Tk 9.0 在本机初始化窗口时
  会被 SIGTERM 干掉（`--auto` 之类的无窗口自检不受影响）。你终端里的 `python3` 是
  `/Library/Frameworks/...`（Tk 8.6），**直接敲命令就行**。
