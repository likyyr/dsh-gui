# dsh-gui

给 DeepSeek Harness 用的 Windows 桌面 GUI 操作插件：**按控件定位、按物理坐标点击、写值回读、真键盘输入**。

## 定位：垫底手段，不是首选

模拟鼠标键盘**慢**（一次动作 0.5–2 秒，长流程里是几十秒），所以默认顺序是：

**接口 / CLI → CDP / DOM（网页用选择器）→ 键盘（快捷键、Tab 走位）→ 本插件（真鼠标 / 逐字输入）**

只有前面几条路全都走不通时才用它：自绘界面没有控件树、验证码、安装向导、别的程序没有 API，或者验收要求"人怎么点我就怎么点"。

## 为什么做它

现有桌面工具的通病是"点不准"：截图坐标换算易错、窗口一动坐标就过期、点击到别的窗口也不报错。
本插件把这件事反过来做——**不猜坐标，问控件**：

1. UIA 读控件树，每个控件自带矩形，**矩形就是屏幕物理像素**（Per-Monitor V2 感知，零 DPI 换算）
2. 每次动作手机 **重新定位**，绝不复用缓存矩形
3. 点击前把窗口真正顶到前台（AttachThreadInput 绕过前台锁）
4. 动作后 **回读验证**（值是否写入、前台是否还在目标窗口），失败如实返回

## 工具

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `screen_map` | 列窗口控件树：名字/类型/矩形/AutomationId/是否可用 | `window`（标题或 PID）、`depth`、`max_items`、`include_unnamed` |
| `screen_click` | 按控件点击（UIA Invoke 优先，退回鼠标点中心）；`button` 右键、`double` 双击、`verify` 点后自验 | `window`、`target`（`{name}`/`{aid}`/`{type}` 或 map 返回的 `index`）、`x`/`y`、`button`、`double`、`verify` |
| `screen_set` | 写值（ValuePattern 直写，失败退键盘） | `window`、`target`、`value` |
| `screen_get` | 读控件当前值 | `window`、`target` |
| `screen_wait` | 等窗口或控件出现（替代 sleep） | `window`、`name`、`timeout` |
| `screen_shot` | 1:1 物理像素截图，返回这张图覆盖的物理矩形；`grid` 叠坐标网格 | `window`、`region`、`annotate`、`grid` |
| `screen_type` | 逐字打字（unicode / vk 两条路，中文直接打） | `window`、`text`、`mode`、`delay_ms`、`enter` |
| `screen_key` | 单键/组合键（ctrl+a、enter、f5…） | `window`、`keys`、`repeat` |
| `screen_calibrate` | 枚举显示器：物理矩形、DPI、缩放系数 | 无参数 |
| `screen_hover` | 悬停到控件/坐标上等一会儿，**报告这期间新冒出来的子菜单/提示框**（三级菜单专用） | `window`、`target`、`wait`、`detect_popup` |
| `screen_scroll` | 指针放到控件/坐标上滚轮（up/down/left/right），能读位置就报百分比，读不到就比画面 | `window`、`target`、`direction`、`amount`、`verify` |
| `screen_drag` | 拖拽：起点→终点分步移动，返回 changed_px / window_moved | `window`、`from_name`/`from_x`/`from_y`、`to_name`/`to_x`/`to_y`、`duration_ms` |
| `screen_find` | 模板匹配找图：给张小图，返回屏幕物理坐标（治"读不到控件但长得固定"的元素） | `template`、`threshold`、`region` |
| `screen_windows` | 列出所有可见顶层窗口（标题/类名/PID/矩形），不确定窗口标题时先查 | 无参数 |

`target` 四种写法任选：

```json
{"name": "文件(F)"}          // 按名字
{"aid": "TextBox"}           // 按 AutomationId
{"type": "EditControl"}      // 按类型（取第一个）
5                            // map 结果里的 index，最省 token
```

