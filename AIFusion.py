"""AI Fusion – AI Copilot add-in entry point.

Architecture
------------
The local Flask server runs inside Fusion's Python process in a daemon thread.
Flask + requests are auto-installed via pip on first run.
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback

import adsk.core
import adsk.fusion

from .bridge import palette as palette_bridge
from .bridge import selection_watcher

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PALETTE_ID = "aifusionPalette"
_PALETTE_NAME = "AI Fusion"
_COMMAND_ID = "aifusionShowPaletteCmd"
_COMMAND_NAME = "AI Fusion – AI Copilot"
_COMMAND_TOOLTIP = "Open the AI Fusion – AI Copilot palette"
_TARGET_PANEL_ID = "SolidScriptsAddinsPanel"
_SERVER_PORT = 8765
_SERVER_URL = f"http://127.0.0.1:{_SERVER_PORT}/chat"
_PALETTE_URL = os.environ.get("AIFUSION_CHAT_URL") or _SERVER_URL
_MAX_LOG_BYTES = 500_000  # rotate debug log at ~500 KB

_handlers: list = []
_server_thread: threading.Thread | None = None
_httpd: object | None = None  # werkzeug BaseWSGIServer（供 _stop_server 真正关闭）

# =========================================================================
# Logging (with rotation)
# =========================================================================

_LOG_FILE: str | None = None


def _log_path() -> str:
    global _LOG_FILE
    if _LOG_FILE is None:
        _LOG_FILE = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "aifusion_debug.log",
        )
    return _LOG_FILE


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"[aifusion] {msg}")
    try:
        path = _log_path()
        # Rotate if too large
        if os.path.isfile(path) and os.path.getsize(path) > _MAX_LOG_BYTES:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                # Keep last 200 KB
                fh.seek(os.path.getsize(path) - 200_000)
                tail = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"[ROTATED at {ts}] — trimmed to last ~200 KB\n")
                fh.write(tail)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def show_error(title: str, detail: str) -> None:
    log(f"ERROR: {title} — {detail}")
    try:
        ui = adsk.core.Application.get().userInterface
        if ui:
            ui.messageBox(
                f"{title}\n\n{detail}\n\nLog: {_log_path()}",
                "AI Fusion",
            )
    except Exception:
        pass


# =========================================================================
# Dependency management
# =========================================================================

_REQUIRED_MODULES = ["flask", "requests"]


def _add_real_python_site_paths(python_exe: str) -> None:
    """把真实 Python 的 site-packages 注入当前进程 sys.path。

    pip 安装的模块默认进入该 Python 的 site-packages;Fusion 嵌入式
    Python 并不共享该路径,必须显式加入 sys.path 才能 import。
    """
    try:
        r = subprocess.run(
            [python_exe, "-c",
             "import site,json;"
             "print(json.dumps(site.getsitepackages()));"
             "print(json.dumps(site.getusersitepackages()))"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return
        for line in (ln.strip() for ln in r.stdout.splitlines() if ln.strip()):
            try:
                paths = json.loads(line)
            except Exception:
                continue
            if isinstance(paths, str):
                paths = [paths]
            for p in paths:
                if os.path.isdir(p) and p not in sys.path:
                    sys.path.insert(0, p)
                    log(f"site-packages added to path: {p}")
    except Exception as exc:
        log(f"site-path detect failed: {exc}")


def _ensure_dependencies() -> bool:
    # 先尝试把真实 Python 的 site-packages 挂进当前进程,
    # 复用已有安装(如 install_deps.bat 或历史 pip 安装过的模块)。
    python_exe = _find_real_python()
    if python_exe:
        _add_real_python_site_paths(python_exe)

    missing = [m for m in _REQUIRED_MODULES if not _try_import(m)]
    if not missing:
        return True

    log(f"Missing: {', '.join(missing)} — auto-installing …")
    if not python_exe:
        show_error("Python Not Found",
            "Run: pip install flask requests\nThen restart the add-in.")
        return False

    for attempt in range(2):
        try:
            result = subprocess.run(
                [python_exe, "-m", "pip", "install", "--quiet"] + missing,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                _add_real_python_site_paths(python_exe)
                # 装完必须在本进程重新验证——pip 装到真实 Python,
                # Fusion 嵌入式解释器要靠注入的 site-packages 才能 import
                still_missing = [m for m in _REQUIRED_MODULES if not _try_import(m)]
                if not still_missing:
                    log(f"Installed: {', '.join(missing)}")
                    return True
                log(f"pip installed into {python_exe}, but Fusion interpreter still "
                    f"cannot import: {', '.join(still_missing)}")
                return False

            if "no module named pip" in (result.stderr or "").lower() and attempt == 0:
                log("Bootstrap pip via ensurepip …")
                r = subprocess.run([python_exe, "-c", "import ensurepip; ensurepip.bootstrap()"],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    log("ensurepip OK — retrying …")
                    continue
                log(f"ensurepip failed: {r.stderr.strip()}")
                break

            log(f"pip install FAILED: {result.stderr.strip()}")
            return False
        except subprocess.TimeoutExpired:
            log("pip install timed out.")
            return False
        except Exception as exc:
            log(f"pip exception: {exc}")
            return False
    return False


def _try_import(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _find_real_python() -> str | None:
    for name in ("python3", "python"):
        try:
            r = subprocess.run([name, "-c", "import sys; print(sys.version)"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                r2 = subprocess.run([name, "-m", "pip", "--version"],
                                    capture_output=True, text=True, timeout=5)
                if r2.returncode == 0:
                    log(f"Found Python: {name}")
                    return name
        except Exception:
            pass

    if sys.executable:
        try:
            r = subprocess.run([sys.executable, "-c", "print(1)"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return sys.executable
        except Exception:
            pass

    webdeploy = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Autodesk", "webdeploy", "production")
    if os.path.isdir(webdeploy):
        for d in sorted(os.listdir(webdeploy), reverse=True):
            py = os.path.join(webdeploy, d, "Python", "python.exe")
            if os.path.isfile(py):
                return py
    return None


# =========================================================================
# Server lifecycle
# =========================================================================

def _server_running() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{_SERVER_PORT}/api/models", method="GET"), timeout=1)
        return True
    except Exception:
        return False


def _server_has_reliability() -> bool:
    """探测当前端口上的服务器是否已加载可靠性层（区分新旧实例）。"""
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{_SERVER_PORT}/api/reliability/ping", method="GET"),
            timeout=1)
        return True
    except Exception:
        return False


def _start_server() -> bool:
    global _server_thread
    if _server_running():
        if not _server_has_reliability():
            log("端口 8765 已被【旧版本】服务器占用（无 reliability 路由）。"
                "插件 stop 不杀 daemon 线程，请完全退出 Fusion 再重启以加载新代码。")
        else:
            log("Server already running (reliability ready).")
        return True

    if not _ensure_dependencies():
        return False

    addin_root = os.path.dirname(os.path.abspath(__file__))
    if addin_root not in sys.path:
        sys.path.insert(0, addin_root)

    try:
        from local_server.server import create_app
        app = create_app()
    except Exception:
        show_error("Server Import Error", traceback.format_exc())
        return False

    def _run():
        global _httpd
        try:
            import logging as _logging
            _log = _logging.getLogger("werkzeug")
            _log.setLevel(_logging.ERROR)  # suppress GET/POST logs
            from werkzeug.serving import make_server
            _httpd = make_server("127.0.0.1", _SERVER_PORT, app,
                                 threaded=True)  # LLM 长请求期间不阻塞其他请求
            _httpd.serve_forever()
        except Exception as exc:
            log(f"Flask stopped: {exc}")

    _server_thread = threading.Thread(target=_run, daemon=True, name="AIFusionServer")
    _server_thread.start()

    for _ in range(100):  # up to 20 s
        time.sleep(0.2)
        if _server_running():
            log("Server ready.")
            return True
    log("Server NOT ready after 20 s.")
    return False


def _stop_server() -> None:
    global _server_thread, _httpd
    if _httpd is not None:
        try:
            _httpd.shutdown()
        except Exception as exc:
            log(f"httpd shutdown error: {exc}")
        _httpd = None
    _server_thread = None


# =========================================================================
# Palette & Toolbar
# =========================================================================

def _show_palette(ui: adsk.core.UserInterface) -> None:
    pal = ui.palettes.itemById(_PALETTE_ID)
    if not pal:
        pal = ui.palettes.add(
            _PALETTE_ID, _PALETTE_NAME, _PALETTE_URL,
            True, True, True, 400, 720, True,
        )
        pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        palette_bridge.attach(pal, _handlers)
        selection_watcher.attach(pal, _handlers)
    pal.isVisible = True


class _ShowPaletteCmd(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            _show_palette(adsk.core.Application.get().userInterface)
        except Exception:
            ui = adsk.core.Application.get().userInterface
            if ui:
                ui.messageBox("Failed to open AI Fusion palette:\n" + traceback.format_exc())


def _register_command(ui: adsk.core.UserInterface) -> None:
    existing = ui.commandDefinitions.itemById(_COMMAND_ID)
    if existing:
        existing.deleteMe()
    cmd = ui.commandDefinitions.addButtonDefinition(_COMMAND_ID, _COMMAND_NAME, _COMMAND_TOOLTIP)
    handler = _ShowPaletteCmd()
    cmd.commandCreated.add(handler)
    _handlers.append(handler)
    panel = ui.allToolbarPanels.itemById(_TARGET_PANEL_ID)
    if panel:
        ctrl = panel.controls.itemById(_COMMAND_ID)
        if ctrl:
            ctrl.deleteMe()
        panel.controls.addCommand(cmd)


def _unregister_command(ui: adsk.core.UserInterface) -> None:
    panel = ui.allToolbarPanels.itemById(_TARGET_PANEL_ID)
    if panel:
        ctrl = panel.controls.itemById(_COMMAND_ID)
        if ctrl:
            try:
                ctrl.deleteMe()
            except Exception:
                pass
    cmd = ui.commandDefinitions.itemById(_COMMAND_ID)
    if cmd:
        try:
            cmd.deleteMe()
        except Exception:
            pass


# =========================================================================
# Add-in lifecycle
# =========================================================================

def run(_context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        log("=" * 50)
        log("AI Fusion starting …")

        server_ok = _start_server()

        if not server_ok:
            ui.messageBox(
                "AI Fusion server could not start.\n"
                f"Check: {_log_path()}",
                "AI Fusion – Error",
            )
            return

        existing = ui.palettes.itemById(_PALETTE_ID)
        if existing:
            existing.deleteMe()

        pal = ui.palettes.add(
            _PALETTE_ID, _PALETTE_NAME, _PALETTE_URL,
            True, True, True, 400, 720, True,
        )
        pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
        pal.isVisible = True
        palette_bridge.attach(pal, _handlers)
        selection_watcher.attach(pal, _handlers)
        _register_command(ui)

        log("Add-in started.")
    except Exception:
        show_error("Startup Error", traceback.format_exc())


def stop(_context):
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        log("Shutting down …")

        for h in _handlers:
            detach = getattr(h, "detach", None)
            if callable(detach):
                try:
                    detach()
                except Exception:
                    pass

        _unregister_command(ui)
        pal = ui.palettes.itemById(_PALETTE_ID)
        if pal:
            pal.deleteMe()
        _handlers.clear()
        _stop_server()
        log("Stopped.")
    except Exception:
        ui = adsk.core.Application.get().userInterface
        if ui:
            ui.messageBox("AI Fusion failed to stop:\n" + traceback.format_exc())
