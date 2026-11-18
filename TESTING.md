# dsh-gui 插件验收记录

测试机：Windows 10 19045，1920×1080，DPI 100%，Per-Monitor V2。
服务：`gui_server.py` 单线程 HTTPServer，端口 8791。

## 一、正式位验收（desktop profile，2026-11-12 01:5x）

挂载方式：**cordis.patch.yml 的 insert 块**（不是 `dsh.profile.bundles`，见"装配教训"）。

| 用例 | 结果 |
|---|---|
| screen_map 记事本 | 20 控件，每行带精确矩形与中心点 |
| 移窗点击（原位 → +250,+110 → -520,-170 含半出屏） | **3/3**，每次 `fg_ok=true`，菜单均弹出 |
| 中文直写（记事本正文） | `via=valuepattern`，回读一致 |
| 设置窗口搜索框（UWP） | `via=valuepattern`，回读"显示" |
| HTTP 直连（脚本）与 DSH 工具调用 | 两条路都通 |

## 二、各窗口控件树（screen_map）

| 窗口 | 控件数 | 耗时 |
|---|---|---|
| 记事本（最大化） | 20 | 0.02s |
| 资源管理器 | 88（55 可用） | 0.23s |
| 设置（UWP） | 66 | 0.13s |
| Edge 网页 | 28 | 0.09s |
| DSH 自己（Electron） | 5 | 0.02s |
| **WPS 文档主窗口（Qt 自绘）** | **1**（只有"新建"） | 0.02s |
| **WPS 打印对话框（原生 Win32）** | **72** | 0.05s |

WPS 结论：**主窗口读不到树，只能截图+坐标；它弹出的原生对话框可以完整读、按控件点**。
实测：对着打印对话框用 target 索引点"关闭"，一次命中；而同一按钮用肉眼估坐标点偏了 28px 没反应——这正是"看控件矩形、别猜坐标"的现场证据。

## 三、装配教训（重要）

1. **file: 本地包进 `dsh.profile.bundles` 列表不会生效**（插件加载了但不注册工具，工具表里没有）。
   正确挂法：`cordis.patch.yml` 末尾加
   ```yaml
   - insert:
       - id: gui
         name: 'dsh-gui'
         config: { port: 8791, python: '', autoStart: true }
   ```
   并把 `dsh-gui` 从 bundles 列表移除（避免重复）。改完重启 DSH。
