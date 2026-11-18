# -*- coding: utf-8 -*-
"""
gui_server.py —— 自创 GUI 插件的常驻服务（HTTP + JSON）
端口默认 8791；插件壳按需拉起。
端点（全部 POST，JSON）：
  /health                    -> 存活 + build（源码指纹，壳用它判断要不要换新进程）
  /shutdown                  -> 让服务自己退出（壳发现源码变了会先调它）
  /windows                   -> 顶层窗口列表
  /map    {title,depth,include_unnamed,max_items}
  /click  {title,target,via,activate,wait}
  /set    {title,target,value,fallback_keys}
  /get    {title,target}
  /wait   {title,target,timeout}
  /shot   {title,annotate,out_path}
  /type   {title,target,text,mode,delay_ms,enter}
  /key    {title,keys,repeat,interval}
  /find   {template,threshold,region,title}
  /hover  {title,target,x,y,wait,activate,detect_popup}
  /scroll {title,target,x,y,direction,amount,activate,verify}
  /drag   {title,from,to,duration_ms,steps,button,activate,verify}
  /calibrate
"""
import sys
import os
import json
import time
import ctypes
import hashlib
import threading
import traceback
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 依赖引导：vendor 优先，缺了自动 pip 装进去（只装一次）──────────────────
# 插件做成"拷到哪都能跑"的自包含包：vendor 里带 uiautomation / comtypes / pillow，
# 宿主 Python 没装也能用。换了 Python 版本导致二进制不兼容时，自动补装到 vendor。
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

# uiautomation / Pillow 是硬依赖；cv2 / numpy 只影响 screen_find 和点后自验，
# 缺了不装（opencv 太大），但那两项功能会明说 NO_CV2，不会假装成功。
_REQUIRED = [("uiautomation", "uiautomation"), ("PIL", "pillow")]


def _missing_deps():
    miss = []
    for mod, pkg in _REQUIRED:
        try:
            __import__(mod)
        except Exception:
            miss.append(pkg)
    return miss


def _bootstrap_deps():
    miss = _missing_deps()
    if not miss:
        return
    print("[gui_server] 缺少依赖 %s，安装到 %s" % (", ".join(miss), _VENDOR), flush=True)
    try:
        os.makedirs(_VENDOR, exist_ok=True)
        index = os.environ.get("DSH_GUI_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
        cmd = [sys.executable, "-m", "pip", "install", "--target", _VENDOR,
               "--upgrade", "--no-warn-script-location", "--disable-pip-version-check",
               "--progress-bar", "off", "--only-binary", ":all:",
               "-i", index, "--timeout", "8", "--retries", "1"] + miss
        env = dict(os.environ)
        # 有的机器 pip.ini 里开了 require-virtualenv，--target 会被拒；这里显式关掉
        env["PIP_REQUIRE_VIRTUALENV"] = "false"
        # 不捕获输出：pip 的进度、报错原文直接进服务日志，出问题能看见
        r = subprocess.run(cmd, timeout=150, env=env)
        if r.returncode == 0:
            print("[gui_server] 依赖安装完成", flush=True)
        else:
            print("[gui_server] pip 安装失败（exit=%s）。可手动执行：%s" % (r.returncode, " ".join(cmd)), flush=True)
    except Exception as e:
        print("[gui_server] pip 安装异常：%r" % (e,), flush=True)
    if _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)


_bootstrap_deps()

import gui_core as core

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
START = time.time()


def _build_id():
    """源码指纹：壳（index.js）用它判断 8791 上跑的是不是当前代码。
    Windows 上父进程退出不会带走子进程，改完源码不重启 DSH 时最容易踩：
    旧 Python 服务还在监听、壳以为服务活着，于是"改了代码没生效"。"""
    h = hashlib.sha1()
    for f in ("gui_core.py", "gui_server.py"):
        try:
            with open(os.path.join(_HERE, f), "rb") as fh:
                h.update(fh.read())
        except Exception:
            pass
    return h.hexdigest()[:12]


BUILD = _build_id()