## 依赖（自包含，不用手动装）

插件自带 `lib/gui_server/vendor/`，里面有 `uiautomation`、`comtypes`、`pillow`（约 19 MB，cp312-win_amd64），
**拷到别的机器上直接能用，不需要 pip 装任何东西**。服务启动时按这个顺序找依赖：

1. 先看 `vendor/`；有就加载（正常情况走这条，零网络、零等待）
2. `vendor/` 里没有、宿主 Python 也没装 → 自动 `pip install --target vendor`（清华源，首次约 1–3 分钟，慢网可能超时）
3. 装不上 → 服务日志打印失败原因和可手动执行的完整命令；插件工具会返回"Python 服务没起来"

> 换 Python 大版本（比如 3.13）时，vendor 里 pillow 的二进制不匹配，会自动走到第 2 步补装对版本的包。
> 想换镜像源：设环境变量 `DSH_GUI_PIP_INDEX`。

## 安装

```bash
# 方式一：从 GitHub 直接装（推荐，谁都能用）
dsh plugin --profile desktop add "https://github.com/likyyr/dsh-gui/releases/download/v0.2.6/dsh-gui-0.2.6.tgz"

# 方式二：源码目录（开发用，改完重启即生效）
dsh plugin --profile desktop add "file:<本地目录>/dsh-gui"

# 方式三：本地打好的包（离线分发用）
dsh plugin --profile desktop add "file:<本地目录>/dsh-gui-0.2.6.tgz"

# 三种方式都要重启 DSH 才生效
```

> 直接从 GitHub 下载在国内可能很慢（8.5 MB 实测 6 分钟），急用就走方式二或先把 tgz 传进来。

插件自带 HTTP 服务（默认端口 8791），`dsh` 进程启动时自动拉起 Python 侧服务；也可手动启动：

```bash
python lib/gui_server/gui_server.py 8791
```

## 架构

```
DSH (Node)
  └─ lib/index.js        注册 11 个工具，校验参数，转发 HTTP
       └─ 127.0.0.1:8791  Python 单线程 HTTPServer
            └─ gui_core.py  UIAutomation + Win32 SendInput
```

Python 侧用 `uiautomation`（Python 版 UIA 封装）。单线程服务是刻意的：
多线程会让每个请求线程都缺 `CoInitialize`，而主线程显式初始化又会让 UIA 卡死 40 秒以上。

## 实测（本机 1920×1080，DPI 100%）

| 场景 | 结果 |
|---|---|
| map 记事本 | 20 控件 / 0.02s |
| map 资源管理器 | 88 控件（55 可用）/ 0.23s |
| map 设置（UWP） | 66 控件 / 0.13s |
| map Edge 网页 | 28 控件 / 0.09s |
| 点击文件菜单（窗口移动 3 次，含半出屏负坐标） | 3/3 命中，菜单内容正确 |
| 写值：设置窗口搜索框 | ValuePattern 直写 + 回读一致 |
| 写值：记事本正文 | 中文 0.85s，回读一致 |

## 已知限制

- **Electron / Qt / 自绘界面**读不全控件树（如 DSH 自己的窗口只有 5 个元素）——这类窗口只能截图 + 坐标，靠 `screen_shot` 辅助
- **管理员权限窗口**：目标进程完整性级别高于本进程时读不到 UIA 树。判据：本机 `EnableLUA=0`（UAC 关）时所有进程都是 High，实测任务管理器 23 控件可读；UAC 开着的机器要「以管理员身份运行 DSH」，或者退回 `screen_shot` + 坐标点击（截图不受 UIPI 限制）
- **UWP**：部分控件 UIA 激活无效，靠 `AttachThreadInput` 顶前台后鼠标点
- **UWP 卡启动画面**：内容没渲染时 UIA 只剩下标题栏三个按钮 + 一个空 Pane。这时 `screen_map` 会返回 `readability=none`、`window_ready=false`，**不要按坐标瞎点**——先 `screen_wait` 等控件出现，或关掉重开应用
- **按屏幕区域找图只看当前可见像素**：窗口被别的窗口挡住时，同样的图在屏幕上就是找不到——这时传 `window`，会按窗口自身像素（PrintWindow）找，抓不到就把窗口顶前台再按窗口矩形截屏。
- 宿主机上的窗口可以点；**虚拟机内部**（VMware 窗口里）是另一台机器的画面，只能截图 + 坐标点，UIA 进不去

