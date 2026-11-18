# -*- coding: utf-8 -*-
"""
gui_core.py —— 自创 GUI 操作插件的内核
设计要点（取各家优点 + 我们自己的"定点"思路）：
  1. 定点：控件矩形就是物理屏幕坐标，零换算，不靠截图量
  2. 新鲜：动作前重新定位控件、重取矩形；动作后校验前台与状态
  3. 直写：能用 ValuePattern 就不敲键盘
  4. 回读：写完/点完都回读，不成功就明说失败
不做：审计事件、逐动作审批（需要时用参数控制）
"""
import os
import re
import time
import ctypes
import threading

import uiautomation as auto

auto.SetGlobalSearchTimeout(3)
u = ctypes.windll.user32

# UIA 是 COM，多线程要串行
_LOCK = threading.RLock()

TYPE_NAMES = {
    "edit": auto.EditControl,
    "button": auto.ButtonControl,
    "menuitem": auto.MenuItemControl,
    "menu": auto.MenuControl,
    "combobox": auto.ComboBoxControl,
    "checkbox": auto.CheckBoxControl,
    "radio": auto.RadioButtonControl,
    "list": auto.ListControl,
    "listitem": auto.ListItemControl,
    "tree": auto.TreeControl,
    "treeitem": auto.TreeItemControl,
    "text": auto.TextControl,
    "pane": auto.PaneControl,
    "tab": auto.TabItemControl,
    "tabitem": auto.TabItemControl,
    "toolbar": auto.ToolBarControl,
    "statusbar": auto.StatusBarControl,
    "document": auto.DocumentControl,
    "hyperlink": auto.HyperlinkControl,
    "slider": auto.SliderControl,
    "spinner": auto.SpinnerControl,
    "scrollbar": auto.ScrollBarControl,
    "image": auto.ImageControl,
    "group": auto.GroupControl,
    "window": auto.WindowControl,
    "titlebar": auto.TitleBarControl,
    "separator": auto.SeparatorControl,
    "menubar": auto.MenuBarControl,
    "custom": auto.CustomControl,
}


def _rect(r):
    return [int(r.left), int(r.top), int(r.right), int(r.bottom)]


