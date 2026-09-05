"""Sever Dub Studio Pro — Server & Desktop Launcher.
Start FastAPI server in background thread and launch PyWebView native window.
Automatically kills any stale process using port 15577 on startup.
"""
import os
import sys
import threading
import time

# ponytail: Global silent subprocess on Windows without breaking asyncio windows_utils inheritance
if sys.platform == 'win32':
    import asyncio  # Nạp asyncio trước để windows_utils thừa kế subprocess.Popen gốc
    import subprocess
    _orig_popen = subprocess.Popen
    class _SilentPopen(_orig_popen):
        def __init__(self, *args, **kwargs):
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            else:
                kwargs['creationflags'] |= 0x08000000
            super().__init__(*args, **kwargs)
    subprocess.Popen = _SilentPopen

    try:
        from asyncio import windows_utils
        _orig_win_popen_init = windows_utils.Popen.__init__
        def _silent_win_popen_init(self, *args, **kwargs):
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            else:
                kwargs['creationflags'] |= 0x08000000
            _orig_win_popen_init(self, *args, **kwargs)
        windows_utils.Popen.__init__ = _silent_win_popen_init
    except Exception:
        pass

    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost
        def _call_connection_lost_patched(self, exc):
            try:
                _orig_call_connection_lost(self, exc)
            except ConnectionResetError as e:
                if getattr(e, 'winerror', None) != 10054:
                    raise
        _ProactorBasePipeTransport._call_connection_lost = _call_connection_lost_patched
    except ImportError:
        pass

HOST = "127.0.0.1"
PORT = 15577
URL = f"http://{HOST}:{PORT}"


_SERVER_ERROR = None
_MUTEX_HANDLE = None


def _acquire_single_instance_mutex(mutex_name: str = "SeverDubStudioPro_SingleInstance_Mutex") -> bool:
    """Tạo Named Mutex trên Windows để đảm bảo chỉ có 1 instance ứng dụng chạy."""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, mutex_name)
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            return False
    except Exception:
        pass
    return True


def _show_error_box(title: str, message: str) -> None:
    """Hiển thị hộp thoại lỗi Windows khi app gặp sự cố nghiêm trọng."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10 | 0x0)  # MB_ICONERROR | MB_OK
            return
        except Exception:
            pass
    print(f"[{title}] {message}", file=sys.stderr)


def _suppress_stdio():
    """Suppress stdio ONLY when running frozen/packaged EXE without console."""
    if not getattr(sys, 'frozen', False):
        return
    class _Null:
        def write(self, _): pass
        def flush(self): pass
        def isatty(self): return False
    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()


def _kill_stale_process_on_port(port):
    """Forcefully kill any stale process listening on port 15577."""
    if sys.platform == 'win32':
        try:
            import subprocess
            _flags = 0x08000000  # CREATE_NO_WINDOW
            out = subprocess.check_output(
                f"netstat -ano | findstr :{port}", shell=True,
                creationflags=_flags,
            ).decode()
            curr_pid = str(os.getpid())
            for line in out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in parts[3]:
                    pid = parts[4]
                    if pid != "0" and pid != curr_pid:
                        subprocess.call(
                            f"taskkill /F /PID {pid}", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=_flags,
                        )
            time.sleep(0.6)
        except Exception:
            pass


def _screen_size():
    """Lay kich thuoc man hinh (giu titlebar he thong de dong/minimize)."""
    if sys.platform == 'win32':
        try:
            import ctypes
            user32 = ctypes.windll.user32
            return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:
            pass
    return 1340, 880


def _run_server():
    """Run uvicorn server in background thread."""
    global _SERVER_ERROR
    try:
        from core.config import ensure_env_paths
        ensure_env_paths()

        import uvicorn
        from app import app

        _kill_stale_process_on_port(PORT)

        try:
            uvicorn.run(app, host=HOST, port=PORT, log_level="warning", log_config=None)
        except OSError as e:
            if "address already in use" in str(e).lower() or "10048" in str(e) or "10013" in str(e):
                _kill_stale_process_on_port(PORT)
                try:
                    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", log_config=None)
                except Exception as exc:
                    _SERVER_ERROR = str(exc)
            else:
                _SERVER_ERROR = str(e)
    except Exception as exc:
        _SERVER_ERROR = str(exc)
        import logging
        logging.getLogger("sever").exception("Server failed to start: %s", exc)


def _setup_file_logging():
    """Ghi toan bo log vao file sever.log (de debug EXE khong console)."""
    try:
        import logging
        from logging.handlers import RotatingFileHandler
        from core.config import app_dir
        log_file = app_dir() / "sever.log"
        handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception:
        pass


def main():
    if not _acquire_single_instance_mutex():
        _show_error_box(
            "Ứng dụng đang chạy",
            "Sever Dub Studio Pro đã được khởi chạy trên máy tính của bạn.\n"
            "Vui lòng kiểm tra trên thanh Taskbar hoặc cửa sổ đang mở."
        )
        return

    _suppress_stdio()
    _setup_file_logging()

    # Nạp PATH môi trường ngay từ đầu
    try:
        from core.config import ensure_env_paths
        ensure_env_paths()
    except Exception:
        pass

    # Start FastAPI server thread
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    time.sleep(1.0)

    if _SERVER_ERROR:
        _show_error_box(
            "Lỗi khởi động máy chủ",
            f"Không thể khởi chạy dịch vụ backend:\n{_SERVER_ERROR}\n\n"
            f"Vui lòng kiểm tra file log tại:\n%APPDATA%\\SeverDesktop\\sever.log\n"
            f"Và đảm bảo máy đã cài đặt Microsoft Visual C++ Redistributable x64."
        )
        return

    # Try PyWebView Desktop Window
    try:
        import webview
        sw, sh = _screen_size()
        webview.create_window(
            title="Sever Dub Studio Pro",
            url=URL,
            width=sw,
            height=sh,
            min_size=(1024, 700),
            resizable=True,
            background_color="#0b0f17",
        )
        webview.start()
    except Exception as err:
        import webbrowser
        try:
            webbrowser.open(URL)
            server_thread.join()
        except Exception as web_err:
            _show_error_box(
                "Lỗi giao diện",
                f"Không thể mở cửa sổ ứng dụng hoặc trình duyệt:\n{err}\n{web_err}\n\n"
                f"Vui lòng cài đặt Microsoft Edge WebView2 Evergreen Runtime."
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as fatal_err:
        _show_error_box(
            "Lỗi nghiêm trọng",
            f"Ứng dụng gặp sự cố không thể tiếp tục:\n{fatal_err}\n\n"
            f"Vui lòng đảm bảo đã cài đặt Microsoft Visual C++ Redistributable (x64)."
        )