## 更新记录

> **怎么读这份记录**：0.2.2～0.2.6 是 11-12 晚到 11-13 中午连续五版，围着同一批薄弱点（读得准、点得准、报得准）打。后一轮里有不少是**前一轮没覆盖到的同类入口**，不是同一个 bug 修两遍。所以每条前面标了：
> **【新增】** 第一次做的能力 ・ **【补漏】** 上一轮只做了一半、这轮补全（写清上一轮做到哪） ・ **【修复】** 实测或逐行核出来的 bug。

### 五版总览

| 版本 | 时间 | 这一轮在干什么 |
|---|---|---|
| 0.2.2 | 11-12 22:55 | 模板定位：给张图就按图点，不靠估坐标 |
| 0.2.3 | 11-12 23:41 | 可读性判断纠偏：卡启动画面的窗口不再被报成"可放心操作" |
| 0.2.4 | 11-13 00:54 | 全量复查：名字匹配分层、按窗口找图、防呆错误码、回归落地 |
| 0.2.5 | 11-13 10:36 | 补动作类：hover / scroll / drag |
| 0.2.6 | 11-13 11:48 | 收口：安全闸门、服务自愈、参数兜底、回归加满 |

### 0.2.6（11-13 11:48）安全闸门 + 服务自愈

**安全闸门（避免点到 / 写到别的窗口）**

- 【补漏】用 `name/aid/type` 定位控件时必须给 `window`，否则 `MISSING_WINDOW`。0.2.4 只给 `map` / `wait` 加了这道闸，`click/get/set/type/hover/scroll/drag` 还留着口子——`find_window("")` 里 `"" in 任意标题` 恒真，会落到第一个顶层窗口。实测空 window + aid 读到了计算器的按钮；这轮补齐其余 7 个入口。
- 【新增】`x/y` 必须成对，只给一个报 `BAD_POINT`；同时给 `target` 和 `x/y` 时按坐标点并在结果里回 `warning`（和 hover/scroll/drag 统一口径）。
- 【新增】`drag` 起点终点必须明给（`MISSING_FROM` / `MISSING_TO`）。以前缺省会从窗口中心开始拖——实测真拖了一把。
- 【新增】`screen_type` / `screen_key` 顶不到前台时报 `WINDOW_NOT_FOREGROUND` 且不发键；以前会照发，键落到别的窗口里。

**服务层**

- 【修复】单线程 + HTTP/1.1 keep-alive 会被一条闲连接堵死：客户端不主动关连接时 handler 停在"读下一个请求"，端口还在监听 → 壳的 `portOpen` 判"活着" → 后续调用全排到 60s 超时。改成 HTTP/1.0（每请求一条连接）+ 连接超时 10s。0.2.4 加的"探端口"只是权宜，救不了这一条。
- 【新增】服务自愈：`/health` 返回源码指纹 `build`，壳比对本地指纹，不一致就调 `/shutdown` 让旧进程退、再拉新的。Windows 上父进程退出不带子进程，改完源码旧服务常驻，"改了不生效"就是这么来的。
- 【修复】端口被占时打印明确原因并 `exit 3`；`SendInput` 失败改读 `kernel32.GetLastError()`（`ctypes.get_last_error()` 对 `windll` 恒为 0，以前报 `err=0` 会把人带偏）。
- 【修复】壳侧 `spawn` 挂 `error` 监听（没有监听时启动失败是未捕获异常，能带崩宿主）；python 探测结果缓存、超时 12s→6s；并发拉起冷却 20s（避免两个 python 同时往 vendor 里 pip 装）。