2. **node_modules 里的插件是安装时的副本**：改 `_tools/dsh-gui` 源码后必须同步到
   `<profile>\node_modules\dsh-gui\lib\gui_server\`，
   再重启 8791 服务（`gui_core.py` 是 Python 常驻服务，不随 DSH 重启）。
   本次就踩了：_rewrap 修复没同步，导致 `'Control' object has no attribute 'GetValuePattern'`。
3. 参数写法：DSH 工具用扁平参数（`window`/`name`/`aid`/`type`/`value`）；HTTP 接口用 `target` 嵌套
   （`{"title":..., "target":{"name":...}}`）。

## 四、踩过的坑与修法

1. **`WinError -2147221008 尚未调用 CoInitialize`**：ThreadingHTTPServer 每请求线程缺 COM 初始化。
   修法：单线程 HTTPServer。**别**在主线程显式 `ole32.CoInitialize()`——实测 UIA 卡死 41.5 秒。
2. **`'Control' object has no attribute 'GetValuePattern'`**：`auto.Control()` 搜出来是通用类型。
   修法：`_rewrap()` 按 `ControlTypeName` 用对应子类重建。
3. **UWP 窗口顶不到前台**：`SetActive()` 无效。
   修法：`AttachThreadInput(tid_me, tid_fg, True)` + `SetForegroundWindow`；仍不行先发一次 Alt 抖动。
4. **最大化窗口按名字找菜单错位**：早期 `resolve` 用包含匹配且不校验名字，把标题栏"系统"菜单当成"文件(F)"。
   修法：精确匹配优先 + 校验 `c.Name == 目标` + BFS 先完全相等再包含。

## 五、未覆盖 / 已知风险

- WPS 主窗口（Qt）只能截图+坐标，tab 切换时好时坏（本次"插入"没切过去、"页面"误触了打印——都靠截图发现并回头）
- 需要管理员权限的窗口（任务管理器等）——非提权进程读不到树
- 浏览器内嵌 web 内容（DOM 而非 UIA）——需另外的网页通道
- 多显示器 / 高 DPI 缩放（只测了 100% 单屏）
- 虚拟机内部画面（VMware 窗口）——UIA 进不去 guest，只能截图 + 坐标

## 六、键盘输入（2026-11-12 整合）

来源：`_tools/uitype.py`（SendInput KEYEVENTF_UNICODE 逐字）、`uikey.py`（VK 组合键）、`vktype.py`（VK 逐字符，给 VMware 用），三合一进 `screen_type` / `screen_key`。

- `screen_type`：unicode 模式逐字打（中文/符号不依赖输入法）、vk 模式走虚拟键；可先聚焦控件、可打完回车；给 target 时回读值。
- `screen_key`：`ctrl+a` / `ctrl+shift+end` / `enter` / `f5` 这类，可多组、可重复。
- 两条路都带自检：`OpenInputDesktop` 拿不到就直接报 `INPUT_DESKTOP_UNAVAILABLE`，不再冒出 `SendInput ret=0 err=5` 这种模糊错误。

**实测状态**：2026-11-12 08:00 屏幕锁定（`GetForegroundWindow=0`、`OpenInputDesktop=0`），SendInput 被系统拒绝（err=5），真实打字未测。解锁后补测记事本中文、Ctrl+A/Delete、资源管理器搜索框。

## 七、显示器校准（2026-11-12）

`screen_calibrate` 枚举显示器（`EnumDisplayMonitors` + `GetDpiForMonitor`），锁屏也能读。

**本机实测**：`DISPLAY1` rect `[0,0,1920,1080]`、**dpi `[120,120]`、scale `1.25`**、`process_dpi_aware=per-monitor`。

★ 这条纠正了之前的错误判断：一直以为本机是 100% 缩放（`GetDeviceCaps` 在 DPI-aware 进程里返回物理分辨率，看不出缩放）。实际系统缩放是 125%，只是进程按 Per-Monitor V2 感知，坐标直接用物理像素，所以点准。**换台机器或改缩放，这个假设就不成立**——所以校准要现算，不写死。

`screen_shot` 现在返回 `origin` + `rect`：图内坐标 + origin = 屏幕物理坐标，调用方不需要知道 DPI。

## 八、网站登录与页面点击实测（2026-11-12 08:1x，解锁后）

| 场景 | 操作 | 结果 |
|---|---|---|
| 记事本清空 | `ctrl+a` + `delete` | ok，1.31s |
| 记事本中文打字 | `screen_type` 14 字（delay 2ms） | ok，**0.24s**，回读一致 |
| 记事本 vk 模式 | `screen_type mode=vk` 7 字 | ok |
| Bing 搜索 | `ctrl+l` → 输入「测试关键词」→ `enter` | 成功，页面跳到搜索结果（5280 条） |
| Bing 图片标签 | 坐标点击 (317,267) | 成功切到图片搜索，0.462s |
| **OA 登录** | 点账号框 → 输「testuser」→ 点密码框 → 输密码 → 点登录 | **登录成功**，进主页显示「testuser，您好！」 |
| OA 页面导航 | 点「纪检监察」图标 (196,555) | 打开纪检监察业务页 |

**关键教训（坐标点击）**：
1. 第一次点「图片」标签我按截图估 (242,277)，实际在 **(317,267)**——偏了 75px，点到了下拉面板上。正确做法：**截局部区域放大看**（`screen_shot region`），别在全屏图上目测。
2. 点 OA 的「项目管理」点了「纪检监察」：图标间距 115px，估错一档。同样的毛病。
3. 网页元素 UIA 读不到（Edge 窗口 29 个控件全是浏览器外壳），只能坐标点；**坐标点击必须配合截图验证**。

**结论**：键盘（中文/密码/快捷键）和控件点击稳；网页内容只能坐标点，准确率取决于截图读得准不准，建议固定流程「截局部 → 读坐标 → 点 → 截图验证」。

## 九、工具层验证（2026-11-12 08:30，重启后）

重启后 9 个工具全部挂载，真实调用（不只用 HTTP 直连）：

| 工具 | 调用 | 结果 |
|---|---|---|
| `screen_calibrate` | 无参 | ok，DISPLAY1 1920x1080 / dpi 120 / scale 1.25 / per-monitor |
| `screen_type` | 17 字中文 delay 2ms | ok，**0.167s** |
| `screen_get` | 读记事本 Edit | ok，值一致 |
| `screen_key` | `ctrl+a` + `delete` | ok，0.371s，回读为空 |
| `screen_map` / `click` / `set` / `wait` / `shot` | 此前已验 | ok |

## 十、弹窗与右键菜单（2026-11-12 08:50）

复杂界面测试暴露两类"UIA 根树看不到"的窗口，已修：

| 问题 | 现象 | 修法 |
|---|---|---|
| 对话框 `#32770` | 画图"关于"弹窗，`/windows` 和 map 都找不到 | `_win32_windows()` Win32 EnumWindows 兜底；`find_window` 用 `ControlFromHandle` 接入 |
| 右键菜单 `#32768` | 资源管理器右键，主窗口树里只有标题栏"系统"菜单 | `_popup_menus()` 枚举独立菜单窗口；map 合并（`popup: true`）；resolve 找不到时在弹窗里找 |

