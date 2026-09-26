# -*- coding: utf-8 -*-
"""港口的壳（2026.9.21 任务指令·壳与UI规划）：双击她出现。
本地模式（搬家前，默认）：8000 空闲 → 用 .venv 起 server 子进程 + 开窗（关窗=服务停）；
                          8000 已有她在跑（比如先开了 启动她.bat）→ 不双开，直接开窗复用，关窗只关窗。
云端模式（搬家后）：改旁边 port_app.json {"mode":"cloud","url":"https://..."}，
                   只开窗指向 Tailscale serve 的 HTTPS，不启服务。不用重打包。
壳不打包 server/fastapi/fastembed——exe 放 soulhome 目录里跑，代码和数据都用文件夹里的，
改 server.py 重启 exe 即生效。跑法（开发态）：.venv\\Scripts\\python.exe port_app.py
2026.9.23：关窗弹窗（隐藏到托盘 / 彻底退出）+ 托盘常驻（pystray）——像常用软件那样：
隐藏=窗口消失、服务照跑（书记员/主动消息不停），右下角托盘港口图标双击唤回、右键彻底退出。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# exe 所在目录（onefile 解压目录不是这里，显式指回 soulhome）
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_PATH = APP_DIR / "port_app.json"
LOG_PATH = APP_DIR / "port_app.log"
PORT = 8000
HOME_URL = f"http://127.0.0.1:{PORT}"
CREATE_NO_WINDOW = 0x08000000


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return {"mode": "local"}   # 没写配置文件=本地模式


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def looks_like_her(url: str) -> bool:
    """8000 被占时确认占的是她（/api/scribe/status 免门锁，答 200 就是她；
    /api/jiwen 会被门锁 401，不能用）。"""
    try:
        with urllib.request.urlopen(url + "/api/scribe/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_server():
    venv_py = APP_DIR / ".venv" / "Scripts" / "python.exe"
    if not venv_py.exists():
        print(f"[壳] 找不到 {venv_py}——exe 必须放在 soulhome 目录里跑", file=sys.stderr)
        sys.exit(1)
    log = open(LOG_PATH, "ab")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 壳启动 =====\n".encode("utf-8"))
    # 生产实例：SCRIBE/PROACTIVE 正常开（不设 DISABLE——那是 8001 测试实例的规矩）
    env = {**os.environ, "PYTHONUTF8": "1"}   # uvicorn 日志写文件别用 GBK
    return subprocess.Popen(
        [str(venv_py), "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(APP_DIR), stdout=log, stderr=subprocess.STDOUT,
        env=env, creationflags=CREATE_NO_WINDOW)


def wait_port(port: int, timeout: float = 25.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_listening(port):
            return True
        time.sleep(0.3)
    return False


def log_line(msg: str) -> None:
    """壳的关键动作也落 port_app.log（exe 没有控制台，print 看不见）。"""
    try:
        with open(LOG_PATH, "ab") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n".encode("utf-8"))
    except Exception:
        pass


def ask_close_choice() -> str:
    """关窗时问一句（2026.9.23；2026.9.24b 重做）：是=隐藏到托盘（服务继续）/ 否=彻底退出 / 取消=留下。
    第一版用 tkinter 在副线程画自定义按钮弹窗——实测点击收不到：关窗事件占着 UI 线程，
    主线程堵在等结果上不泵消息，tk 窗口成了死脸（9.24 14:16 现场，日志里零条关窗选择）。
    换 Windows 原生 MessageBoxW：它自带消息泵、就在调用线程上模态运行，怎么点都灵；
    按钮文案是系统的 是/否/取消，含义写在正文里。"""
    import ctypes
    IDYES, IDNO = 6, 7
    MB = 0x3 | 0x20 | 0x1000   # YESNOCANCEL | ICONQUESTION | SYSTEMMODAL(置顶)
    r = ctypes.windll.user32.MessageBoxW(
        0,
        "关掉窗口后，她还继续在后台吗？\n\n"
        "【是】隐藏到托盘——服务照跑（书记员/主动消息不停），右下角图标双击唤回\n"
        "【否】彻底退出\n"
        "【取消】留在原地，什么都不动",
        "港口",
        MB)
    if r == IDYES:
        return "hide"
    if r == IDNO:
        return "exit"
    return "stay"


_quitting = False          # 托盘"彻底退出"放的行，closing 处理器看见就不再弹窗
_tray = {"icon": None, "thread": None}


def start_tray(window) -> None:
    """托盘常驻（2026.9.23）：港口小图标——双击=唤回窗口，右键=打开港口/彻底退出。
    pystray 自己的线程里跑；重复隐藏不重复起。装不上 pystray 时退化为纯隐藏
    （再双击 exe 也能唤回，只是没图标）。"""
    if _tray.get("thread"):
        return
    try:
        import pystray
        from PIL import Image
    except Exception as e:
        log_line(f"托盘起不来（{type(e).__name__}）——隐藏后可再双击 exe 唤回")
        return

    def open_window(icon, item):
        window.show()
        try:
            window.restore()
        except Exception:
            pass

    def quit_app(icon, item):
        global _quitting
        _quitting = True
        try:
            icon.stop()
        except Exception:
            pass
        window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("打开港口", open_window, default=True),
        pystray.MenuItem("彻底退出", quit_app),
    )
    try:
        img = Image.open(str(APP_DIR / "港口图标.ico"))
    except Exception as e:
        log_line(f"托盘图标读不了（{type(e).__name__}）——用空图标顶着")
        img = Image.new("RGBA", (64, 64), (184, 150, 63, 255))
    import threading
    icon = pystray.Icon("soulhome", img, "港口", menu)
    _tray["icon"] = icon
    t = threading.Thread(target=icon.run, daemon=True)
    _tray["thread"] = t
    t.start()


def main():
    cfg = load_config()
    server_proc = None
    url = HOME_URL

    if cfg.get("mode") == "cloud" and cfg.get("url"):
        url = cfg["url"]                       # 云端模式：只开窗
    else:
        if port_listening(PORT):
            if looks_like_her(HOME_URL):
                print("[壳] 8000 已经是她在跑（启动她.bat？）——直接开窗，不双开")
            else:
                print(f"[壳] 8000 被别的程序占了，开窗会显示错误页")
        else:
            server_proc = start_server()
            if not wait_port(PORT):
                print(f"[壳] 服务 25 秒没起来，看 {LOG_PATH.name} 排查；窗口照开")
            else:
                print("[壳] 她已就绪")

    import webview
    window = webview.create_window("港口", url, width=1280, height=820,
                                   min_size=(960, 640), background_color="#f5efe0")

    def on_closing():
        # pywebview closing 事件的返回值语义（2026.9.24c 复现定罪，此前两版都写反了）：
        # handler 返回 **False = 拦下关闭**（args.Cancel=True）；返回 True/None = 放行关闭。
        # 见 webview/event.py：false_values=[v for v in return_values if v is False] → set() 返回 True → 取消。
        global _quitting
        if _quitting:
            return True           # 托盘"彻底退出"的 destroy() 触发的，放行
        choice = ask_close_choice()
        if choice == "hide":
            log_line("关窗选择：隐藏到托盘（服务继续）")
            window.hide()
            start_tray(window)
            return False          # 拦下关闭：窗口转为托盘
        if choice == "exit":
            log_line("关窗选择：彻底退出")
            _quitting = True
            return True           # 放行关闭 → start() 返回 → 收尾
        log_line("关窗选择：取消（留在原地）")
        return False              # 拦下关闭：原地不动

    window.events.closing += on_closing
    # private_mode=False：WebView2 的 cookie/localStorage 落到固定目录持久化——
    # 门锁通行证和夜间模式才记得住（默认隐私模式关窗即焚，每次都要重新输码，2026.9.21 修）
    webview.start(private_mode=False,
                  storage_path=os.path.join(os.environ.get("LOCALAPPDATA", str(APP_DIR)), "soulhome_webview"))

    # start() 走到这=所有窗口真关了（直接退出或经托盘"彻底退出"）→ 收尾
    if _tray.get("icon"):
        try:
            _tray["icon"].stop()
        except Exception:
            pass
    if server_proc:                            # 复用模式下没起子进程，不动 bat 的服务
        server_proc.terminate()
        try:
            server_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        log_line("服务已随彻底退出而停")
        print("[壳] 服务已随窗口关闭")
    # 硬收尾（2026.9.24c）：不给 pywebview/CLR 内部线程留悬挂机会——9.24 两次现场都是
    # 进程赖着不死变僵尸；正常路径本来也会退，这行只是保险丝。
    os._exit(0)


if __name__ == "__main__":
    main()
