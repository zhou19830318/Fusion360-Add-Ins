"""AI Fusion – AI Copilot add-in entry point.

Architecture
------------
The local Flask server runs inside Fusion's Python process in a daemon thread.
Flask + requests are auto-installed via pip on first run.
"""

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


def _ensure_dependencies() -> bool:
    missing = [m for m in _REQUIRED_MODULES if not _try_import(m)]
    if not missing:
        return True

    log(f"Missing: {', '.join(missing)} — auto-installing …")
    python_exe = _find_real_python()
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
                log(f"Installed: {', '.join(missing)}")
                return True

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


def _start_server() -> bool:
    global _server_thread
    if _server_running():
        log("Server already running.")
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
        try:
            import logging as _logging
            _log = _logging.getLogger("werkzeug")
            _log.setLevel(_logging.ERROR)  # suppress GET/POST logs
            app.run(host="127.0.0.1", port=_SERVER_PORT, debug=False, use_reloader=False)
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
    global _server_thread
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