**实测**：画图"关于"弹窗读 **12 控件**、点"确定"关闭；资源管理器右键菜单读 **13 项**、点"刷新(E)" `via=invoke` 成功、菜单关闭。
**新增参数**：`screen_click` 加 `button`（left/right/middle）与 `double`（双击）。
**测试中发现**：计算器(UWP) 83 控件 0.20s、7+8=15 全流程 4/4 点击成功；任务管理器 500 控件 0.65s、性能页/详细信息页切换成功；画图 Ribbon 51 控件。

## 十一、文件保存对话框全流程（2026-11-12 08:55）

画图 `Ctrl+S` → "保存为"对话框（#32770，**73 控件**）→ 导航 → 写名 → 保存，全链路通过：

| 步骤 | 调用 | 结果 |
|---|---|---|
| 打开 | `screen_key ctrl+s` | 弹出"保存为" |
| 读树 | `screen_map` | 73 控件，含树/列表/文件名框(aid=1001)/保存按钮(aid=1) |
| 导航 | `screen_click name="图片 (已固定)"` | `via=mouse` 0.48s |
| 写名 | `screen_set aid=1001 value="test_save.png"` | `via=valuepattern` 回读一致 |
| 保存 | `screen_click name="保存(S)"` | `via=invoke`，文件落盘 23832 字节，标题变"test_save - 画图" |

**坑**：在"此电脑"根目录直接填完整路径 `E:\...\x.png` 点保存会弹错误框"你不能保存到"此电脑"。必须先导航到具体文件夹（树项点击），文件名只写名字。
**错误框也可读**：`#32770 title="mspaint.exe"` 读到"你不能保存到"此电脑"。请选择其他位置。"+确定按钮；其 Invoke 会报 `COMError(-2147220991, '事件无法调用任何订户')` **但实际生效**（报错≠失败，需看窗口是否消失）。

## 十二、复杂界面可读性地图（2026-11-12 09:00）

| 界面 | 技术栈 | 控件数 | map 耗时 | 结论 |
|---|---|---|---|---|
| 记事本 | Win32 经典 | 20 | 0.02s | 全可读 |
| 资源管理器 | Win32 列表 | 86 | 0.21s | 全可读 |
| 画图主界面 | Win32 Ribbon | 51 | 0.15s | 全可读 |
| 画图"关于" | #32770 对话框 | 12 | — | 全可读（Win32 兜底） |
| 画图"保存为" | #32770 对话框 | 73 | — | 全可读，全流程落盘 |
| 右键菜单 | #32768 弹出 | 13 | — | 全可读（弹窗合并） |
| 计算器 | UWP XAML | 83 | 0.20s | 全可读，7+8=15 点击 4/4 |
| 设置 | UWP XAML | 66–93 | 0.13s | 全可读，搜索框可写 |
| 任务管理器 | Win32 表格 | 500(上限) | 0.65s | 全可读，页签切换 ok |
| Edge | Chromium | 5 | 0.09s | **只读外壳**（窗口按钮/标签栏），网页内容读不到 |
| 微信 | Qt 5.15 自绘 | 1 | 0.10s | **完全不可读**，走"截图→读坐标→点→截图验证" |
| DSH 自己 | Electron | 5 | — | 只读外壳 |