**动作与结果**

- 【修复】点完控件可能已经被销毁（点"确定"关窗、点"删除"关对话框），此前组装结果时读 `c.Name` 抛 `ElementNotAvailableError` → 500 → 模型重试 → 重复点。现在 `_safe_desc()` 兜底并标 `gone`。
- 【修复】`verified` 改三态：截不到图（窗口被挡 / 尺寸变化）时给 `null` + warning，不再把"没法验证"说成"白点了"——后者会让模型重复点。
- 【补漏】UWP 卡启动画面时 `map` 不重读。0.2.3 修的是**判据**（认得出"只剩外壳"），这里修的是**动作**：原来只在"一个控件都没有"时顶前台重读，而卡启动画面时能读到 4 条窗口装饰，于是不触发。现在"非前台 + 读到的全是窗口装饰"也重读一次（重读前清空 items，避免重复条目）。
- 【修复】右键菜单合并只收本窗口进程的 `#32768`，别的程序开着的菜单不再混进这张表。

**参数与安全**

- 【新增】跨站请求守卫：带 `Origin` 的请求一律 403，POST 必须是 `application/json`。此前任意网页用 `fetch(mode:'no-cors', content-type:'text/plain')` 发 simple request 就能驱动键鼠（不触发预检）。
- 【补漏】参数兜底。0.2.4 的防呆只覆盖 `map` / `wait` / `key` / `type` 几个入口，其余是裸的——给 2 个数量的 `region` 直接 500。这轮：`region` 必须 4 个整数且 x1<x2（`BAD_REGION`）；`depth/max_items/amount/steps/repeat/delay_ms/wait/timeout/threshold` 全部夹紧（`/wait` 上限 55s，避免把唯一线程占死）；字符串 `"false"` 不再被当成 True；`mode` 只认 unicode/vk（`BAD_MODE`）；`find` 只给 region 时不再被窗口矩形覆盖。

**其它**

- 【修复】`_grab_window_print` 中途抛异常时 GDI 句柄全归还（以前每次失败漏 3 个；Electron/GPU 窗口失败是常态）。
- 【新增】回归 45 → **55 条**：新增空 window、坏坐标、缺起终点、坏 region、坏 mode、Origin 守卫、坏 depth、坏 wait 八类用例；计算器改成"等就绪，否则 SKIP"（不再因为应用自己卡启动画面误报 5 条 FAIL）。**55 条全过**。

### 0.2.5（11-13 10:36）补动作类

- 【新增】`screen_hover`：真鼠标停到点上，报告这期间新冒出来的弹层（子菜单 `#32768`、tooltip），带每项的名字和物理坐标——三级菜单不用再猜悬停多久、不用整屏截图找菜单。
- 【新增】`screen_scroll`：滚 up/down/left/right，`amount` 格（1 格 = 120）。有 UIA ScrollPattern 就报 `scroll_before/after/delta`；没有就截控件左上角一块比画面，给 `changed_px`/`verified`——不假装成功。
- 【新增】`screen_drag`：起点终点可给控件名或物理坐标，按 `duration_ms` 分 `steps` 步移动（一次跳过去很多程序会当单击）。返回 `changed_px`、`window_rect_before/after`、`window_moved`。
- 【修复】弹层刚出现时子项读不到（动画/延迟创建）→ 加 4 次重试（0.15s）。
- 【修复】`_center()` 收到 list 抛 `'list' object has no attribute 'left'`，被 `except` 吞掉，表现成"弹层找到但 items=0"。
- 校验坑（省得下次再查）：滚动对比不能截控件中心——记事本这种宽控件，中心是一大片空白，滚了也看不出像素变化，已改成截控件左上角。
- 实测：资源管理器右键 → 悬停「发送到」抓回二级菜单 8 项（传真收件人 ×2 / 蓝牙设备 / 文档 / 压缩(zipped)文件夹 / 邮件收件人 / 桌面快捷方式 / dshare(Z:)），每项带中心坐标。
- 回归 37 → **45 条**，新增 scroll 上下/坏方向、hover 正常/找不到控件、drag 拖窗口/坏按键/起点不存在；45 条全过（拖窗口实测位移 +180/+90，`changed_px` 38286）。