def _center(r):
    return [int((r.left + r.right) // 2), int((r.top + r.bottom) // 2)]


def _win32_windows(include_untitled=False):
    """Win32 EnumWindows：枚举可见顶层窗口。
    UIA 根树看不到走 owned 的对话框（如 #32770 "关于 画图"）和无标题的弹出菜单（#32768），
    include_untitled=True 时把无标题的也一起列出，供弹窗合并使用。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    res = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lparam):
        try:
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0 and not include_untitled:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if not buf.value and not include_untitled:
                return True
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rect))
            res.append({"hwnd": int(hwnd), "title": buf.value, "class": cls.value,
                        "pid": int(pid.value),
                        "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]})
        except Exception:
            pass
        return True
    try:
        u32.EnumWindows(CB(cb), 0)
    except Exception:
        pass
    return res


def _popup_menus(pid=None):
    """当前可见的 Win32 弹出菜单窗口（#32768）：右键菜单是独立窗口，不在目标窗口的 UIA 树里。
    pid 给了就只收这个进程的菜单（菜单窗口属于开它的那个进程）。"""
    out = []
    for ww in _win32_windows(include_untitled=True):
        if ww["class"] != "#32768":
            continue
        if pid is not None and ww.get("pid") != pid:
            continue
        out.append(ww)
    return out


def find_window(title, timeout=3.0):
    """按标题模糊找顶层窗口。UWP 同名有两层：Windows.UI.Core.CoreWindow（内部窗口、控件树为空）
    与 ApplicationFrameWindow（外壳）；必须跳过 CoreWindow 并优先外壳，否则 map 出来 0 控件。

    空标题直接返回 None：`"" in 任意标题` 恒真，不拦的话会落到"第一个顶层窗口"，
    变成"往随便哪个窗口里写字"（2026-11-13 实测：空 window + aid 读到了计算器的按钮）。"""
    if not (title or "").strip():
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            try:
                root = auto.GetRootControl()
                cands = []
                for c in root.GetChildren():
                    try:
                        nm = c.Name or ""
                        if title not in nm:
                            continue
                        if (c.ClassName or "") == "Windows.UI.Core.CoreWindow":
                            continue
                        cands.append(c)
                    except Exception:
                        continue
                if cands:
                    # 同名多实例（如两个"无标题 - 记事本"）优先返回前台那个
                    for c in cands:
                        try:
                            if _is_fg(c):
                                return c
                        except Exception:
                            pass
                    for pref in ("ApplicationFrameWindow", "Chrome_WidgetWin_1", "Notepad", "CabinetWClass"):
                        for c in cands:
                            if c.ClassName == pref:
                                return c
                    return cands[0]
            except Exception:
                pass
        time.sleep(0.15)
    for ww in _win32_windows():
        if title in ww["title"] and "CoreWindow" not in (ww.get("class") or ""):
            try:
                c = auto.ControlFromHandle(ww["hwnd"])
                if c is not None:
                    return c
            except Exception:
                continue
    return None


def _num(v, default, lo, hi):
    """把外部传进来的数字压进合理区间：None/字符串/NaN 一律回默认值。
    Node 侧统一 60s 超时，Python 这边不封顶的话，一个 /wait 300 就能把唯一线程占死 5 分钟，
    期间 /health 都不通（单线程服务），表现就是"插件突然全废"。"""
    try:
        f = float(v)
    except Exception:
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(f, hi))


def _bool(v):
    """外部参数转布尔：curl / JSON 里常见的 "false"、"0" 不能被 bool() 当成 True"""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s not in ("", "0", "false", "no", "off", "none", "null")


def _need_window(title, target=None):
    """动作类调用的统一闸门：给了 target 就必须给 window。

    不带 window 时 UIA 会从桌面根开始全树搜同名控件（resolve2 传 searchFromControl=None），
    可能命中别的程序的同名按钮——读值是脏数据，点击/写值就是事故。
    纯坐标动作（x/y）不受限制：物理坐标本来就是绝对的。"""
    if target and not (title or "").strip():
        return {"ok": False, "error": "MISSING_WINDOW",
                "hint": "用 name/aid/type 定位控件时必须给 window（窗口标题关键词）："
                        "不给窗口就会从整个桌面找同名控件，可能命中别的程序。"
                        "只有直接用 x/y 物理坐标时才不需要 window"}
    return None


def list_windows():
    with _LOCK:
        out = []
        seen = set()
        root = auto.GetRootControl()
        for c in root.GetChildren():
            try:
                if c.ClassName in ("Progman", "WorkerW"):
                    continue
                nm = c.Name or ""
                if not nm:
                    continue
                r = c.BoundingRectangle
                if r.right - r.left <= 0 or r.bottom - r.top <= 0:
                    continue
                hw = c.NativeWindowHandle
                seen.add(hw)
                out.append({
                    "hwnd": hw,
                    "title": nm,
                    "class": c.ClassName,
                    "pid": c.ProcessId,
                    "rect": _rect(r),
                })
            except Exception:
                continue
    # Win32 兜底：UIA 根树里没有的 owned 对话框
    for ww in _win32_windows():
        if ww["hwnd"] in seen:
            continue
        if ww["class"] in ("Progman", "WorkerW", "Shell_TrayWnd"):
            continue
        seen.add(ww["hwnd"])
        out.append(ww)
    # UWP 同一个 App 会出来两口窗：ApplicationFrameWindow（外壳）和 Windows.UI.Core.CoreWindow（内容）。
    # CoreWindow 是重复项（find_window 也跳过它），列出来只会让人挑错——同名有外壳就去掉它。
    frames = {(o.get("title") or "") for o in out
              if (o.get("class") or "") != "Windows.UI.Core.CoreWindow"}
    return [o for o in out
            if not ((o.get("class") or "") == "Windows.UI.Core.CoreWindow"
                    and (o.get("title") or "") in frames)]


def _probe_patterns(c):
    """探测控件支持哪些模式（不抛异常）"""
    pats = []
    for name, getter in (
        ("value", "GetValuePattern"),
        ("invoke", "GetInvokePattern"),
        ("toggle", "GetTogglePattern"),
        ("selection", "GetSelectionItemPattern"),
        ("expand", "GetExpandCollapsePattern"),
        ("text", "GetTextPattern"),
    ):
        try:
            fn = getattr(c, getter, None)
            if fn is None:
                continue
            if fn() is not None:
                pats.append(name)
        except Exception:
            pass
    return pats


def map_window(title, depth=6, include_unnamed=False, max_items=400):
    """返回窗口控件表：每条含编号/类型/名字/ID/物理矩形/中心点/可用/模式"""
    depth = int(_num(depth, 6, 1, 40))
    max_items = int(_num(max_items, 400, 1, 5000))
    if not (title or "").strip():
        return {"ok": False, "error": "MISSING_WINDOW",
                "hint": "window 不能为空：先 screen_windows 看真实标题，再传标题里的关键词"}
    w = find_window(title)
    if w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}

    with _LOCK:
        items = []
        counter = [0]
        truncated = [False]

        def walk(cur, d):
            if d >= depth:
                # 到了遍历深度上限还有孩子 = 树被截断了，要在结果里说清楚，
                # 不然 depth 给小了会看到"只有外壳却报 full"的假象（2026-11-13 复查发现的同类坑）
                try:
                    if d == depth and cur.GetChildren():
                        truncated[0] = True
                except Exception:
                    pass
                return
            if len(items) >= max_items:
                truncated[0] = True
                return
            try:
                kids = cur.GetChildren()
            except Exception:
                return
            for ch in kids:
                if len(items) >= max_items:
                    return
                try:
                    nm = ch.Name or ""
                    aid = ch.AutomationId or ""
                    if not include_unnamed and not nm and not aid:
                        walk(ch, d + 1)
                        continue
                    r = ch.BoundingRectangle
                    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
                        walk(ch, d + 1)
                        continue
                    counter[0] += 1
                    items.append({
                        "i": counter[0],
                        "type": ch.ControlTypeName,
                        "name": nm[:120],
                        "aid": aid[:80],
                        "rect": _rect(r),
                        "center": _center(r),
                        "enabled": bool(ch.IsEnabled),
                        "depth": d,
                        "patterns": _probe_patterns(ch),
                    })
                except Exception:
                    pass
                walk(ch, d + 1)

        walk(w, 0)

        # UWP 应用在后台/启动中时 UIA 树不展开。旧判据只看"一个控件都没有"，
        # 但卡在启动画面时其实能读到 4 条（标题栏 + 最小化/最大化/关闭），
        # 2026-11-13 计算器卡启动画面那次就没触发重读，直接报 readability=none 让人去截图。
        # 现在改成：窗口不是前台 + 读到的全是窗口装饰 → 顶到前台重读一次。
        if ((w.ClassName or "") in ("ApplicationFrameWindow", "Windows.UI.Core.CoreWindow")
                and not _is_fg(w) and (not items or all(_is_chrome(i) for i in items))):
            try:
                _activate(w)
                time.sleep(0.4)
                items.clear()          # 重读前必须清空：不清会把这些条目追加第二遍
                counter[0] = 0
                walk(w, 0)
            except Exception:
                pass

        # 合并当前可见的弹出菜单：右键菜单是独立 #32768 窗口，不在目标窗口的 UIA 树里。
        # 只收本窗口进程的菜单——别的程序（或别的 DSH 会话）开着的菜单不该混进这张表。
        try:
            own_pid = int(w.ProcessId)
        except Exception:
            own_pid = None
        for pw in _popup_menus(own_pid):
            try:
                pc = auto.ControlFromHandle(pw["hwnd"])
                if pc is None:
                    continue
                for ch in pc.GetChildren():
                    try:
                        nm = ch.Name or ""
                        if not nm:
                            continue
                        r = ch.BoundingRectangle
                        if r.right - r.left <= 0 or r.bottom - r.top <= 0:
                            continue
                        counter[0] += 1
                        items.append({
                            "i": counter[0],
                            "type": ch.ControlTypeName,
                            "name": nm[:120],
                            "aid": (ch.AutomationId or "")[:80],
                            "rect": _rect(r),
                            "center": _center(r),
                            "enabled": bool(ch.IsEnabled),
                            "depth": 0,
                            "patterns": _probe_patterns(ch),
                            "popup": True,
                        })
                    except Exception:
                        pass
            except Exception:
                pass

        info = {
            "title": w.Name,
            "class": w.ClassName,
            "pid": w.ProcessId,
            "hwnd": w.NativeWindowHandle,
            "rect": _rect(w.BoundingRectangle),
            "foreground": _is_fg(w),
        }
        # 客户区顶端（标题栏底边）：判"没名字的标题栏按钮"用真实几何，比固定 48px 稳
        client_top = None
        try:
            from ctypes import wintypes as _wt
            hwnd = info["hwnd"]
            if hwnd:
                pt = _wt.POINT(0, 0)
                u.ClientToScreen(hwnd, ctypes.byref(pt))
                client_top = int(pt.y)
        except Exception:
            client_top = None
    readability, hint, ready = _readability(items, (w.ClassName or ""), info.get("rect"), client_top)
    if truncated[0] and readability == "full":
        readability = "partial"
        hint = (hint + "。注意：遍历到 depth=%d 上限就停了，更深的控件没列出来"
                "（要全一点就加大 depth，默认 6）" % depth)
    return {"ok": True, "window": info, "count": len(items), "items": items,
            "readability": readability, "hint": hint, "window_ready": ready,
            "truncated": bool(truncated[0])}


# 窗口装饰（标题栏按钮）——名字有中英两套，中文还带应用名（「关闭 计算器」）
_DECO_AIDS = {"Minimize", "Maximize", "Close", "Restore", "TitleBar"}
_DECO_NAMES = {"最小化", "最大化", "关闭", "还原",
               "Minimize", "Maximize", "Close", "Restore"}
_DECO_RE = re.compile(r"^(使\s*)?(最小化|最大化|还原|关闭)(\s|$)|^(Minimize|Maximize|Close|Restore)(\s|$)", re.I)
_CONTAINER_TYPES = {"PaneControl", "WindowControl", "ImageControl", "CustomControl",
                    "GroupControl", "ListControl", "TabControl", "TitleBarControl"}


def _is_chrome(i, win_rect=None, client_top=None):
    """这条控件是不是窗口装饰（标题栏/最小化/最大化/关闭）：
    ①按 automationId ②按类型 ③按名字（UWP 的按钮名带应用名，用正则剥）
    ④没名字的按钮再看几何：整块落在客户区上方（标题栏里）。"""
    aid = i.get("aid") or ""
    if aid in _DECO_AIDS:
        return True
    if i.get("type") == "TitleBarControl":
        return True
    nm = (i.get("name") or "").strip()
    if nm in _DECO_NAMES:
        return True
    if nm and _DECO_RE.match(nm) and i.get("type") in ("ButtonControl", "WindowControl"):
        return True
    if (not nm) and client_top and i.get("type") in ("ButtonControl", "WindowControl"):
        try:
            r = i.get("rect") or []
            if len(r) == 4 and r[3] <= client_top + 2:
                return True
        except Exception:
            pass
    return False


def _readability(items, window_class="", win_rect=None, client_top=None):
    """判断这个窗口能不能靠控件操作——把"看一眼就知道走不走 UIA"的经验写进代码，
    免得每次都靠人判断。返回 (full/partial/none, 下一步建议, window_ready)。
    判据：①只剩窗口装饰+空容器=内容区没渲染（UWP 卡启动画面最典型）；②Chromium/Electron
    没有 DocumentControl=网页内容读不到；③容器类控件占满=自绘界面。"""
    total = len(items)
    if total == 0:
        return ("none",
                "读不到任何控件。多半是自绘/Electron/Qt 窗口：改用 screen_shot 截图 + screen_find 模板找元素 + screen_click x/y 坐标点",
                False)
    named = sum(1 for i in items if i.get("name"))
    core = [i for i in items if not _is_chrome(i, win_rect, client_top)]
    rendered = [i for i in core if (i.get("type") or "") not in _CONTAINER_TYPES]
    if not rendered:
        # 2026-11-12 计算器那次翻车就栽在这：窗口卡在启动画面（蓝底+图标），UIA 只出
        # 标题栏 3 个按钮 + 一个空 Pane，旧逻辑数出 4 个"有名字"控件就报 full，
        # 结果是拿坐标瞎点，点中了 % 而不是 CE。这种情况必须明说"内容没渲染"。
        return ("none",
                "只读到窗口外壳（标题栏按钮 + 空容器），内容区一个控件都没有：应用多半卡在启动画面/内容没渲染出来（UWP 最常见），或这本来就是纯展示窗口。"
                "别按坐标瞎点——先 screen_wait 等目标控件名出现；还不行就关掉重开这个应用，再 screen_map 一次。",
                False)
    if named < 3:
        # 小对话框（只有「确定」「取消」两个按钮）是能操作的，别因为控件少就当它不可用
        return ("partial",
                "只有 %d 个控件、%d 个有名字（小对话框很常见）。按名字点有把握；名字不全的用 screen_shot 确认布局后再点" % (total, named),
                True)
    if (window_class or "").startswith("Chrome_WidgetWin") and not any(i["type"] == "DocumentControl" for i in items):
        return ("partial",
                "Chromium/Electron 窗口：只读到浏览器框架（标签、工具栏），网页内容区没暴露。网页元素用浏览器控制（CDP/JS），桌面部分按控件或坐标",
                True)
    if named < total * 0.5:
        return ("partial",
                "%d 个控件里只有 %d 个有名字。优先挑有名字的控件操作，剩下的用坐标；先 screen_shot 确认布局" % (total, named),
                True)
    if total > 200:
        return ("full",
                "%d 个控件可读（列表类窗口）。用 name/aid 精确指定目标，别用类型取第一个" % total,
                True)
    return ("full", "%d 个控件、%d 个有名字，可以放心按控件操作" % (total, named), True)


def _is_fg(w):
    """按 hwnd 判断前台，不能按 pid：UWP 的「设置」「计算器」同属 ApplicationFrameHost.exe，
    pid 一样，会把后台那个也当成前台（导致该激活时不激活、fg_ok 假阳）。"""
    try:
        fg = u.GetForegroundWindow()
        return int(fg) == int(w.NativeWindowHandle)
    except Exception:
        return False


def _force_foreground(hwnd):
    """把窗口真正顶到前台：Alt 抖动 + AttachThreadInput（绕 Windows 前台锁）"""
    try:
        fg = u.GetForegroundWindow()
        tid_fg = u.GetWindowThreadProcessId(fg, None)
        tid_me = u.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(u.AttachThreadInput(tid_me, tid_fg, True))
        u.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        if attached:
            u.AttachThreadInput(tid_me, tid_fg, False)
        return u.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _activate(w):
    """把窗口顶到前台：UWP/后台窗口都试一遍（SetActive 对 UWP 无效）"""
    try:
        if _is_fg(w):
            return
        hwnd = w.NativeWindowHandle
        if not hwnd:
            w.SetActive()
            time.sleep(0.25)
            return
        # 最小化的先还原
        try:
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.3)
        except Exception:
            pass
        _force_foreground(hwnd)
        if not _is_fg(w):
            # Alt 抖一下再试（给本进程抢前台的权限）
            u.keybd_event(0x12, 0, 0, 0)
            u.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.1)
            _force_foreground(hwnd)
        if not _is_fg(w):
            w.SetActive()
            time.sleep(0.25)
    except Exception:
        pass


_CLS_BY_TYPE = {
    "EditControl": auto.EditControl,
    "ButtonControl": auto.ButtonControl,
    "MenuItemControl": auto.MenuItemControl,
    "MenuControl": auto.MenuControl,
    "ComboBoxControl": auto.ComboBoxControl,
    "CheckBoxControl": auto.CheckBoxControl,
    "RadioButtonControl": auto.RadioButtonControl,
    "ListItemControl": auto.ListItemControl,
    "TreeItemControl": auto.TreeItemControl,
    "TabItemControl": auto.TabItemControl,
    "TextControl": auto.TextControl,
    "DocumentControl": auto.DocumentControl,
    "HyperlinkControl": auto.HyperlinkControl,
    "PaneControl": auto.PaneControl,
    "ToolBarControl": auto.ToolBarControl,
    "StatusBarControl": auto.StatusBarControl,
}


def _rewrap(c, w, depth):
    """uiautomation 用通用 Control 搜出来的对象没有 GetValuePattern 等方法；
    按它的真实 ControlType 重新构造一次，拿到带模式的子类。"""
    if c is None:
        return None
    cls = _CLS_BY_TYPE.get(c.ControlTypeName)
    if cls is None or isinstance(c, cls):
        return c
    kw = {"searchFromControl": w, "searchDepth": depth}
    try:
        aid = c.AutomationId
        nm = c.Name
    except Exception:
        return c
    if aid:
        kw["AutomationId"] = aid
    if nm:
        kw["Name"] = nm
    if len(kw) <= 2:
        return c
    try:
        c2 = cls(**kw)
        if c2.Exists(1):
            return c2
    except Exception:
        pass
    return c


_KEY_SUFFIX = re.compile(r"\s*[（(]&?[A-Za-z][)）]\s*$")


def _name_key(s):
    """名字归一化：去掉助记键后缀「编辑(E)」「Save(&S)」，再把大小写抹平。
    有了它，按「编辑」能精确对上记事本的「编辑(E)」——不用退到包含匹配去撞「文本编辑器」。"""
    s = (s or "").strip()
    s = _KEY_SUFFIX.sub("", s)
    return s.casefold()


def _type_key(t):
    """'edit' / 'EditControl' 都归一成 'edit'"""
    t = (t or "").strip().lower()
    if t.endswith("control"):
        t = t[:-7]
    return t


def _type_matches(ch, want_type):
    if not want_type:
        return True
    try:
        return _type_key(ch.ControlTypeName) == _type_key(want_type)
    except Exception:
        return False


def resolve2(w, target, depth=12):
    """把 target 解析成 (控件, 匹配信息)。支持 {name}/{aid}/{type}/{i}，每次重新查。
    名字匹配顺序：精确（含去助记键）> 前缀 > 包含；「包含」是弱匹配，调用方要当心
    ——2026-11-13 回归里「编辑」被「文本编辑器」包含匹配抢走，就是没有这层区分。"""
    meta = {"mode": "none", "candidates": 0, "matched_name": None}
    with _LOCK:
        if target.get("aid"):
            try:
                c = auto.Control(searchFromControl=w, searchDepth=depth,
                                 AutomationId=target["aid"])
                if c.Exists(1):
                    meta.update(mode="aid", matched_name=(c.Name or "")[:60])
                    return _rewrap(c, w, depth), meta
            except Exception:
                pass
        if target.get("name"):
            want = _name_key(target["name"])
            cls = TYPE_NAMES.get((target.get("type") or "").lower(), auto.Control)
            try:
                c = cls(searchFromControl=w, searchDepth=depth, Name=target["name"])
                if c.Exists(1) and _name_key(c.Name) == want and _type_matches(c, target.get("type")):
                    meta.update(mode="exact", candidates=1, matched_name=(c.Name or "")[:60])
                    return _rewrap(c, w, depth), meta
            except Exception:
                pass

            buckets = {"exact": [], "prefix": [], "contains": []}

            def walk(cur, d):
                if d > depth:
                    return
                try:
                    kids = cur.GetChildren()
                except Exception:
                    return
                for ch in kids:
                    try:
                        cn = ch.Name or ""
                        k = _name_key(cn)
                        if k and _type_matches(ch, target.get("type")):
                            if k == want:
                                buckets["exact"].append(ch)
                            elif k.startswith(want):
                                buckets["prefix"].append(ch)
                            elif want in k:
                                buckets["contains"].append(ch)
                    except Exception:
                        pass
                    walk(ch, d + 1)

            walk(w, 0)
            for mode in ("exact", "prefix", "contains"):
                got = buckets[mode]
                if got:
                    # 包含匹配可能有多个候选，挑名字最短的（越短越像本体）
                    if mode == "contains":
                        got = sorted(got, key=lambda x: len(x.Name or ""))
                    meta.update(mode=mode, candidates=len(got),
                                matched_name=(got[0].Name or "")[:60])
                    return _rewrap(got[0], w, depth), meta
            # 窗口树里没有 → 试当前弹出菜单（右键菜单是独立 #32768 窗口）
            for pw in _popup_menus():
                try:
                    pc = auto.ControlFromHandle(pw["hwnd"])
                    if pc is None:
                        continue
                    for ch in pc.GetChildren():
                        if _name_key(ch.Name) == want:
                            meta.update(mode="menu", candidates=1,
                                        matched_name=(ch.Name or "")[:60])
                            return _rewrap(ch, pc, 4), meta
                except Exception:
                    pass
            meta["mode"] = "none"
            return None, meta
        if target.get("type"):
            ct = (target["type"] or "").strip().lower()
            if ct not in TYPE_NAMES and ct.endswith("control"):
                ct = ct[:-7]
            cls = TYPE_NAMES.get(ct)
            if cls:
                c = cls(searchFromControl=w, searchDepth=depth)
                if c.Exists(1):
                    meta.update(mode="type", candidates=1, matched_name=(c.Name or "")[:60])
                    return _rewrap(c, w, depth), meta
        return None, meta


def resolve(w, target, depth=12):
    """兼容旧调用：只要控件"""
    return resolve2(w, target, depth)[0]


def _snap_region(x, y, half=120):
    """截取以点为中心的小区域（点后验证用）"""
    from PIL import ImageGrab
    try:
        vx1, vy1, vx2, vy2 = _virtual_rect()
        box = (max(vx1, x - half), max(vy1, y - half), min(vx2, x + half), min(vy2, y + half))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        return ImageGrab.grab(bbox=box, all_screens=True)
    except Exception:
        return None


def _snap_verify(x, y, box=None, half=120):
    """点后验证用的截图：给了窗口矩形就截整窗（变化常发生在点击点之外，如显示区），否则截点周围"""
    from PIL import ImageGrab
    try:
        if box:
            vx1, vy1, vx2, vy2 = _virtual_rect()
            b = (max(vx1, box[0]), max(vy1, box[1]), min(vx2, box[2]), min(vy2, box[3]))
            if b[2] > b[0] and b[3] > b[1]:
                return ImageGrab.grab(bbox=b, all_screens=True)
        return _snap_region(x, y, half)
    except Exception:
        return None


def _diff_stats(a, b):
    """两张图差异：(比例, 变化像素数)；任一为空或尺寸不同返回 (None, None)"""
    if a is None or b is None:
        return None, None
    try:
        import numpy as np
        x = np.asarray(a.convert("RGB"), dtype=np.int16)
        y = np.asarray(b.convert("RGB"), dtype=np.int16)
        if x.shape != y.shape:
            return None, None
        diff = np.any(np.abs(x - y) > 8, axis=2)
        n = int(np.count_nonzero(diff))
        return round(n / float(diff.size), 4), n
    except Exception:
        return None, None


def _safe_desc(c, r=None):
    """动作后取控件信息：控件可能已经被点没了（点"确定"关窗、点"删除"关对话框）。
    直接读 c.Name 会抛 ElementNotAvailableError，把已经成功的动作变成 500，
    模型看到 500 就会重试——"确定"于是点两遍。这里读不到就标 gone，不再抛。"""
    try:
        name = c.Name
    except Exception:
        name = None
    try:
        ctype = c.ControlTypeName
    except Exception:
        ctype = None
    try:
        rect = _rect(r if r is not None else c.BoundingRectangle)
    except Exception:
        rect = None
    out = {"name": name, "type": ctype, "rect": rect}
    if name is None and ctype is None:
        out["gone"] = True
    return out


def _verify_fields(out, before, after, note="截图没比对上（窗口被挡/尺寸变化/截图失败），这次动作点没点进去不确定"):
    """把"前后截图差异"写进结果：verified 三态——
    True=画面确实变了，False=一点没变（白点了），None=没法比对（≠没反应）。"""
    ch, npx = _diff_stats(before, after)
    out["changed"] = ch
    out["changed_px"] = npx
    if npx is None:
        out["verified"] = None
        out["warning"] = (out["warning"] + "；" + note) if out.get("warning") else note
    else:
        out["verified"] = npx > 300
    return out


def _send_click(x, y, button="left", double=False):
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    if button == "right":
        down, up = 0x0008, 0x0010
    elif button == "middle":
        down, up = 0x0020, 0x0040
    else:
        down, up = 0x0002, 0x0004
    u.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.04)
    u.mouse_event(up, 0, 0, 0, 0)
    if double:
        time.sleep(0.08)
        u.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.04)
        u.mouse_event(up, 0, 0, 0, 0)


def click(title, target, via="auto", activate=True, wait=0.35, x=None, y=None,
          button="left", double=False, verify=True, template=None, threshold=0.85):
    """点控件：auto=先 Invoke 再鼠标；mouse=只用鼠标；点后校验前台与矩形。

    自绘/网页元素读不到控件时，可给 x/y 直接点物理坐标（配合 screen_shot 的 origin 换算）。
    button=left/right/middle（默认 left）；double=True 双击。
    verify 默认开：点前后截窗口对比，返回 changed/changed_px/verified——
    changed_px=0 就是白点了，不用自己再截图确认（不想要就传 verify=False）。
    """
    t0 = time.time()
    bad = _need_window(title, target)
    if bad:
        return bad
    if (x is None) != (y is None):
        return {"ok": False, "error": "BAD_POINT", "x": x, "y": y,
                "hint": "x 和 y 必须成对给（物理屏幕坐标）；只给一个没法定位，也容易被当成「控件点击」处理"}
    wait = _num(wait, 0.35, 0.0, 10.0)
    w = find_window(title) if title else None
    if title and w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title,
                "hint": "窗口没找到：①标题可能不准，先调 screen_windows 看真实标题 ②窗口可能根本没打开"}

    # 给了模板就先按图找位置（不用人眼估坐标），找不到宁可报错也不瞎点
    if template:
        fr = find_template(template, threshold=threshold, title=title)
        if not fr.get("ok"):
            return {"ok": False, "error": fr.get("error", "TEMPLATE_NOT_FOUND"),
                    "template": template, "score": fr.get("score"), "threshold": threshold,
                    "hint": "模板没匹配上：①模板要从当前界面截的（主题/缩放变了要重截）②调低 threshold ③窗口被挡时传 window，会按窗口自身像素找"}
        x, y = fr["point"]

    # 显式坐标优先：同时给了 target 和 x/y 就按坐标点（和 hover/scroll/drag 一个口径），
    # 但要在结果里说清楚，免得以为点的是那个控件。
    if x is not None and y is not None:
        px, py = int(x), int(y)
        vbox = None
        if verify and w is not None:
            try:
                rr = w.BoundingRectangle
                vbox = (rr.left, rr.top, rr.right, rr.bottom)
            except Exception:
                vbox = None
        fg_before = _is_fg(w) if w is not None else None
        before = _snap_verify(px, py, vbox) if verify else None
        _send_click(px, py, button, double)
        time.sleep(wait)
        after = _snap_verify(px, py, vbox) if verify else None
        out = {
            "ok": True,
            "via": "point",
            "point": [px, py],
            "button": button,
            "double": double,
            "fg_before": fg_before,
            "fg_ok": _is_fg(w) if w is not None else None,
            "elapsed": round(time.time() - t0, 3),
        }
        if verify:
            _verify_fields(out, before, after)
            if out.get("verified") is False:
                out["hint"] = ("点完画面像素没变化（changed_px=%s）：可能点空了、点在了不可点区域，"
                               "也可能这个控件本来就不改画面（比如只是聚焦）" % out.get("changed_px"))
        warns = []
        if target:
            warns.append("同时给了 target 和 x/y：按坐标点 (%d,%d)，没按控件中心" % (px, py))
        # 坐标点击不抢前台，但窗口不在前台时这一下可能落到别的窗口上——必须说出来
        if w is not None and not fg_before:
            warns.append("点之前目标窗口不在前台，这一下可能落到了别的窗口上；确认前先 screen_shot 看一眼")
        if warns:
            out["warning"] = "；".join(([out["warning"]] if out.get("warning") else []) + warns)
        return out

    if activate and w is not None:
        _activate(w)
    c, meta = resolve2(w, target)
    if c is None:
        return {"ok": False, "error": "TARGET_NOT_FOUND", "target": target,
                "hint": "控件没找到：①先 screen_map 看真实名字（名字可能变了）②可能被弹窗挡着 ③自绘窗口读不到控件，改用 screen_shot + screen_find 模板匹配"}

    r = c.BoundingRectangle
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    t0 = time.time()
    used = "none"
    err = ""
    vbox = None
    if verify and w is not None:
        try:
            rr = w.BoundingRectangle
            vbox = (rr.left, rr.top, rr.right, rr.bottom)
        except Exception:
            vbox = None
    before = _snap_verify(cx, cy, vbox) if verify else None

    # 右键/中键/双击一律走鼠标（没有对应 UIA 模式）
    if button == "left" and not double and via in ("auto", "invoke"):
        try:
            ip = c.GetInvokePattern()
            if ip is not None:
                ip.Invoke()
                used = "invoke"
        except Exception as e:
            err = repr(e)

    if used == "none" and via in ("auto", "mouse"):
        if r.right - r.left <= 0 or r.bottom - r.top <= 0:
            return {"ok": False, "error": "EMPTY_RECT", "target": target,
                    "hint": "控件矩形为空（隐藏或被遮挡）：先截图看，滚动/展开/关掉遮它的弹窗再点"}
        _send_click(cx, cy, button, double)
        used = "mouse"
    elif used == "none" and via == "invoke":
        return {"ok": False, "error": "NO_INVOKE_PATTERN", "target": target,
                "hint": "这个控件没有 Invoke 模式：改 via=auto 或 via=mouse 用鼠标点"}

    time.sleep(wait)

    # 校验
    after = _snap_verify(cx, cy, vbox) if verify else None
    fg_ok = _is_fg(w)
    try:
        r2 = c.BoundingRectangle
        moved = (int(r2.left), int(r2.top)) != (int(r.left), int(r.top))
    except Exception:
        r2, moved = r, False

    out = {
        "ok": used != "none",
        "via": used,
        "point": [cx, cy],
        "target": _safe_desc(c, r),
        "match": meta.get("mode"),
        "post_rect": _rect(r2),
        "moved": moved,
        "fg_ok": fg_ok,
        "elapsed": round(time.time() - t0, 3),
        "err": err,
    }
    # 弱匹配点中了别的控件，是事故级的坑（2026-11-13「编辑」误中「文本编辑器」）：
    # 不拦，但必须明说，让人一眼看见点的是谁
    if meta.get("mode") in ("prefix", "contains") and meta.get("candidates", 0) > 1:
        out["warning"] = ("名字是模糊匹配、候选 %d 个，点的是「%s」。要稳就传精确 name 或 aid"
                          % (meta["candidates"], (out["target"] or {}).get("name")))
    if verify:
        _verify_fields(out, before, after, "截图没比对上（窗口被挡/尺寸变化），点没点进去不确定")
    return out


def get_value(title, target):
    """读控件当前值：ValuePattern 优先，没有就退 TextPattern（TextControl 没有 ValuePattern）。
    两者都拿不到才算失败——别让"读不到值"把"能读到文字"的控件判死。"""
    bad = _need_window(title, target)
    if bad:
        return bad
    w = find_window(title)
    if w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}
    c = resolve(w, target)
    if c is None:
        return {"ok": False, "error": "TARGET_NOT_FOUND"}
    val = None
    err = ""
    try:
        if hasattr(c, "GetValuePattern"):
            vp = c.GetValuePattern()
            val = vp.Value if vp is not None else None
    except Exception as e:
        err = repr(e)
    txt = None
    try:
        tp = c.GetTextPattern()
        if tp is not None:
            txt = tp.DocumentRange.GetText(-1)
    except Exception:
        pass
    if val is None and not txt:
        # 兜底：TextControl 的 Name 通常就是显示文本（如计算器 "显示为 7"）
        d = _safe_desc(c)
        nm = (d.get("name") or "").strip()
        if nm:
            return {"ok": True, "value": nm, "text": nm, "via": "name",
                    "name": nm, "type": d.get("type")}
        return {"ok": False, "error": "NO_VALUE_PATTERN", "detail": err,
                "hint": "该控件既没有 ValuePattern/TextPattern，Name 也是空的；用 screen_map 看名字/类型，或截图确认"}
    d = _safe_desc(c)
    return {"ok": True, "value": val, "text": (txt or "")[:2000],
            "name": d.get("name"), "type": d.get("type")}


def set_value(title, target, value, activate=True, wait=0.3, fallback_keys=True):
    """直写：ValuePattern 优先，失败可退到聚焦+键盘；写完回读"""
    bad = _need_window(title, target)
    if bad:
        return bad
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    wait = _num(wait, 0.3, 0.0, 10.0)
    w = find_window(title)
    if w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}
    if activate:
        _activate(w)
    c = resolve(w, target)
    if c is None:
        return {"ok": False, "error": "TARGET_NOT_FOUND"}
    t0 = time.time()
    used = "valuepattern"
    try:
        vp = c.GetValuePattern()
        if vp is None:
            raise RuntimeError("no value pattern")
        vp.SetValue(value)
    except Exception as e:
        if not fallback_keys:
            return {"ok": False, "error": "SET_FAILED", "detail": repr(e)}
        # 退到键盘：走自己的 SendInput（unicode），不用 auto.SendKeys——
        # 后者把 { } + ^ % ~ ( ) 当特殊键解析，写含这些字符的文本会被吃掉或变异；
        # 中文也只有 unicode 注入这条稳。
        if not _input_desktop_ok():
            return dict(INPUT_DESKTOP_UNAVAILABLE)
        used = "keys"
        try:
            c.SetFocus()
            time.sleep(0.1)
            press_key("ctrl+a")
            press_key("delete")
            type_text(value)
        except Exception as e2:
            return {"ok": False, "error": "SET_FAILED", "detail": repr(e2)}
    time.sleep(wait)
    cur = _read_back(c)
    return {
        "ok": cur == value,
        "via": used,
        "value": cur,
        "target": _safe_desc(c),
        "elapsed": round(time.time() - t0, 3),
    }


def _read_back(c):
    """回读控件内容：ValuePattern → TextPattern → Name（键盘输入的控件常没有 ValuePattern）"""
    try:
        vp = c.GetValuePattern()
        if vp is not None:
            v = vp.Value
            if v is not None:
                return v
    except Exception:
        pass
    try:
        tp = c.GetTextPattern()
        if tp is not None:
            t = tp.DocumentRange.GetText(-1)
            if t:
                return t
    except Exception:
        pass
    try:
        return c.Name or None
    except Exception:
        return None


def wait_for(title, target=None, timeout=10.0, poll=0.3):
    """等窗口出现 / 等控件出现；返回等到的时间点和控件信息"""
    if not (title or "").strip():
        return {"ok": False, "error": "MISSING_WINDOW", "elapsed": 0,
                "hint": "window 不能为空：要等哪个窗口就把标题关键词传进来"}
    # 单线程服务：一次 /wait 最长 55s（Node 侧 60s 超时），再长就会把整个服务堵死
    timeout = _num(timeout, 10.0, 0.05, 55.0)
    poll = _num(poll, 0.3, 0.05, 2.0)
    t0 = time.time()
    while True:
        w = find_window(title, timeout=0.3)
        if w is not None:
            if target is None:
                return {"ok": True, "elapsed": round(time.time() - t0, 3), "window": {"title": w.Name, "pid": w.ProcessId}}
            c = resolve(w, target)
            if c is not None:
                r = c.BoundingRectangle
                return {"ok": True, "elapsed": round(time.time() - t0, 3),
                        "control": {"name": c.Name, "type": c.ControlTypeName, "rect": _rect(r), "enabled": bool(c.IsEnabled)}}
        if time.time() - t0 >= timeout:
            return {"ok": False, "error": "TIMEOUT", "elapsed": round(time.time() - t0, 3)}
        time.sleep(poll)


def _virtual_rect():
    """虚拟桌面范围（多屏时副屏可能是负坐标）"""
    x = u.GetSystemMetrics(76)
    y = u.GetSystemMetrics(77)
    w = u.GetSystemMetrics(78)
    h = u.GetSystemMetrics(79)
    return (x, y, x + w, y + h)


def _draw_grid(img, box, step=100):
    """在截图上画坐标网格：细线每 100px、红线每 500px，边缘标屏幕物理坐标。
    看图直接报坐标，不用在缩放图上估。"""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    ox, oy = (box[0], box[1]) if box else (0, 0)
    w, h = img.size
    try:
        step = max(40, int(step))
    except Exception:
        step = 100
    for x in range(0, w, step):
        sx = ox + x
        major = (sx % 500 == 0)
        d.line([(x, 0), (x, h)], fill=(255, 60, 60) if major else (150, 150, 150), width=1)
        if major:
            d.text((x + 2, 1), str(sx), fill=(255, 0, 0))
    for y in range(0, h, step):
        sy = oy + y
        major = (sy % 500 == 0)
        d.line([(0, y), (w, y)], fill=(255, 60, 60) if major else (150, 150, 150), width=1)
        if major:
            d.text((1, y + 1), str(sy), fill=(255, 0, 0))


def _grab_window_print(hwnd):
    """用 PrintWindow 抓窗口自身像素（被别的窗口盖住也能抓）。

    失败或全黑返回 None——Electron/GPU 渲染的窗口常给黑图，调用方要退回屏幕截图。
    """
    try:
        from ctypes import wintypes
        from PIL import Image
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        r = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        hdc = u32.GetWindowDC(hwnd)
        if not hdc:
            return None
        mdc = bmp = old = None
        img = None
        try:
            mdc = g32.CreateCompatibleDC(hdc)
            bmp = g32.CreateCompatibleBitmap(hdc, w, h)
            old = g32.SelectObject(mdc, bmp)
            ok = u32.PrintWindow(hwnd, mdc, 2)  # 2 = PW_RENDERFULLCONTENT
            if ok:
                class _BIH(ctypes.Structure):
                    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                                ("biClrImportant", wintypes.DWORD)]
                bi = _BIH()
                bi.biSize = ctypes.sizeof(_BIH)
                bi.biWidth = w
                bi.biHeight = -h  # top-down
                bi.biPlanes = 1
                bi.biBitCount = 32
                bi.biCompression = 0
                buf = ctypes.create_string_buffer(w * h * 4)
                g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
                img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        finally:
            # GDI 句柄必须无条件归还：中途抛异常也要走这里，
            # 否则每次抓 Electron/GPU 窗口失败就漏 DC + 位图（2026-11-13 修）
            try:
                if mdc and old:
                    g32.SelectObject(mdc, old)
                if bmp:
                    g32.DeleteObject(bmp)
                if mdc:
                    g32.DeleteDC(mdc)
            finally:
                u32.ReleaseDC(hwnd, hdc)
        if img is None:
            return None
        # 全黑/单色基本等于没抓到（Electron 常见），当失败处理
        try:
            ext = img.convert("L").getextrema()
            if ext and ext[0] == ext[1]:
                return None
        except Exception:
            pass
        return img
    except Exception:
        return None


def shot(title=None, out_path=None, annotate=False, max_items=60, region=None, grid=None):
    """1:1 物理像素截图。

    返回值带 origin/rect：图内坐标 + origin = 屏幕物理坐标，调用方按这个换算，
    不需要知道 DPI 和缩放（换机器、多屏、高 DPI 都成立）。
    """
    bad = _desktop_ok_error()
    if bad:
        bad.pop("detail", None)
        bad["path"] = None
        return bad
    from PIL import ImageGrab, ImageDraw

    if out_path is None:
        out_path = os.path.join(os.environ.get("TEMP", "."), "gui_shot_%d.png" % int(time.time() * 1000))

    vx1, vy1, vx2, vy2 = _virtual_rect()
    box = None
    items = []
    img = None
    src = "screen"
    if region is not None:
        try:
            nums = [int(v) for v in region]
        except Exception:
            return {"ok": False, "error": "BAD_REGION", "region": region,
                    "hint": "region 要 4 个整数 [x1,y1,x2,y2]（物理像素）"}
        if len(nums) != 4:
            return {"ok": False, "error": "BAD_REGION", "region": region,
                    "hint": "region 要 4 个整数 [x1,y1,x2,y2]（物理像素），收到 %d 个" % len(nums)}
        if nums[2] <= nums[0] or nums[3] <= nums[1]:
            return {"ok": False, "error": "BAD_REGION", "region": region,
                    "hint": "region 要 x1<x2 且 y1<y2（左上角 → 右下角），顺序反了截不到"}
        box = tuple(nums)
    elif title:
        w = find_window(title)
        if w is None:
            return {"ok": False, "error": "WINDOW_NOT_FOUND"}
        r = w.BoundingRectangle
        box = (r.left, r.top, r.right, r.bottom)
        if annotate:
            m = map_window(title, depth=6, include_unnamed=False, max_items=max_items)
            items = m.get("items", []) if m.get("ok") else []
        # 先抓窗口自身像素：窗口被别的窗口盖住时，屏幕截图只能拿到上面那层
        try:
            img = _grab_window_print(w.NativeWindowHandle)
        except Exception:
            img = None
        if img is not None:
            src = "printwindow"
            box = (r.left, r.top, r.left + img.width, r.top + img.height)
    if box is not None and img is None:
        box = (max(vx1, box[0]), max(vy1, box[1]), min(vx2, box[2]), min(vy2, box[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            return {"ok": False, "error": "EMPTY_REGION", "rect": list(box)}
    if img is None:
        try:
            img = ImageGrab.grab(bbox=box, all_screens=True) if box else ImageGrab.grab(all_screens=True)
        except TypeError:  # 老版 Pillow 没有 all_screens
            img = ImageGrab.grab(bbox=box) if box else ImageGrab.grab()
    if annotate and items:
        d = ImageDraw.Draw(img)
        ox, oy = (box[0], box[1]) if box else (0, 0)
        for it in items:
            x1, y1, x2, y2 = it["rect"]
            x1, y1, x2, y2 = x1 - ox, y1 - oy, x2 - ox, y2 - oy
            if x2 <= 0 or y2 <= 0 or x1 >= img.width or y1 >= img.height:
                continue
            d.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=2)
            label = str(it["i"])
            tw = 8 + 6 * len(label)
            d.rectangle([x1, max(0, y1 - 16), x1 + tw, max(0, y1 - 16) + 14], fill=(255, 255, 0))
            d.text((x1 + 3, max(0, y1 - 15)), label, fill=(0, 0, 0))
    if grid:
        _draw_grid(img, box, grid if grid is not True else 100)
    try:
        d = os.path.dirname(os.path.abspath(out_path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    img.save(out_path)
    origin = [box[0], box[1]] if box else [vx1, vy1]
    return {
        "ok": True,
        "path": out_path,
        "source": src,
        "size": [img.width, img.height],
        "origin": origin,
        "rect": [origin[0], origin[1], origin[0] + img.width, origin[1] + img.height],
        "note": "图内坐标 + origin = 屏幕物理坐标（图是物理像素 1:1 裁的）",
        "annotated": bool(annotate and items),
        "items": len(items),
    }


def find_template(template_path, threshold=0.85, region=None, title=None):
    """模板匹配：在屏幕上找一张小图，直接返回物理坐标。
    给"读不到控件、但长相固定"的元素用（按钮、图标、菜单项）——比人眼在截图里估坐标准。
    给了 title 就只在这一窗的像素里找（PrintWindow，窗口被别的窗口挡住也能找到）。"""
    threshold = _num(threshold, 0.85, 0.0, 1.0)
    if not (template_path or "").strip():
        return {"ok": False, "error": "MISSING_TEMPLATE",
                "hint": "template 要给模板图路径（PNG）；没有模板就先 screen_shot region=… 截一块再传"}
    try:
        import cv2
        import numpy as np
        from PIL import ImageGrab
    except Exception as e:
        return {"ok": False, "error": "NO_CV2", "detail": repr(e)}
    # cv2.imread 不支持中文/非 ASCII 路径，走 imdecode
    try:
        with open(template_path, "rb") as fh:
            tpl = cv2.imdecode(np.frombuffer(fh.read(), dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        tpl = None
    if tpl is None:
        return {"ok": False, "error": "TEMPLATE_UNREADABLE", "path": template_path}
    vx1, vy1, vx2, vy2 = _virtual_rect()
    src = "screen"
    img = None
    win = find_window(title, timeout=1.0) if title else None
    if win is not None:
        img = _grab_window_print(win.NativeWindowHandle)
    box = None
    if region is not None:
        try:
            nums = [int(v) for v in region]
        except Exception:
            nums = []
        if len(nums) != 4:
            return {"ok": False, "error": "BAD_REGION", "region": region,
                    "hint": "region 要 4 个整数 [x1,y1,x2,y2]（物理像素）"}
        box = (max(vx1, nums[0]), max(vy1, nums[1]), min(vx2, nums[2]), min(vy2, nums[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            return {"ok": False, "error": "EMPTY_REGION", "region": region}
    if img is not None:
        # 窗口自身像素：原点用窗口左上角；同时给了 region 就裁到两者交集
        try:
            rr = win.BoundingRectangle
            wx, wy = int(rr.left), int(rr.top)
            src = "printwindow"
            if box:
                x1 = max(0, box[0] - wx)
                y1 = max(0, box[1] - wy)
                x2 = min(img.width, box[2] - wx)
                y2 = min(img.height, box[3] - wy)
                if x2 - x1 < tpl.shape[1] or y2 - y1 < tpl.shape[0]:
                    img = None            # 交集太小，退回屏幕截图
                else:
                    img = img.crop((x1, y1, x2, y2))
                    box = (wx + x1, wy + y1, wx + x2, wy + y2)
            else:
                box = (wx, wy, wx + img.width, wy + img.height)
        except Exception:
            img = None
    if img is None:
        src = "screen"
        if win is not None:
            # PrintWindow 抓不到（UWP/GPU 渲染常见）：把窗口顶到前台，再按窗口矩形抓屏，
            # 否则窗口被别的窗口挡着时，等于在别人家窗玻璃上找按钮。
            # 调用方明确给了 region 就只顶前台、不覆盖 region（2026-11-13 修）。
            try:
                _activate(win)
                time.sleep(0.25)
                if box is None:
                    rr = win.BoundingRectangle
                    box = (int(rr.left), int(rr.top), int(rr.right), int(rr.bottom))
            except Exception:
                pass
        try:
            grabbed = ImageGrab.grab(bbox=box, all_screens=True) if box else ImageGrab.grab(all_screens=True)
        except TypeError:
            grabbed = ImageGrab.grab(bbox=box) if box else ImageGrab.grab()
        img = grabbed
    screen = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    try:
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
    except Exception as e:
        return {"ok": False, "error": "MATCH_FAILED", "detail": repr(e)}
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    ox, oy = (box[0], box[1]) if box else (0, 0)
    th, tw = tpl.shape[:2]
    score = round(float(maxv), 3)
    if maxv < threshold:
        return {"ok": False, "error": "NO_MATCH", "score": score, "threshold": threshold,
                "source": src}
    # 平面对平面会骗人：纯色/均匀区域在 TM_CCOEFF_NORMED 下也能拿高分（方差趋零时归一化失效），
    # 2026-11-12 就出现过"模板 match 到一片空白蓝底"。这里比一下匹配块的纹理强度，
    # 明显比模板平就直接判假（除非模板本身就是纯色块，那种没得比）。
    try:
        patch = screen[maxloc[1]:maxloc[1] + th, maxloc[0]:maxloc[0] + tw]
        t_std = float(tpl.astype("float32").std())
        p_std = float(patch.astype("float32").std()) if patch.size else 0.0
        if t_std >= 4.0 and p_std < t_std * 0.5:
            return {"ok": False, "error": "FLAT_PATCH", "score": score,
                    "threshold": threshold, "source": src,
                    "detail": "匹配块的纹理强度 %.1f 远低于模板 %.1f，八成是落在纯色/空白区域" % (p_std, t_std)}
    except Exception:
        pass
    return {
        "ok": True,
        "score": score,
        "source": src,
        "point": [ox + maxloc[0] + tw // 2, oy + maxloc[1] + th // 2],
        "rect": [ox + maxloc[0], oy + maxloc[1], ox + maxloc[0] + tw, oy + maxloc[1] + th],
        "template_size": [tw, th],
    }


# ══════════════════════════════════════════════════════════════════
# 键盘输入（整合 uitype.py / uikey.py / vktype.py 三条路子）
#   mode="unicode"：KEYEVENTF_UNICODE 逐字，中文直接打、不依赖输入法（默认）
#   mode="vk"     ：VK 路径逐字符，给 VMware 这类不认 UNICODE 的环境
#   组合键        ：ctrl+a / ctrl+shift+end / enter / f5 …
# ══════════════════════════════════════════════════════════════════
from ctypes import wintypes

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ULONG_PTR = ctypes.c_ulonglong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _input_desktop_ok():
    """当前会话是否在输入桌面：锁屏 / 切到登录桌面时 SendInput 必被拒（err=5）"""
    try:
        h = u.OpenInputDesktop(0, 0, 0x0001)
        if not h:
            return False
        u.CloseDesktop(h)
        return True
    except Exception:
        return False


INPUT_DESKTOP_UNAVAILABLE = {
    "ok": False,
    "error": "INPUT_DESKTOP_UNAVAILABLE",
    "detail": "当前不在输入桌面（屏幕已锁定或已切到登录桌面），SendInput 会被拒绝，键盘操作发不出去。解锁后重试。",
}

DESKTOP_UNAVAILABLE_SHOT = {
    "ok": False,
    "error": "INPUT_DESKTOP_UNAVAILABLE",
    "detail": "当前不在活动桌面（屏幕已锁定或切到登录桌面）：截不到画面，键盘鼠标也发不出去。解锁后重试。",
    "path": None,
    "origin": None,
    "rect": None,
}


def _desktop_ok_error():
    """活动桌面不可用时返回统一错误对象，可用时返回 None"""
    return None if _input_desktop_ok() else dict(DESKTOP_UNAVAILABLE_SHOT)


def _send_input(events):
    """送一批输入事件；SendInput 被拒就抛错，不静默吞掉"""
    n = len(events)
    arr = (_INPUT * n)(*events)
    ret = u.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))
    if ret != n:
        # ctypes.get_last_error() 只在 WinDLL(use_last_error=True) 下有效，
        # windll.user32 是普通 DLL，读出来永远是 0（2026-11-13 修）；直接问 kernel32
        err = ctypes.windll.kernel32.GetLastError()
        raise OSError("SendInput 失败 ret=%d err=%d（5=当前桌面不可输入，87=结构体参数错）" % (ret, err))


def _kb_unicode(code, up=False):
    i = _INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki = _KEYBDINPUT(0, code, (KEYEVENTF_KEYUP if up else 0) | KEYEVENTF_UNICODE, 0, 0)
    return i


def _kb_vk(vk, up=False):
    i = _INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki = _KEYBDINPUT(vk, 0, (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return i


VK_MAP = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "printscreen": 0x2C,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def _vk_of(name):
    n = (name or "").strip().lower()
    if n in VK_MAP:
        return VK_MAP[n]
    if len(n) == 1:
        return ord(n.upper())
    raise ValueError("unknown key: %r" % name)


def press_key(spec, delay=0.03):
    """发一组键：'enter' / 'ctrl+a' / 'ctrl+shift+end'"""
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key spec")
    vks = [_vk_of(p) for p in parts]
    _send_input([_kb_vk(v) for v in vks])
    time.sleep(delay)
    _send_input([_kb_vk(v, True) for v in reversed(vks)])
    return spec


VK_BASE = {" ": 0x20, "\\": 0xDC, "/": 0xBF, "-": 0xBD, ".": 0xBE, "=": 0xBB,
           ";": 0xBA, "'": 0xDE, ",": 0xBC, "[": 0xDB, "]": 0xDD, "`": 0xC0}
VK_SHIFTED = {"_": 0xBD, "~": 0xC0, "!": 0x31, "@": 0x32, "#": 0x33, "$": 0x34,
              "%": 0x35, "^": 0x36, "&": 0x37, "*": 0x38, "(": 0x39, ")": 0x30,
              "+": 0xBB, ":": 0xBA, '"': 0xDE, "<": 0xBC, ">": 0xBE, "?": 0xBF,
              "|": 0xDC}


def _char_vk(ch):
    if ch.isalpha():
        return ord(ch.upper()), False
    if ch.isdigit():
        return 0x30 + int(ch), False
    if ch in VK_BASE:
        return VK_BASE[ch], False
    if ch in VK_SHIFTED:
        return VK_SHIFTED[ch], True
    raise ValueError("VK 路径不支持字符: %r（中文请用 unicode 模式）" % ch)


def _units(ch):
    raw = ch.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def type_text(text, delay_ms=4, mode="unicode"):
    """逐字打一段文字：unicode 模式走 KEYEVENTF_UNICODE（中文/emoji 都行），vk 模式走虚拟键"""
    sent = 0
    for ch in text:
        if ch == "\r":
            continue
        if ch in ("\n", "\t"):
            vk = 0x0D if ch == "\n" else 0x09
            _send_input([_kb_vk(vk), _kb_vk(vk, True)])
        elif mode == "vk":
            vk, need_shift = _char_vk(ch)
            if need_shift:
                _send_input([_kb_vk(0x10), _kb_vk(vk), _kb_vk(vk, True), _kb_vk(0x10, True)])
            else:
                _send_input([_kb_vk(vk), _kb_vk(vk, True)])
        else:
            units = _units(ch)
            ev = [_kb_unicode(x) for x in units] + [_kb_unicode(x, True) for x in reversed(units)]
            _send_input(ev)
        sent += 1
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
    return sent


def type_into(title, target=None, text="", activate=True, delay_ms=4, mode="unicode", enter=False):
    """先激活窗口（可选聚焦控件），再逐字输入；给了 target 就回读值"""
    t0 = time.time()
    if not _input_desktop_ok():
        return dict(INPUT_DESKTOP_UNAVAILABLE)
    bad = _need_window(title, target)
    if bad:
        return bad
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    mode = (mode or "unicode").strip().lower()
    if mode not in ("unicode", "vk"):
        return {"ok": False, "error": "BAD_MODE", "mode": mode,
                "hint": "mode 只认 unicode（默认，中文直打）或 vk（虚拟机等不认 UNICODE 的环境）"}
    delay_ms = _num(delay_ms, 4.0, 0.0, 2000.0)
    w = find_window(title)
    if w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}
    if activate:
        _activate(w)
        # 顶不到前台就别瞎打：键会落到当前前台那个窗口里去（UIPI 提权窗口、抢前台失败都这样）
        if not _is_fg(w):
            return {"ok": False, "error": "WINDOW_NOT_FOREGROUND", "title": title,
                    "hint": "窗口没能顶到前台（可能被系统拦了，或被管理员权限的程序占着）——"
                            "这时打字会打进别的窗口，所以先不发。手动点一下窗口再试，或用 SetFocus 走控件"}
    c = None
    if target:
        c = resolve(w, target)
        if c is None:
            return {"ok": False, "error": "TARGET_NOT_FOUND", "target": target}
        try:
            c.SetFocus()
        except Exception:
            pass
        time.sleep(0.08)
    try:
        sent = type_text(text, delay_ms=delay_ms, mode=mode)
    except Exception as e:
        return {"ok": False, "error": "TYPE_FAILED", "detail": repr(e),
                "hint": "vk 模式只支持英文/符号，中文要用默认的 unicode 模式；也可能是活动桌面不可用（锁屏/UAC）"}
    if enter:
        press_key("enter")
    time.sleep(0.08)
    value = _read_back(c) if c is not None else None
    return {"ok": True, "sent": sent, "mode": mode, "value": value,
            "elapsed": round(time.time() - t0, 3)}


def send_keys(title, keys, activate=True, repeat=1, interval=0.15):
    """先激活窗口再发键；keys 是列表（每个元素是 'ctrl+a' 这类）或单个字符串"""
    t0 = time.time()
    if not _input_desktop_ok():
        return dict(INPUT_DESKTOP_UNAVAILABLE)
    w = find_window(title)
    if w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}
    if activate:
        _activate(w)
        # 同上：顶不到前台就发键，ctrl+a / delete 会落到别的窗口里去
        if not _is_fg(w):
            return {"ok": False, "error": "WINDOW_NOT_FOREGROUND", "title": title,
                    "hint": "窗口没能顶到前台（可能被系统拦了，或被管理员权限的程序占着）——"
                            "这时发键会落到别的窗口，所以先不发。手动点一下窗口再试"}
    try:
        repeat = int(_num(repeat, 1, 1, 200))
    except Exception:
        repeat = 1
    interval = _num(interval, 0.15, 0.0, 5.0)
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)):
        return {"ok": False, "error": "BAD_KEY", "keys": keys,
                "hint": "keys 要字符串或字符串数组，例如 [\"ctrl+a\",\"enter\"]"}
    sent = []
    for _ in range(max(1, repeat)):
        for k in keys:
            try:
                press_key(k)
            except Exception as e:
                return {"ok": False, "error": "BAD_KEY", "key": k, "sent": sent,
                        "detail": repr(e),
                        "hint": "按键写法：单键 enter/tab/esc/f5，组合键 ctrl+a；未知键名会在这里报出来"}
            sent.append(k)
            time.sleep(interval)
    return {"ok": True, "sent": sent, "elapsed": round(time.time() - t0, 3)}


# ══════════════════════════════════════════════════════════════════
# 显示器校准（通用：多屏 / 高 DPI / 锁屏也能读，不依赖输入桌面）
# ══════════════════════════════════════════════════════════════════
class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def calibrate():
    """枚举显示器：物理矩形 / DPI / 缩放系数；另给虚拟桌面范围和进程 DPI 感知级别。"""
    found = []
    CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

    def _cb(hmon, hdc, lprc, data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if u.GetMonitorInfoW(hmon, ctypes.byref(info)):
            found.append((hmon, info))
        return 1

    u.EnumDisplayMonitors(0, 0, CALLBACK(_cb), 0)

    shcore = None
    try:
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
    except Exception:
        shcore = None

    monitors = []
    for hmon, info in found:
        dpi_x = dpi_y = 96
        if shcore is not None:
            try:
                dx, dy = ctypes.c_uint(), ctypes.c_uint()
                if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value > 0:
                    dpi_x, dpi_y = dx.value, dy.value
            except Exception:
                pass
        r = info.rcMonitor
        monitors.append({
            "device": info.szDevice,
            "primary": bool(info.dwFlags & 0x1),
            "rect": [r.left, r.top, r.right, r.bottom],
            "size": [r.right - r.left, r.bottom - r.top],
            "dpi": [dpi_x, dpi_y],
            "scale": round(dpi_x / 96.0, 3),
        })

    out = {
        "ok": True,
        "monitors": monitors,
        "count": len(monitors),
        "virtual_rect": list(_virtual_rect()),
        "system_dpi": 96,
        "process_dpi_aware": None,
        "note": "坐标全为物理像素；截图坐标 = 物理坐标 - 截图 origin",
    }
    try:
        out["system_dpi"] = u.GetDpiForSystem()
    except Exception:
        pass
    try:
        ctx = u.GetThreadDpiAwarenessContext()
        aware = u.GetAwarenessFromDpiAwarenessContext(ctx)
        out["process_dpi_aware"] = {0: "unaware", 1: "system", 2: "per-monitor"}.get(aware, "per-monitor-v2")
    except Exception:
        pass
    return out



# ══════════════════════════════════════════════════════════════════
# 动作类（0.2.5）：悬停 hover / 滚轮 scroll / 拖拽 drag
# 之前只有"点击、写值、打字"，遇到长列表要滚、三级菜单要悬停、
# 文件/滑块要拖，就只能靠估坐标，翻过两次车。这三个动作全部走
# 真鼠标（SendInput 绝对坐标），并自带"动作后看有没有变化"的校验。
# ══════════════════════════════════════════════════════════════════
INPUT_MOUSE = 0
ME_MOVE = 0x0001
ME_LEFTDOWN = 0x0002
ME_LEFTUP = 0x0004
ME_RIGHTDOWN = 0x0008
ME_RIGHTUP = 0x0010
ME_MIDDLEDOWN = 0x0020
ME_MIDDLEUP = 0x0040
ME_WHEEL = 0x0800
ME_HWHEEL = 0x1000
ME_ABSOLUTE = 0x8000
ME_VIRTUALDESK = 0x4000

_BTN = {
    "left": (ME_LEFTDOWN, ME_LEFTUP),
    "right": (ME_RIGHTDOWN, ME_RIGHTUP),
    "middle": (ME_MIDDLEDOWN, ME_MIDDLEUP),
}


def _abs_move(x, y):
    """真鼠标移到物理坐标：绝对坐标要按虚拟桌面归一化到 0..65535，否则高 DPI 多屏会偏"""
    vx1, vy1, vx2, vy2 = _virtual_rect()
    vw = max(1, vx2 - vx1 - 1)
    vh = max(1, vy2 - vy1 - 1)
    nx = int(round((int(x) - vx1) * 65535.0 / vw))
    ny = int(round((int(y) - vy1) * 65535.0 / vh))
    ev = _INPUT()
    ev.type = INPUT_MOUSE
    ev.u.mi = _MOUSEINPUT(nx, ny, 0, ME_MOVE | ME_ABSOLUTE | ME_VIRTUALDESK, 0, 0)
    _send_input([ev])


def _point_for(w, target=None, x=None, y=None, depth=12):
    """把 target / x,y / 窗口中心 统一成物理坐标点：(px, py, 控件, meta)"""
    if x is not None and y is not None:
        return int(x), int(y), None, {"mode": "xy"}
    if target:
        c, meta = resolve2(w, target, depth)
        if c is None:
            return None, None, None, meta
        px, py = _center(c.BoundingRectangle)
        return px, py, c, meta
    if w is None:
        return None, None, None, {"mode": "none"}
    px, py = _center(w.BoundingRectangle)
    return px, py, None, {"mode": "window-center"}


def _missing_point(target, meta):
    return {
        "ok": False,
        "error": "TARGET_NOT_FOUND",
        "target": target,
        "candidates": meta.get("candidates") if isinstance(meta, dict) else None,
        "hint": "控件没找到：①先用 screen_map 看真实名字（工具名后带助记符，如「文件(F)」）"
                "②窗口没在前台时先 screen_wait 等它出来 ③这个界面可能读不到控件树（Electron/Qt/自绘），改成传 x/y 物理坐标",
    }


def _read_scroll(sp):
    try:
        return {"h": round(float(sp.HorizontalScrollPercent), 1),
                "v": round(float(sp.VerticalScrollPercent), 1)}
    except Exception:
        return None


def _scroll_pattern_of(c, max_up=10):
    """从控件往上找 ScrollPattern：列表自身常常没有，滚动容器才是支持的那个"""
    node = c
    for _ in range(max_up):
        if node is None:
            return None, None
        try:
            sp = node.GetScrollPattern()
            if sp is not None:
                return node, sp
        except Exception:
            pass
        try:
            node = node.GetParentControl()
        except Exception:
            return None, None
    return None, None


def hover(title, target=None, x=None, y=None, wait=0.6, activate=True, detect_popup=True):
    """悬停：把鼠标停到控件/坐标上等一会儿，并报告这期间新冒出来的弹层。

    专治三级菜单：先 hover 一级 → 返回新弹出的子菜单项（名字+坐标）→ 再 hover/click 下一级，
    不用再猜悬停多久、也不用整屏截图找菜单。
    """
    t0 = time.time()
    bad = _desktop_ok_error()
    if bad:
        return bad
    bad = _need_window(title, target)
    if bad:
        return bad
    wait = _num(wait, 0.6, 0.0, 30.0)
    w = find_window(title) if title else None
    if title and w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title,
                "hint": "窗口没找到：先 screen_windows 看真实标题；标题只要包含就行"}
    px, py, _c, meta = _point_for(w, target, x, y)
    if px is None:
        return _missing_point(target, meta)

    activated = False
    if activate and w is not None and not _is_fg(w):
        _activate(w)
        activated = True

    before = {ww["hwnd"] for ww in _win32_windows(include_untitled=True)}
    _abs_move(px, py)
    time.sleep(max(0.0, float(wait)))

    popups = []
    if detect_popup:
        for ww in _win32_windows(include_untitled=True):
            if ww["hwnd"] in before:
                continue
            cls = ww["class"] or ""
            if cls != "#32768" and not cls.startswith("tooltips") and cls != "Chrome_WidgetWin_1":
                continue
            item = {"hwnd": ww["hwnd"], "class": cls, "title": ww["title"] or "",
                    "rect": ww["rect"], "items": [], "count": 0}
            # 菜单刚弹出来的那一瞬常常还读不到子项（动画/延迟创建），重试几次再下结论
            for attempt in range(4):
                try:
                    pc = auto.ControlFromHandle(ww["hwnd"])
                    for ch in (pc.GetChildren() if pc else []):
                        r = _rect(ch.BoundingRectangle)
                        nm = (ch.Name or "").replace("|", "/")[:60]
                        if not nm:
                            continue
                        item["items"].append({"name": nm, "type": ch.ControlTypeName,
                                              "rect": r,
                                              "center": [(r[0] + r[2]) // 2, (r[1] + r[3]) // 2]})
                except Exception as e:
                    item.setdefault("errors", []).append(repr(e)[:160])
                if item["items"]:
                    break
                time.sleep(0.15)
            item["count"] = len(item["items"])
            popups.append(item)

    out = {
        "ok": True,
        "point": [px, py],
        "target": target or None,
        "match": meta.get("mode") if isinstance(meta, dict) else None,
        "fg_ok": _is_fg(w) if w is not None else None,
        "activated": activated,
        "popup_count": len(popups),
        "popups": popups,
        "elapsed": round(time.time() - t0, 3),
    }
    if popups:
        out["hint"] = "冒出新弹层了，直接用里面的 name 继续 hover/click（这些是独立窗口，不在原窗口控件树里）"
    elif detect_popup:
        out["hint"] = "这段时间没有新弹层：①悬停点可能不对（先 screen_map 核对中心）②这个界面读不到控件树，改用 x/y"
    return out


def _snap_scroll(c, px, py, half=200):
    """滚动校验用的截图：有控件就截控件左上角一块（文字/图标都在左边，
    按中心截宽控件会截到一片空白，导致"滚了但看不出变化"），没有控件才截点周围。"""
    from PIL import ImageGrab
    try:
        vx1, vy1, vx2, vy2 = _virtual_rect()
        if c is not None:
            r = c.BoundingRectangle
            x1, y1 = int(r.left), int(r.top)
            x2 = min(int(r.right), x1 + 560)
            y2 = min(int(r.bottom), y1 + 420)
        else:
            x1, y1 = int(px) - half, int(py) - half
            x2, y2 = int(px) + half, int(py) + half
        box = (max(vx1, x1), max(vy1, y1), min(vx2, x2), min(vy2, y2))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return None
        return ImageGrab.grab(bbox=box, all_screens=True)
    except Exception:
        return None


def scroll(title, target=None, x=None, y=None, direction="down", amount=3,
           activate=True, verify=True, wait=0.3):
    """滚轮：把指针放到控件/坐标上，滚 direction 方向 amount 格。

    能用 UIA ScrollPattern 读位置时，动作前后各读一次百分比，明确告诉你滚没滚动；
    读不到位置（自绘界面）就返回 verified=null，靠截图确认——不假装成功。
    """
    t0 = time.time()
    bad = _desktop_ok_error()
    if bad:
        return bad
    direction = str(direction or "down").strip().lower()
    if direction not in ("up", "down", "left", "right"):
        return {"ok": False, "error": "BAD_DIRECTION", "direction": direction,
                "hint": "direction 只认 up / down / left / right"}
    bad = _need_window(title, target)
    if bad:
        return bad
    wait = _num(wait, 0.3, 0.0, 10.0)
    try:
        amount = max(1, min(60, int(amount)))
    except Exception:
        amount = 3

    w = find_window(title) if title else None
    if title and w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}
    px, py, c, meta = _point_for(w, target, x, y)
    if px is None:
        return _missing_point(target, meta)

    node, sp = (None, None)
    if c is not None:
        node, sp = _scroll_pattern_of(c)
    before = _read_scroll(sp) if sp is not None else None

    if activate and w is not None and not _is_fg(w):
        _activate(w)
    shot_before = _snap_scroll(c, px, py) if verify else None
    _abs_move(px, py)
    time.sleep(0.05)

    vertical = direction in ("up", "down")
    flag = ME_WHEEL if vertical else ME_HWHEEL
    sign = 1 if direction in ("up", "right") else -1
    # 一格 120；分批发（一次灌太多，有的列表会丢）
    left = amount
    while left > 0:
        chunk = min(3, left)
        u.mouse_event(flag, 0, 0, 120 * sign * chunk, 0)
        left -= chunk
        time.sleep(0.02)
    time.sleep(max(0.0, float(wait)))

    after = _read_scroll(sp) if sp is not None else None
    verified = None
    moved = None
    changed_px = None
    if before is not None and after is not None:
        key = "v" if vertical else "h"
        moved = round(abs(after[key] - before[key]), 1)
        verified = moved > 0
    elif shot_before is not None:
        # 读不到滚动位置（自绘/网页/资源管理器列表）就比画面：肉眼能看出滚没滚
        shot_after = _snap_scroll(c, px, py)
        _ratio, changed_px = _diff_stats(shot_before, shot_after)
        if changed_px is not None:
            verified = changed_px > 0

    out = {
        "ok": True,
        "point": [px, py],
        "target": target or None,
        "match": meta.get("mode") if isinstance(meta, dict) else None,
        "direction": direction,
        "amount": amount,
        "scroll_before": before,
        "scroll_after": after,
        "scroll_delta": moved,
        "changed_px": changed_px,
        "verified": verified,
        "fg_ok": _is_fg(w) if w is not None else None,
        "elapsed": round(time.time() - t0, 3),
    }
    if verified is False:
        out["hint"] = ("滚了但位置没变：可能到头/到底了，或者指针下面不是可滚动区域"
                       "（先 screen_map 确认 target 是可滚容器，或改用 x/y 对准列表本体）")
    elif verified is None:
        out["hint"] = "这个控件不暴露滚动位置（自绘/网页），画面也没截到，滚没滚动用 screen_shot 前后对比确认"
    return out


def drag(title, frm=None, to=None, duration_ms=400, steps=16, button="left",
         activate=True, verify=True, wait=0.2):
    """拖拽：从 frm 拖到 to（各可给 name/aid/type 或 x/y 物理坐标）。

    按 duration 分 steps 步移动（一次跳过去很多程序不认，会当成单击）。
    给了 verify 就截目标点周围，返回 changed_px——0 说明这次拖拽没产生任何画面变化。
    """
    t0 = time.time()
    bad = _desktop_ok_error()
    if bad:
        return bad
    button = (button or "left").strip().lower()
    if button not in _BTN:
        return {"ok": False, "error": "BAD_BUTTON", "button": button,
                "hint": "button 只认 left / right / middle"}
    frm = frm or {}
    to = to or {}
    bad = _need_window(title, frm.get("target") or to.get("target"))
    if bad:
        return bad
    # 起点/终点必须明给：以前缺省会从窗口中心开始拖，
    # 拖到哪、拖了什么全看运气（2026-11-13 实测：没给 from 就从窗口正中拖了一把）。
    if not frm.get("target") and (frm.get("x") is None or frm.get("y") is None):
        return {"ok": False, "error": "MISSING_FROM", "target": frm.get("target"),
                "hint": "拖拽起点必须明说：from_name（控件名）或 from_x/from_y（物理坐标）。不要依赖窗口中心"}
    if not to.get("target") and (to.get("x") is None or to.get("y") is None):
        return {"ok": False, "error": "MISSING_TO", "target": to.get("target"),
                "hint": "拖拽终点必须明说：to_name（控件名）或 to_x/to_y（物理坐标）"}

    w = find_window(title) if title else None
    if title and w is None:
        return {"ok": False, "error": "WINDOW_NOT_FOUND", "title": title}
    if activate and w is not None and not _is_fg(w):
        _activate(w)

    fx, fy, _fc, fmeta = _point_for(w, frm.get("target"), frm.get("x"), frm.get("y"))
    if fx is None:
        return {"ok": False, "error": "TARGET_NOT_FOUND", "which": "from", "target": frm.get("target"),
                "hint": "拖拽起点没定位到：给 from_name 或 from_x/from_y"}
    tx, ty, _tc, tmeta = _point_for(w, to.get("target"), to.get("x"), to.get("y"))
    if tx is None:
        return {"ok": False, "error": "TARGET_NOT_FOUND", "which": "to", "target": to.get("target"),
                "hint": "拖拽终点没定位到：给 to_name 或 to_x/to_y"}

    try:
        steps = max(2, min(80, int(steps)))
    except Exception:
        steps = 16
    duration_ms = int(_num(duration_ms, 400, 0, 10000))
    wait = _num(wait, 0.2, 0.0, 10.0)

    shot_before = None
    if verify:
        shot_before = _snap_region(tx, ty, half=140)
    win_before = _rect(w.BoundingRectangle) if w is not None else None

    down, up = _BTN[button]
    _abs_move(fx, fy)
    time.sleep(0.10)
    u.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.08)
    step_sleep = (duration_ms / 1000.0) / steps if duration_ms else 0
    for i in range(1, steps + 1):
        ix = fx + (tx - fx) * i / float(steps)
        iy = fy + (ty - fy) * i / float(steps)
        _abs_move(int(round(ix)), int(round(iy)))
        if step_sleep:
            time.sleep(step_sleep)
    time.sleep(0.08)
    u.mouse_event(up, 0, 0, 0, 0)
    time.sleep(max(0.05, float(wait)))

    changed_px = None
    verified = None
    if verify and shot_before is not None:
        shot_after = _snap_region(tx, ty, half=140)
        _ratio, changed_px = _diff_stats(shot_before, shot_after)
        if changed_px is not None:
            verified = changed_px > 0
    win_after = _rect(w.BoundingRectangle) if w is not None else None
    win_moved = bool(win_before and win_after and win_before != win_after)

    out = {
        "ok": True,
        "from": [fx, fy],
        "to": [tx, ty],
        "from_match": fmeta.get("mode") if isinstance(fmeta, dict) else None,
        "to_match": tmeta.get("mode") if isinstance(tmeta, dict) else None,
        "button": button,
        "steps": steps,
        "duration_ms": duration_ms,
        "changed_px": changed_px,
        "verified": verified,
        "window_rect_before": win_before,
        "window_rect_after": win_after,
        "window_moved": win_moved,
        "elapsed": round(time.time() - t0, 3),
    }
    if verified is False and not win_moved:
        out["hint"] = ("拖完画面没变化：①起点没按在可拖对象上（标题栏/文件图标/滑块）"
                       "②目标是选中文字或不可拖区域 ③拖拽被程序拒绝")
    return out