**不可读界面标准流程（微信实测）**：`screen_shot window=` 拿图（带 origin）→ 图内坐标 + origin = 物理坐标 → `screen_click x= y=` → `screen_type` 键盘输入 → 再截图验证。微信搜索框 (图内 192,70 → 物理 894,184) 点击并输入"测试"，搜索面板正常弹出。
**Chromium 要点**：网页元素要读 UIA，必须 Edge 带 `--force-renderer-accessibility` 启动（或在设置里开辅助功能树），默认只给外壳控件——所以网页操作还是"截图+坐标"最稳。

## 十三、WPS 文档 + 网站实测（2026-11-12 09:20）

### WPS 文字（Qt 自绘，2 控件不可读）

| 操作 | 结果 |
|---|---|
| 打开 docx | 标题 `test_wps.docx - WPS Office`，map 只有 2 控件（"新建"按钮 + 主窗口） |
| 点文档区域 | (960,540) 定位光标，`fg_ok=true` |
| 输入中文 | `screen_type` 7 字 `GUI测试输入` 0.145s |
| 验证输入 | 截图看到文字进了末尾段落 |
| Ctrl+S 保存 | 0.195s，python-docx 读回 **"含输入文字: True"**（真落盘） |
| 功能区切换 | 点"插入"标签 (587,68) → 面板变成插入页（空白页/封面/表格/图片…） |
| Ctrl+A + Ctrl+B | 全选 → 加粗，截图确认全文变粗 |

**结论**：WPS 完全自绘，UIA 只有 2 个控件，但**截图定位 + 坐标点击 + 键盘输入**全流程可用；保存确认对话框 `KWinMessageContentDlg` 反而可读（5 控件：保存/不保存/取消）。

### 网站

| 站点 | 结果 |
|---|---|
| CSDN | 搜索触发**登录墙模态框**（"请登录后，再进行搜索哦~" 微信扫码/验证码/APP 登录），Esc 可关 |
| **百度地图** | **全流程通过**：关广告浮层 → 点搜索框输入"鄱阳收费站" → 点搜索按钮 → 结果列表 5 条 → 点第 1 条 → 地图定位 + 详情面板（地址/相册/评论） |
| GitHub | `ERR_CONNECTION_TIMED_OUT`——国内直连不行，要挂代理（`_tools\mj.ps1`） |

**网页操作结论**：Chromium 网页元素读不到 UIA，一律"截图→读坐标→点→截图验证"；真实网站常见弹窗（CSDN 登录墙、百度地图广告），先看截图再动手，别盲点。

## 十四、准头三件套与准确率实测（2026-11-12 12:10）

### 三个新能力

| 能力 | 实现 | 实测 |
|---|---|---|
| `screen_find` | opencv 模板匹配（imdecode 兼容中文路径） | 百度地图"路况/地铁"按钮 **score=1.0**，坐标误差 **1px**（1535,150 对真实 1534,150） |
| `screen_click verify` | 点前后整窗截图对比，返回 `changed`（比例）+ `changed_px`（像素数），`>300` 判"有反应" | 点"路况" `px=81244` OK；点空白 `px≈0` 正确判无反应 |
| `screen_shot grid` | 每 100px 灰线、每 500px 红线并标屏幕坐标 | 19120×360 测试图网格清晰，读数直接对红线 |

### 准确率实测（标准化脚本 `_tmp/acc_test2.py`）

**A 组（控件模式，35 次）**：记事本菜单 6 次、画图标签 2 次、任务管理器标签 7 次、计算器按钮 20 次。
- 点击发出 **35/35 = 100%**（重定位保证，窗口挪了也不点偏）
- 界面有可检测反应 **27/35 = 77%**
- 8 次"无反应"全是计算器数字/运算符键：`px` 40–360 低于阈值，但显示区确实有变化（窗口大、数字区小）
- 平均 **1.17s/次**

**B 组（模板+坐标，2 次）**：百度地图工具栏
- find score 1.0、误差 1px；点击 2/2 有反应 **100%**

### 结论

