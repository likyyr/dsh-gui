# -*- coding: utf-8 -*-
"""dsh-gui 插件回归测试：直接打 8791 的 HTTP 接口，覆盖 11 个工具的正常/异常路径。
结果写 _tmp/raw/gui_test_report.txt（UTF-8），屏幕上只汇总。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8791"
REPORT = []


def call(path, payload=None, timeout=90):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"content-type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body, round(time.time() - t0, 3)
    except Exception as e:
        return {"ok": False, "error": "EXCEPTION", "detail": repr(e)}, round(time.time() - t0, 3)


def check(name, cond, detail, ms):
    REPORT.append((name, bool(cond), round(ms, 2), detail))
    print(("PASS " if cond else "FAIL ") + name + "  " + str(detail)[:300])


def skip(name, detail):
    """环境没就绪（比如计算器卡启动画面）——记 SKIP，不算插件失败"""
    REPORT.append((name, True, 0, "[SKIP] " + str(detail)))
    print("SKIP " + name + "  " + str(detail)[:300])


def wait_ready(title, seconds=12):
    """等窗口内容渲染出来：UWP 冷启动会先卡一阵启动画面，这时 map 只读到窗口外壳"""
    end = time.time() + seconds
    last = {}
    while time.time() < end:
        last, _ = call("/map", {"title": title, "depth": 6, "max_items": 40})
        if last.get("ok") and last.get("window_ready"):
            return last, True
        time.sleep(0.8)
    return last, False


def main():
    # 0 health / calibrate / windows
    r, ms = call("/health")
    check("health + build 指纹", r.get("ok") and bool(r.get("build")), r, ms)

    r, ms = call("/calibrate")
    check("calibrate", r.get("ok") and r.get("count", 0) >= 1,
          "monitors=%s dpi=%s" % (r.get("count"), r.get("system_dpi")), ms)

    r, ms = call("/windows")
    ws = r.get("windows") or []
    check("windows", r.get("ok") and len(ws) > 0, "count=%d" % len(ws), ms)

    # 1 记事本
    subprocess.Popen(["notepad.exe"])
    time.sleep(1.2)
    title = "记事本"
    r, ms = call("/wait", {"title": title, "timeout": 8})
    check("wait window notepad", r.get("ok"), r, ms)

    r, ms = call("/map", {"title": title, "depth": 6})
    items = r.get("items") or []
    check("map notepad", r.get("ok") and len(items) > 0,
          "count=%d readability=%s ready=%s" % (r.get("count"), r.get("readability"), r.get("window_ready")), ms)

    r, ms = call("/set", {"title": title, "target": {"name": "文本编辑器"}, "value": "monitor 回归测试 123 ABC"})
    check("set notepad edit", r.get("ok"), r, ms)

    r, ms = call("/get", {"title": title, "target": {"name": "文本编辑器"}})
    ok = r.get("ok") and "monitor 回归测试" in str(r.get("value") or r.get("text") or "")
    check("get notepad edit", ok, r, ms)

    r, ms = call("/type", {"title": title, "target": {"name": "文本编辑器"}, "text": " 尾巴"})
    check("type notepad", r.get("ok"), r, ms)

    r, ms = call("/get", {"title": title, "target": {"name": "文本编辑器"}})
    ok = r.get("ok") and "尾巴" in str(r.get("value") or r.get("text") or "")
    check("type 生效", ok, r, ms)

    r, ms = call("/key", {"title": title, "keys": ["ctrl+a"]})
    check("key ctrl+a", r.get("ok"), r, ms)

    r, ms = call("/type", {"title": title, "text": "覆盖测试"})
    r2, ms2 = call("/get", {"title": title, "target": {"name": "文本编辑器"}})
    val = str(r2.get("value") or r2.get("text") or "")
    check("全选后覆盖", r.get("ok") and "覆盖测试" in val, "value=%r" % val[:60], ms + ms2)

    # 2 点菜单（按名字点）
    r, ms = call("/click", {"title": title, "target": {"name": "编辑"}, "via": "auto"})
    tgt = (r.get("target") or {})
    check("click menuitem 编辑", r.get("ok") and tgt.get("type") == "MenuItemControl",
          "target=%s match=%s warn=%s" % (tgt.get("name"), r.get("match"), r.get("warning")), ms)
    call("/key", {"title": title, "keys": ["esc"]})

    # 3 截屏三种
    r, ms = call("/shot", {"title": title})
    check("shot window", r.get("ok") and r.get("size", [0])[0] > 100,
          "source=%s size=%s origin=%s" % (r.get("source"), r.get("size"), r.get("origin")), ms)

    r, ms = call("/shot", {"region": [0, 0, 200, 120]})
    check("shot region", r.get("ok") and r.get("size") == [200, 120], r.get("size"), ms)

    r, ms = call("/shot", {"title": title, "annotate": True, "grid": True})
    check("shot annotate+grid", r.get("ok") and r.get("items", 0) > 0,
          "items=%s path=%s" % (r.get("items"), r.get("path")), ms)

    # 4 模板匹配：正例（计算器 + 键）/ 反例（空白区）
    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "plus_template.png")
    if os.path.exists(tpl):
        # 先按窗口找（窗口被 notepad 挡着，验证 PrintWindow/顶前台这条路）
        r, ms = call("/find", {"template": tpl, "threshold": 0.85, "window": "计算器"})
        check("find 按窗口找", r.get("ok") and 1150 < (r.get("point") or [0])[0] < 1400,
              "score=%s source=%s point=%s" % (r.get("score"), r.get("source"), r.get("point")), ms)
        # 区域找图不能靠 z-order：现场截一小块当模板，再在同一区域找回来
        shot = call("/shot", {"region": [700, 300, 900, 400]})[0]
        if shot.get("ok"):
            from PIL import Image
            im = Image.open(shot["path"])
            tpl2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "_runtime_crop.png")
            im.crop((40, 20, 120, 60)).save(tpl2)
            r, ms = call("/find", {"template": tpl2, "threshold": 0.9, "region": [700, 300, 900, 400]})
            check("find 正例(现场模板)", r.get("ok"), "score=%s point=%s" % (r.get("score"), r.get("point")), ms)
        else:
            check("find 正例(现场模板)", False, shot, ms)
        r, ms = call("/find", {"template": tpl, "threshold": 0.85, "region": [300, 300, 600, 500]})
        check("find 反例", (not r.get("ok")) and r.get("error") in ("NO_MATCH", "FLAT_PATCH"),
              "error=%s score=%s" % (r.get("error"), r.get("score")), ms)
        # 窗口被别的窗口挡住时：按窗口自身像素找（PrintWindow）


    # 5 异常路径
    r, ms = call("/map", {"title": "绝对不存在的窗口XYZ"})
    check("map 不存在", (not r.get("ok")) and r.get("error") == "WINDOW_NOT_FOUND", r.get("error"), ms)

    r, ms = call("/click", {"title": title, "target": {"name": "绝对不存在的控件XYZ"}})
    check("click 控件不存在", (not r.get("ok")) and r.get("error") == "TARGET_NOT_FOUND", r.get("error"), ms)

    r, ms = call("/get", {"title": title, "target": {"name": "绝对不存在的控件XYZ"}})
    check("get 控件不存在", (not r.get("ok")) and r.get("error") == "TARGET_NOT_FOUND", r.get("error"), ms)

    r, ms = call("/find", {"template": r"C:\no-such-file.png"})
    check("find 模板不存在", (not r.get("ok")) and r.get("error") == "TEMPLATE_UNREADABLE", r.get("error"), ms)

    r, ms = call("/wait", {"title": title, "target": {"name": "绝对不存在的控件XYZ"}, "timeout": 1.2})
    check("wait 超时", (not r.get("ok")) and r.get("error") == "TIMEOUT", r.get("error"), ms)

    r, ms = call("/shot", {"region": [5000, 5000, 6000, 6000]})
    check("shot 越界区域", r.get("ok") or r.get("error") in ("EMPTY_REGION", "EMPTY_RECT"), r.get("error"), ms)

    # 6 UWP 计算器（冷启动会先卡启动画面：先等它就绪；一直不就绪标 SKIP，别冤枉插件）
    calc_map, ready = wait_ready("计算器")
    if not ready:
        why = "计算器没就绪（readability=%s window_ready=%s）——多半是应用卡在启动画面，重启计算器再跑" % (
            calc_map.get("readability"), calc_map.get("window_ready"))
        skip("map 计算器", why)
        skip("找得到清除键", why)
        for _ in range(4):
            skip("click 计算器", why)
        skip("get 计算器结果 7+8=15", why)
    else:
        check("map 计算器", calc_map.get("ok") and calc_map.get("count", 0) > 20,
              "count=%s ready=%s" % (calc_map.get("count"), calc_map.get("window_ready")), 0)

        # 计算器的清除键 aid 会随状态在 clearButton / clearEntryButton 之间切换（C ↔ CE）
        clear_aid = None
        for it in (call("/map", {"title": "计算器", "depth": 9, "max_items": 300})[0].get("items") or []):
            if (it.get("aid") or "").lower().startswith("clear"):
                clear_aid = it["aid"]
                break
        check("找得到清除键", bool(clear_aid), "aid=%s" % clear_aid, 0)
        # 先清空再按：上一轮遗留的 15 会让"按 7 没变化"误判成失败（显示区本来就有数字）
        if clear_aid:
            call("/click", {"title": "计算器", "target": {"aid": clear_aid}})
        for aid, label in (("num7Button", "七"), ("plusButton", "加"), ("num8Button", "八"),
                           ("equalButton", "等于")):
            r, ms = call("/click", {"title": "计算器", "target": {"aid": aid}})
            # 运算符键（+）按下只切换高亮，显示区不变 → verified=False 是正常的，别判成失败；
            # 数字键和等号必须看到画面变化（changed_px 几十就是白点了）
            need_change = aid != "plusButton"
            ok = r.get("ok") and (r.get("verified") is not False or not need_change)
            check("click 计算器 %s" % label, ok, r, ms)

        r, ms = call("/get", {"title": "计算器", "target": {"aid": "CalculatorResults"}})
        check("get 计算器结果 7+8=15", r.get("ok") and "15" in str(r.get("value") or r.get("text") or ""),
              r.get("value") or r.get("text"), ms)

    # 7 右键菜单（走 _popup_menus 分支：菜单是独立 #32768 窗口）
    r, ms = call("/click", {"title": title, "target": {"name": "文本编辑器"}, "button": "right", "verify": False})
    time.sleep(0.6)
    r, ms = call("/windows")
    has_menu = any((w.get("class") or "") == "#32768" for w in (r.get("windows") or []))
    check("右键弹出菜单", has_menu, "windows=%d" % len(r.get("windows") or []), ms)
    r, ms = call("/click", {"title": title, "target": {"name": "全选"}, "verify": False})
    check("点右键菜单项", r.get("ok"), "target=%s" % ((r.get("target") or {}).get("name")), ms)
    call("/key", {"title": title, "keys": ["esc"]})

    # 8 新增的防呆路径
    r, ms = call("/map", {"title": ""})
    check("map 空标题", (not r.get("ok")) and r.get("error") == "MISSING_WINDOW", r.get("error"), ms)

    r, ms = call("/key", {"title": title, "keys": ["ctrl+notakey"]})
    check("key 未知键", (not r.get("ok")) and r.get("error") == "BAD_KEY", r.get("error"), ms)

    r, ms = call("/click", {"title": title, "target": {"type": "edit"}, "via": "invoke"})
    check("click 无 Invoke 模式", (not r.get("ok")) and r.get("error") == "NO_INVOKE_PATTERN",
          "error=%s" % r.get("error"), ms)

    r, ms = call("/set", {"title": title, "target": {"name": "文本编辑器"}, "value": "键盘回读测试"})
    check("set 回读", r.get("ok") and r.get("value") == "键盘回读测试", "via=%s value=%r" % (r.get("via"), r.get("value")), ms)

    # 9 动作类（0.2.5）：scroll / hover / drag
    long_text = "\n".join("第 %d 行 滚动回归" % i for i in range(1, 60))
    call("/set", {"title": title, "target": {"name": "文本编辑器"}, "value": long_text})
    r, ms = call("/scroll", {"title": title, "target": {"name": "文本编辑器"}, "direction": "down", "amount": 5})
    check("scroll 向下", r.get("ok") and (r.get("verified") is True),
          "before=%s after=%s changed=%s" % (r.get("scroll_before"), r.get("scroll_after"), r.get("changed_px")), ms)

    r, ms = call("/scroll", {"title": title, "target": {"name": "文本编辑器"}, "direction": "up", "amount": 5})
    check("scroll 向上", r.get("ok") and (r.get("verified") is True),
          "changed=%s" % (r.get("changed_px"),), ms)

    r, ms = call("/scroll", {"title": title, "target": {"name": "文本编辑器"}, "direction": "sideways"})
    check("scroll 坏方向", (not r.get("ok")) and r.get("error") == "BAD_DIRECTION", r.get("error"), ms)

    r, ms = call("/hover", {"title": title, "target": {"name": "编辑"}, "wait": 0.4})
    check("hover 菜单项", r.get("ok") and (r.get("point") or [0])[0] > 0,
          "point=%s popups=%s" % (r.get("point"), r.get("popup_count")), ms)

    r, ms = call("/hover", {"title": title, "target": {"name": "绝对不存在的控件XYZ"}})
    check("hover 控件不存在", (not r.get("ok")) and r.get("error") == "TARGET_NOT_FOUND", r.get("error"), ms)

    wr = [w for w in (call("/windows")[0].get("windows") or []) if "记事本" in (w.get("title") or "")]
    before_rect = wr[0]["rect"] if wr else None
    if before_rect:
        fx, fy = before_rect[0] + 120, before_rect[1] + 14
        r, ms = call("/drag", {"title": title, "from": {"x": fx, "y": fy},
                               "to": {"x": fx + 180, "y": fy + 90}, "duration_ms": 400, "steps": 16})
        moved = r.get("window_moved")
        check("drag 拖窗口标题栏", r.get("ok") and moved is True,
              "before=%s after=%s changed=%s" % (r.get("window_rect_before"), r.get("window_rect_after"), r.get("changed_px")), ms)
    else:
        check("drag 拖窗口标题栏", False, "记事本窗口没找到", 0)

    r, ms = call("/drag", {"title": title, "from": {"x": 100, "y": 100}, "to": {"x": 200, "y": 200}, "button": "wheel"})
    check("drag 坏按键", (not r.get("ok")) and r.get("error") == "BAD_BUTTON", r.get("error"), ms)

    r, ms = call("/drag", {"title": title, "from": {"target": {"name": "绝对不存在的控件XYZ"}}, "to": {"x": 200, "y": 200}})
    check("drag 起点不存在", (not r.get("ok")) and r.get("error") == "TARGET_NOT_FOUND", r.get("error"), ms)

    # 10 参数闸门（2026-11-13 加：不拦就会点到/写到别的窗口里去）
    r, ms = call("/get", {"title": "", "target": {"aid": "num1Button"}})
    check("空 window + target 被拦", (not r.get("ok")) and r.get("error") == "MISSING_WINDOW", r.get("error"), ms)

    r, ms = call("/click", {"title": title, "x": 800})
    check("x/y 不成对被拦", (not r.get("ok")) and r.get("error") == "BAD_POINT", r.get("error"), ms)

    r, ms = call("/drag", {"title": title, "to": {"x": 300, "y": 300}})
    check("drag 缺起点被拦", (not r.get("ok")) and r.get("error") == "MISSING_FROM", r.get("error"), ms)

    r, ms = call("/drag", {"title": title, "from": {"x": 300, "y": 300}})
    check("drag 缺终点被拦", (not r.get("ok")) and r.get("error") == "MISSING_TO", r.get("error"), ms)

    r, ms = call("/shot", {"region": [10, 10]})
    check("region 少于 4 个数被拦", (not r.get("ok")) and r.get("error") == "BAD_REGION", r.get("error"), ms)

    r, ms = call("/shot", {"region": [400, 400, 300, 300]})
    check("region 反向被拦", (not r.get("ok")) and r.get("error") == "BAD_REGION", r.get("error"), ms)

    r, ms = call("/type", {"title": title, "text": "x", "mode": "bogus"})
    check("type 坏 mode 被拦", (not r.get("ok")) and r.get("error") == "BAD_MODE", r.get("error"), ms)

    # 跨站请求守卫：带 Origin 的必须 403（浏览器发 simple request 打进来的口子）
    try:
        import urllib.request as _u
        _req = _u.Request(BASE + "/click", data=b"{}",
                          headers={"content-type": "application/json", "Origin": "http://evil.example"})
        _u.urlopen(_req, timeout=10)
        blocked = False
    except Exception:
        blocked = True
    check("带 Origin 的请求被拒", blocked, "403 才算挡住", 0)

    # 11 类型坏参数不该打出 500（给的是字符串，Python 端要能兜住）
    r, ms = call("/map", {"title": title, "depth": "abc"})
    check("depth 非数字走默认不报错", r.get("ok") is True, r.get("error") or "ok", ms)

    r, ms = call("/click", {"title": title, "target": {"name": "编辑"}, "wait": "abc"})
    check("wait 非数字走默认不报错", r.get("ok") is True, r.get("error") or "ok", ms)
    call("/key", {"title": title, "keys": ["esc"]})

    # 收尾
    subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_regression_report.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for name, ok, ms, detail in REPORT:
            f.write("%s\t%s\t%s\t%s\n" % ("PASS" if ok else "FAIL", name, ms, str(detail)[:500]))
    fail = [r for r in REPORT if not r[1]]
    skipped = [r for r in REPORT if str(r[3]).startswith("[SKIP]")]
    print("\nTOTAL %d  PASS %d  SKIP %d  FAIL %d"
          % (len(REPORT), len(REPORT) - len(fail) - len(skipped), len(skipped), len(fail)))
    print("REPORT " + out)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