def b2(v, default=False):
    """外部参数转布尔：'false' / '0' / 'no' 不能当 True（bool('false') 是 True）"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() not in ("", "0", "false", "no", "off", "none", "null")


def _delayed_exit():
    time.sleep(0.25)
    os._exit(0)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：一次请求一条连接。
    # 单线程服务 + HTTP/1.1 keep-alive 会出大问题：客户端不主动关连接时，
    # handler 就停在 readline 等下一个请求，整个服务不再 accept，
    # 而端口还在监听 → 壳的 portOpen 判"活着"，后续调用全排到 60s 超时。
    # 本机回环每请求多一次 TCP 握手，代价远小于被一条空闲连接堵死（2026-11-13 修）。
    protocol_version = "HTTP/1.0"
    timeout = 10  # 连上来却不发数据的连接最多挂 10s（单线程服务不能被一条闲连接长期占着）

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        if n > 8 * 1024 * 1024:      # 8MB 上限：正常请求都是几 KB
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _guard(self):
        """挡浏览器跨站请求。本服务没有认证，而网页用
        fetch(mode:'no-cors', content-type:'text/plain') 发的是 simple request——
        不触发预检，服务端照样收 JSON 并执行，等于打开一个恶意页面就交出了键鼠。
        浏览器总会带 Origin；本机进程（index.js / curl / 测试脚本）不会带。
        再要求 POST 必须是 application/json：text/plain 正是 simple request 能用的类型。"""
        origin = self.headers.get("Origin")
        if origin is not None:
            self._send(403, {"ok": False, "error": "FORBIDDEN_ORIGIN",
                             "detail": "本服务不接受浏览器跨站请求（Origin=%s）；本机程序调用不带 Origin" % origin})
            return False
        if self.command == "POST":
            ct = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ct:
                self._send(415, {"ok": False, "error": "UNSUPPORTED_MEDIA_TYPE",
                                 "detail": "POST 必须 Content-Type: application/json"})
                return False
        return True

    def do_GET(self):
        if self.path in ("/health", "/"):
            self._send(200, {"ok": True, "build": BUILD, "uptime": round(time.time() - START, 1), "port": PORT})
        else:
            self._send(404, {"ok": False, "error": "NOT_FOUND"})

    def do_POST(self):
        if not self._guard():
            return
        p = self.path
        b = self._body()
        try:
            if p == "/windows":
                self._send(200, {"ok": True, "windows": core.list_windows()})
            elif p == "/map":
                self._send(200, core.map_window(
                    b.get("title", ""), depth=b.get("depth", 6),
                    include_unnamed=b2(b.get("include_unnamed"), False),
                    max_items=b.get("max_items", 400)))
            elif p == "/click":
                self._send(200, core.click(
                    b.get("title", ""), b.get("target") or {},
                    via=b.get("via", "auto"), activate=b2(b.get("activate"), True),
                    wait=b.get("wait", 0.35),
                    x=b.get("x"), y=b.get("y"),
                    button=b.get("button", "left"),
                    double=b2(b.get("double"), False),
                    verify=b2(b.get("verify"), True),
                    template=b.get("template"),
                    threshold=b.get("threshold", 0.85)))
            elif p == "/set":
                self._send(200, core.set_value(
                    b.get("title", ""), b.get("target") or {}, b.get("value", ""),
                    activate=b2(b.get("activate"), True),
                    fallback_keys=b2(b.get("fallback_keys"), True)))
            elif p == "/get":
                self._send(200, core.get_value(b.get("title", ""), b.get("target") or {}))
            elif p == "/wait":
                self._send(200, core.wait_for(
                    b.get("title", ""), b.get("target"),
                    timeout=b.get("timeout", 10)))
            elif p == "/type":
                self._send(200, core.type_into(
                    b.get("title", ""), b.get("target"), b.get("text", ""),
                    activate=b2(b.get("activate"), True),
                    delay_ms=b.get("delay_ms", 4),
                    mode=b.get("mode", "unicode"),
                    enter=b2(b.get("enter"), False)))
            elif p == "/key":
                self._send(200, core.send_keys(
                    b.get("title", ""), b.get("keys") or b.get("key") or [],
                    activate=b2(b.get("activate"), True),
                    repeat=b.get("repeat", 1),
                    interval=b.get("interval", 0.15)))
            elif p == "/shot":
                self._send(200, core.shot(
                    b.get("title"), b.get("out_path"),
                    annotate=b2(b.get("annotate"), False),
                    region=b.get("region"),
                    grid=b.get("grid")))
            elif p == "/find":
                self._send(200, core.find_template(
                    b.get("template", ""),
                    threshold=b.get("threshold", 0.85),
                    region=b.get("region"),
                    title=b.get("title") or b.get("window")))
            elif p == "/hover":
                self._send(200, core.hover(
                    b.get("title", ""), b.get("target") or {},
                    x=b.get("x"), y=b.get("y"),
                    wait=b.get("wait", 0.6),
                    activate=b2(b.get("activate"), True),
                    detect_popup=b2(b.get("detect_popup"), True)))
            elif p == "/scroll":
                self._send(200, core.scroll(
                    b.get("title", ""), b.get("target") or {},
                    x=b.get("x"), y=b.get("y"),
                    direction=b.get("direction", "down"),
                    amount=b.get("amount", 3),
                    activate=b2(b.get("activate"), True),
                    verify=b2(b.get("verify"), True)))
            elif p == "/drag":
                self._send(200, core.drag(
                    b.get("title", ""),
                    frm=b.get("from") or {}, to=b.get("to") or {},
                    duration_ms=b.get("duration_ms", 400),
                    steps=b.get("steps", 16),
                    button=b.get("button", "left"),
                    activate=b2(b.get("activate"), True),
                    verify=b2(b.get("verify"), True)))
            elif p == "/calibrate":
                self._send(200, core.calibrate())
            elif p == "/health":
                self._send(200, {"ok": True, "build": BUILD, "uptime": round(time.time() - START, 1)})
            elif p == "/shutdown":
                # 先回执再退：壳看到 build 不一致会先调它，等端口释放后再拉新进程
                self._send(200, {"ok": True, "build": BUILD, "bye": True})
                threading.Thread(target=_delayed_exit, daemon=True).start()
            else:
                self._send(404, {"ok": False, "error": "NOT_FOUND", "path": p})
        except Exception as e:
            self._send(500, {"ok": False, "error": "SERVER_ERROR", "detail": repr(e),
                             "trace": traceback.format_exc()[-1200:]})


def main():
    # 单线程服务：请求都在主线程里跑，COM 交给 comtypes 自己初始化
    # （显式 ole32.CoInitialize 会让 uiautomation 后续调用卡死，实测 41s 无返回）
    try:
        srv = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print("[gui_server] 端口 %d 绑定失败：%r（多半是另一个进程占着；先关掉它，或改 cordis.patch.yml 的 port）"
              % (PORT, e), flush=True)
        sys.exit(3)
    print("gui_server listening on 127.0.0.1:%d build=%s" % (PORT, BUILD), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