1. **控件模式点空率基本为 0**——因为点的是"重新查出来的矩形中心"，不是记忆坐标。
2. **坐标模式的短板是人眼估坐标**；用 `screen_find` 模板匹配后降到 1px，这条路可以替代目测。
3. `verify` 的阈值 300px 对小窗口（计算器显示区）偏严，实际使用看 `changed_px` 数值自己判断更稳。
4. **中文路径坑**：`cv2.imread` 读不了中文路径，必须 `open(rb)` + `cv2.imdecode`（已修）。

## 十五、把经验写进代码（2026-11-12 12:15）

用户问"都写进插件代码了吗？不要依托你的记忆和经验"——原来的回答是**没有**，现在补上：

| 原来靠记忆 | 现在写进代码 |
|---|---|
| "这窗口能不能读控件？" | `screen_map` 返回 **`readability`（full/partial/none）+ `hint`**，自动判断并给出下一步 |
| "点完得截图看有没有点中" | `screen_click` 的 `verify` **默认开**，返回 `changed_px/verified` |
| "找不到窗口先看有哪些" | 新增 **`screen_windows`** 工具（列出含对话框/弹窗的全部窗口） |
| "找不到控件怎么办" | `TARGET_NOT_FOUND` 返回 **hint**：先 map 看名字 / 可能被弹窗挡 / 自绘走截图+find |
| "窗口没找到怎么办" | `WINDOW_NOT_FOUND` 带 hint：先 screen_windows 看真实标题 |
| "控件矩形空怎么办" | `EMPTY_RECT` 带 hint：滚动/展开/关弹窗后再点 |

**实测**：记事本 → `readability=full`、"20 个控件、20 个有名字，可以放心按控件操作"；DSH（Electron）→ `readability=none`、"读不到任何控件。多半是自绘/Electron/Qt 窗口：改用 screen_shot + screen_find + 坐标"。

**判断规则**（`_readability`）：0 控件→none；有名字<3→none；全是 Pane/Window/Image→none；有名<50%→partial；>200 控件→full（提示用 name/aid 别用类型）。

## 四、0.2.5 动作类回归（2026-11-13）

新增 `screen_hover` / `screen_scroll` / `screen_drag`，回归从 37 条加到 **45 条，全部通过**（`tests/gui_regression.py`，报告 `tests/gui_regression_report.txt`）。

| 用例 | 结果 |
|---|---|
| scroll 记事本向下 5 格 | `changed_px=15233`，`verified=true` |
| scroll 记事本向上 5 格 | `changed_px=15233`，`verified=true` |
| scroll 坏方向 | `BAD_DIRECTION` |
| hover 记事本菜单项「编辑」 | `point=[716,279]`，窗口已在前台（popups=0） |
| hover 资源管理器「刷新」 | 抓到 `tooltips_class32` 弹层 1 个（tooltip 文本 UIA 不暴露） |
| hover 不存在的控件 | `TARGET_NOT_FOUND` |
| drag 记事本标题栏 +180/+90 | 窗口 `[614,229,1630,935] → [794,319,1810,1025]`，`window_moved=true`，`changed_px=38286` |
| drag 坏按键 / 起点不存在 | `BAD_BUTTON` / `TARGET_NOT_FOUND` |

**踩到的坑（已修）**：滚动校验最初截「控件中心」400×400，记事本这种宽控件中心全是空白，明明滚了也报 `changed_px=0`。改成截控件左上角 560×420（文字/图标都在左上）后恢复正常。

### 悬停读子菜单（2026-11-13 12:0x 补测，用户追问"悬浮菜单测试了吗"）

场景：资源管理器右键 `__pycache__` → 悬停「发送到」→ 看能不能把二级菜单读出来。

结果：`popup_count=1`、`class=#32768`、`rect=[1004,720,1339,956]`、**items=8**，每项带中心坐标：
`传真收件人`(1171,738) / `传真收件人`(1171,768) / `蓝牙设备`(1171,796) / `文档`(1171,824) /
`压缩(zipped)文件夹`(1171,852) / `邮件收件人`(1171,880) / `桌面快捷方式`(1171,908) / `dshare (\\192.168.80.1) (Z:)`(1171,937)。
证据：`_tmp\hover_menu_test.json`。