### 0.2.4（11-13 00:54）全量复查

- 【新增】回归用例落到 `tests/gui_regression.py`（37 条，覆盖 11 个工具的正常/异常路径、UWP、右键菜单、模板匹配），改完插件跑一遍再提交。
- 【修复】名字匹配按真实语义分层：精确（去掉 `编辑(E)` / `Save(&S)` 这类助记键后缀）→ 前缀 → 包含。以前「编辑」会被包含匹配抢去点「文本编辑器」；现在精确对上菜单项，模糊命中多个候选时回 `match`/`warning`。
- 【新增】`screen_find` 支持 `window`：窗口被别的窗口挡住时先按窗口自身像素（PrintWindow）找，抓不到就顶前台再按窗口矩形截屏；返回带 `source`（printwindow / screen）。
- 【新增】坐标点击回 `fg_before`：目标窗口不在前台时明确警告"这一下可能落到别的窗口上"，不再闷头当成功。
- 【修复·权宜】服务存活判定：`/health` 2.5 秒没回不再直接判死，先探端口（单线程服务被 `screen_wait` 占住时，旧逻辑会误判成没起来、再 spawn 一个绑不上端口的）。**这条只解决误判，真正被闲连接堵死的问题 0.2.6 才修。**
- 【新增】防呆错误码：`map`/`wait` 空窗口名 `MISSING_WINDOW`、未知按键 `BAD_KEY`（以前是 500）、`via=invoke` 但控件没有 Invoke 模式 `NO_INVOKE_PATTERN`、`type` 异常 `TYPE_FAILED`。**这几道闸只盖了这几个入口，其余入口 0.2.6 补。**
- 【修复】`set`/`type` 回读改 ValuePattern → TextPattern → Name 三级（键盘输入的控件常常没有 ValuePattern，以前会误报失败）。
- 【修复】`screen_windows` 去掉 UWP 重复项（同名有 ApplicationFrameWindow 外壳时不再单列 CoreWindow）。
- 【修复】`map` 认 depth 截断：到了遍历上限还有孩子就降为 partial 并在 hint 里说明，避免"depth 给小了看着像空窗口却报 full"的假象。
- 回归 37 条全过，另跑长文本 308 字、emoji、清空、并发与压力（20 次校准 + 15 次 map 无衰减）。

### 0.2.3（11-12 23:41）可读性判断纠偏

- 【修复】`readability` 误判：UWP 标题栏按钮名带应用名（`关闭 计算器`、`使 计算器 最大化`），旧的精确名比对判不出装饰，于是"卡在启动画面、只剩外壳"的窗口被报成 `full`、提示"可以放心按控件操作"。现在按 automationId + 正则 + 几何（窗口顶部 48px）三层判装饰，内容区一个控件都没有时返回 `readability=none` + `window_ready=false`，并直接给出处置办法。
- 【修复】模板匹配防假阳性：`TM_CCOEFF_NORMED` 在纯色/均匀区域会给虚高分（那次 match 到一片空白蓝底），现在比对匹配块与模板的纹理强度，明显偏平返回 `FLAT_PATCH`，不瞎点。

### 0.2.2（11-12 22:55）模板定位点击

- 【新增】`screen_click` 加 `template` / `threshold`：给模板就先按图定位再点，找不到报 `TEMPLATE_NOT_FOUND`、不瞎点，省得人眼估坐标。

## 许可

MIT