**这一追追出两个真 bug（都已修）**：
1. 弹层刚出现时 `ControlFromHandle` 会读到空子项（动画/延迟创建）→ 加 4 次重试（每次 0.15s），读到为止。
2. `_center()` 吃的是 RECT，代码里却把 `_rect()` 返回的 **list** 传了进去 → 抛 `AttributeError: 'list' object has no attribute 'left'`，被 `except` 吞掉，表现就是"弹层找到了但 items=0"。改成按下标算中心。

## 五、悬停 / 网页悬浮菜单点击（2026-11-13，重启后实机）

| 场景 | 动作 | 结果 |
|---|---|---|
| 桌面悬停 | 资源管理器「刷新」按钮悬停 | 抓到 `tooltips_class32` 弹层（tooltip 文本 UIA 不暴露） |
| 京东首页悬浮分类 | hover「电脑 / 办公」→ 浮层展开 | 截图确认浮层（笔记本 / 平板 / 游戏本…） |
| 京东浮层内点击 | 点「笔记本」 | **新标签打开 `passport.jd.com/new/login.aspx?ReturnUrl=…search.jd.com…`**（京东登录墙，链接命中） |
| 皖建云商悬浮分类 | hover「钢材」→ `钢材 > 螺纹钢 盘螺` | 浮层出现，`changed_px` 明显 |
| 皖建云商浮层内点击 | 点「螺纹钢」 | **新标签打开 `https://www.wjys365.com/mall/view/index/goodslist?navId=aa5462ec61ad43ceb53f0e7813d44e1f`** |

**结论**：网页浮层是 DOM，不是窗口，所以 `hover` 的 `popup_count` 恒为 0（只认新顶层窗口），网页流程固定是
`screen_hover(x,y)` → `screen_shot` 看浮层 → `screen_click(x,y)` → 核对新标签/URL。

**踩的坑**：坐标点击不激活窗口。测试中途 DSH 窗口被叫到前台（rect 从 `-32000` 变 `150,1,1769,1037`），
之后几下全点在 DSH 上，表现是 `changed_px=0`、页面不动。坐标点击前先确认目标窗口在前台。

## 六、0.2.6 最后一轮检测与修复（2026-11-13）

跑法：`python tests/gui_regression.py`（打 8791 的 HTTP，55 条）。本轮结果 **55 PASS / 0 SKIP / 0 FAIL**。

| 类别 | 用例 | 结果 |
|---|---|---|
| 基础 | health（含 build 指纹）、calibrate、windows | 通 |
| 记事本 | wait/map/set/get/type/全选覆盖/菜单点击 | 通 |
| 截屏 | 窗口 / region / annotate+grid / 越界 | 通 |
| 模板匹配 | 按窗口找（PrintWindow+顶前台）、现场模板正例、反例 NO_MATCH | 通 |
| UWP 计算器 | map、清除键、7+8=15 | 通 |
| 右键菜单 | 独立 #32768 窗口、点菜单项 | 通 |
| 异常路径 | 窗口/控件/模板不存在、wait 超时、坏按键、无 Invoke | 通 |
| 动作类 | scroll 上下/坏方向、hover、drag 窗口/坏按键/缺起终点 | 通 |
| 新闸门 | 空 window、坏坐标、缺起终点、坏 region、坏 mode、Origin 403、坏 depth/wait | 通 |

**这轮抓到的环境坑（不是插件 bug）**：本机计算器进程从 09:24 起一直卡在启动画面（蓝底 + 图标），
UIA 只能读到 4 条窗口装饰。旧测试直接断言 `count>20`，于是连带 5 条用例报 FAIL，看着像插件坏了。
杀掉 `CalculatorApp` 重开后立刻 `count=120 ready=True`。现在测试先 `wait_ready()` 等就绪，等不到记 SKIP 并写明原因。

**服务层验证**：
- `/health` 返回 `build`（`gui_core.py` + `gui_server.py` 的 sha1 前 12 位），改完源码重启服务后指纹必然变化。
- 带 `Origin` 的请求返回 403；非 `application/json` 的 POST 返回 415。
- 空闲连接（连上不发数据）最多占服务 10s（HTTP/1.0 + `timeout=10`），不再无限堵住 accept。
