"""Register run.py as a background Windows service.

Usage:

    python service.py enable    # install + start, auto-start on boot
    python service.py disable   # stop + uninstall
    python service.py restart   # stop + start without reinstalling
    python service.py status    # show the current state
    python service.py console   # run the supervisor in the foreground (debug)

``enable`` and ``disable`` require administrator rights. When run from a
non-elevated terminal, the script asks for elevation (UAC) and reruns the same
command in a new administrator console, which stays open so the result is
visible.

The service itself is a thin supervisor: it launches ``run.py`` as a child
process, forwards its output to ``logs/service.log`` and restarts it if it
exits unexpectedly. Requires pywin32:

    python -m pip install pywin32
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from _win32typing import PySC_HANDLE

if sys.platform != "win32":
    raise SystemExit("service.py only supports Windows.")

try:
    import servicemanager
    import win32api
    import win32con
    import win32event
    import win32service
    import win32serviceutil
except ImportError as exc:  # pragma: no cover - depends on the host
    raise SystemExit(
        "pywin32 is required to register the Windows service:\n"
        f"  {sys.executable} -m pip install pywin32"
    ) from exc

ROOT = Path(__file__).resolve().parent
RUN_PY = ROOT / "run.py"
STATE_FILE = ROOT / "service.json"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "service.log"

SERVICE_NAME = "MCPIndex"
SERVICE_DISPLAY_NAME = "MCP Index Service"
SERVICE_DESCRIPTION = "Runs run.py (MCP Index) in the background as a Windows service."

RESTART_DELAY_SECONDS = 5
STOP_TIMEOUT_SECONDS = 5
STOP_WATCHDOG_SECONDS = 40
SERVICE_STOP_WAIT_SECONDS = 25.0
SERVICE_START_WAIT_SECONDS = 20.0
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_CANNOT_ACCEPT_CTRL = 1061
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_MARKED_FOR_DELETION = 1072

SERVICE_STATE_NAMES = {
    win32service.SERVICE_STOPPED: "stopped",
    win32service.SERVICE_START_PENDING: "start pending",
    win32service.SERVICE_STOP_PENDING: "stop pending",
    win32service.SERVICE_RUNNING: "running",
    win32service.SERVICE_CONTINUE_PENDING: "continue pending",
    win32service.SERVICE_PAUSE_PENDING: "pause pending",
    win32service.SERVICE_PAUSED: "paused",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _service_logger() -> logging.Logger:
    """Return the logger shared by the service host and its child output."""
    logger = logging.getLogger("mcp-service")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())

    return logger


def _add_console_output(logger: logging.Logger) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Child process management
# ---------------------------------------------------------------------------


def _write_state(python_exe: Path) -> None:
    payload = {"python": str(python_exe), "run": str(RUN_PY)}
    STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _remove_state() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def _resolve_python() -> Path:
    """Locate the interpreter that should run run.py.

    The service host is pythonservice.exe, so ``sys.executable`` is not a usable
    interpreter there. ``enable`` records the invoking interpreter in
    ``service.json``; the fallbacks keep ``console`` mode working standalone.
    """
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        recorded = Path(data.get("python", ""))
        if recorded.is_file():
            return recorded
    except (OSError, ValueError):
        pass

    for candidate in (Path(sys.executable), Path(sys.prefix) / "python.exe"):
        if candidate.name.lower() == "python.exe" and candidate.is_file():
            return candidate

    found = shutil.which("python")
    if found:
        return Path(found)

    raise RuntimeError(
        "Could not locate python.exe. Run 'python service.py enable' again."
    )


def _pump_output(proc: subprocess.Popen[str], logger: logging.Logger) -> None:
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        text = line.rstrip()
        if text:
            logger.info("[run.py] %s", text)
    proc.stdout.close()


def _bind_to_kill_on_close_job(
    proc: subprocess.Popen[str], logger: logging.Logger
) -> None:
    """Kill the child if the service host dies, so it never becomes an orphan."""
    try:
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        win32job.AssignProcessToJobObject(job, proc._handle)
    except Exception as exc:  # noqa: BLE001 - supervision works without it
        logger.warning("could not attach run.py to a job object: %s", exc)
        return

    # Keep the job handle alive for as long as the child is running; closing it
    # (when the Popen object is dropped) also kills any grandchildren.
    proc._job = job  # type: ignore[attr-defined]


def _spawn_child(logger: logging.Logger, hidden: bool = True) -> subprocess.Popen[str]:
    python_exe = _resolve_python()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if hidden:
        flags |= subprocess.CREATE_NO_WINDOW

    logger.info("starting %s %s", python_exe, RUN_PY)
    proc = subprocess.Popen(
        [str(python_exe), "-u", str(RUN_PY)],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    _bind_to_kill_on_close_job(proc, logger)
    threading.Thread(
        target=_pump_output,
        args=(proc, logger),
        name="run.py-output",
        daemon=True,
    ).start()
    logger.info("run.py started (pid=%s)", proc.pid)
    return proc


def _deliver_ctrl_break(proc: subprocess.Popen[str], shared_console: bool) -> bool:
    """Ask the child to shut down gracefully via CTRL_BREAK.

    A console-mode child shares our console, so the signal can be sent directly.
    A service-mode child gets its own hidden console (CREATE_NO_WINDOW), so we
    have to attach to that console first. CTRL_BREAK is handled by uvicorn and
    lets it close listeners and finish in-flight work.
    """
    if shared_console:
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            return False
        return True

    kernel32 = ctypes.windll.kernel32
    if not kernel32.AttachConsole(proc.pid):
        return False
    try:
        return bool(
            kernel32.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, proc.pid)
        )
    finally:
        kernel32.FreeConsole()


def _signal_child(
    proc: subprocess.Popen[str], shared_console: bool, logger: logging.Logger
) -> bool:
    """Deliver CTRL_BREAK without letting console APIs stall the service host."""
    outcome: dict[str, bool] = {"delivered": False}

    def _deliver() -> None:
        outcome["delivered"] = _deliver_ctrl_break(proc, shared_console)

    worker = threading.Thread(target=_deliver, name="run.py-stop", daemon=True)
    worker.start()
    worker.join(STOP_TIMEOUT_SECONDS)
    if worker.is_alive():
        logger.warning(
            "console signal to run.py did not return within %ss",
            STOP_TIMEOUT_SECONDS,
        )
        return False
    return outcome["delivered"]


def _stop_child(
    proc: subprocess.Popen[str],
    logger: logging.Logger,
    shared_console: bool = True,
) -> None:
    if proc.poll() is not None:
        logger.info("run.py already exited (code=%s)", proc.returncode)
        return

    logger.info("stopping run.py (pid=%s)", proc.pid)
    if _signal_child(proc, shared_console, logger):
        try:
            proc.wait(timeout=STOP_TIMEOUT_SECONDS)
            logger.info("run.py stopped")
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "run.py did not exit within %ss; terminating", STOP_TIMEOUT_SECONDS
            )
    else:
        logger.warning("could not signal run.py; terminating")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("run.py ignored terminate; killing it")
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Service definition
# ---------------------------------------------------------------------------


class MCPIndexService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._stopped_event = win32event.CreateEvent(None, 1, 0, None)
        self._logger = _service_logger()

    def SvcStop(self) -> None:
        self.ReportServiceStatus(
            win32service.SERVICE_STOP_PENDING,
            waitHint=(STOP_TIMEOUT_SECONDS + RESTART_DELAY_SECONDS + 10) * 1000,
        )
        self._logger.info("stop requested")
        win32event.SetEvent(self._stop_event)
        threading.Thread(
            target=self._stop_watchdog, name="stop-watchdog", daemon=True
        ).start()

    def _stop_watchdog(self) -> None:
        """Leave SERVICE_STOP_PENDING even if the supervisor thread is wedged.

        The SCM rejects ``stop``/``start``/``delete`` for the whole wait hint
        ("The service is starting or stopping. Please try again later."), so
        this keeps a stuck stop from wedging the service forever. run.py is
        attached to a kill-on-close job object, so exiting here also takes the
        child down with it.
        """
        if (
            win32event.WaitForSingleObject(
                self._stopped_event, int(STOP_WATCHDOG_SECONDS * 1000)
            )
            == win32event.WAIT_OBJECT_0
        ):
            return
        self._logger.error(
            "service did not stop within %ss; forcing exit", STOP_WATCHDOG_SECONDS
        )
        os._exit(1)

    def SvcDoRun(self) -> None:
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting")
        try:
            os.chdir(ROOT)
        except OSError:
            pass
        self._logger.info("service started (pid=%s)", os.getpid())
        try:
            self._supervise()
        finally:
            win32event.SetEvent(self._stopped_event)
            self._logger.info("service stopped")
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} stopped")

    def _stop_requested(self) -> bool:
        return (
            win32event.WaitForSingleObject(self._stop_event, 0)
            == win32event.WAIT_OBJECT_0
        )

    def _wait(self, seconds: float) -> None:
        win32event.WaitForSingleObject(self._stop_event, int(seconds * 1000))

    def _supervise(self) -> None:
        while True:
            if self._stop_requested():
                return

            try:
                child = _spawn_child(self._logger)
            except Exception:  # noqa: BLE001 - keep the service alive
                self._logger.exception("failed to start run.py")
                self._wait(RESTART_DELAY_SECONDS)
                continue

            while child.poll() is None and not self._stop_requested():
                self._wait(0.5)

            _stop_child(child, self._logger, shared_console=False)

            if self._stop_requested():
                return

            self._logger.warning(
                "run.py exited with code %s; restarting in %ss",
                child.returncode,
                RESTART_DELAY_SECONDS,
            )
            self._wait(RESTART_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------


def _has_admin_rights() -> bool:
    """Probe for the privilege the commands actually need.

    ``IsUserAnAdmin`` is deprecated and can disagree with the SCM about a
    filtered token, so ask the SCM for the access the commands require.
    """
    try:
        hscm = win32service.OpenSCManager(
            None, None, win32service.SC_MANAGER_CREATE_SERVICE
        )
    except win32service.error as exc:
        if exc.winerror == ERROR_ACCESS_DENIED:
            return False
        raise
    win32service.CloseServiceHandle(hscm)
    return True


def _require_admin(elevated: bool) -> None:
    if _has_admin_rights():
        return

    if elevated:
        raise SystemExit(
            "Administrator privileges are still not available after elevation.\n"
            "Open a terminal as Administrator and run the command again."
        )

    print("Administrator privileges are required; requesting elevation...")
    result = _elevate_and_rerun()
    if result <= 32:
        raise SystemExit(
            "Elevation was cancelled or failed "
            f"(ShellExecuteW returned {result}).\n"
            "Open a terminal as Administrator and run the command again."
        )

    print("Continuing in the new administrator console.")
    raise SystemExit(0)


def _elevation_command_line() -> str:
    script = str(Path(__file__).resolve())
    args = [arg for arg in sys.argv[1:] if arg != "--elevated"]
    return subprocess.list2cmdline([script, "--elevated", *args])


def _elevate_and_rerun() -> int:
    """Relaunch this script elevated and report the ShellExecuteW result."""
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_ssize_t
    return shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        _elevation_command_line(),
        str(ROOT),
        1,  # SW_SHOWNORMAL
    )


def _pause_before_close() -> None:
    """Keep the auto-elevated console open so the result stays readable."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    print()
    try:
        input("Press Enter to close this window...")
    except (EOFError, OSError):
        pass


def _open_service(access: int) -> PySC_HANDLE:
    """Open this service straight through the SCM.

    Deliberately avoids ``win32serviceutil.StopServiceWithDeps``: it walks
    ``HKLM\\SYSTEM\\CurrentControlSet\\Services`` and opens every subkey, so a
    single key with a restrictive ACL aborts the whole command with
    ``(5, 'RegOpenKeyEx', 'Access is denied')``.
    """
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        return win32service.OpenService(hscm, SERVICE_NAME, access)
    finally:
        win32service.CloseServiceHandle(hscm)


def _service_state() -> int | None:
    try:
        hs = _open_service(win32service.SERVICE_QUERY_STATUS)
    except win32service.error as exc:
        if exc.winerror == ERROR_SERVICE_DOES_NOT_EXIST:
            return None
        raise
    try:
        return win32service.QueryServiceStatus(hs)[1]
    finally:
        win32service.CloseServiceHandle(hs)


def _service_pid() -> int | None:
    try:
        hs = _open_service(win32service.SERVICE_QUERY_STATUS)
    except win32service.error:
        return None
    try:
        return _service_status_info(hs).get("ProcessId") or None
    finally:
        win32service.CloseServiceHandle(hs)


def _service_status_info(hs: PySC_HANDLE) -> dict[str, Any]:
    """QueryServiceStatusEx returns a dict at runtime, despite the stub type."""
    return cast(dict[str, Any], win32service.QueryServiceStatusEx(hs))


def _wait_for_state(hs: PySC_HANDLE, wanted: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if win32service.QueryServiceStatus(hs)[1] == wanted:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _wait_for_running(hs: PySC_HANDLE, timeout: float) -> bool:
    """Wait for SERVICE_RUNNING and bail out if the start already failed."""
    deadline = time.monotonic() + timeout
    time.sleep(0.5)  # give the SCM a moment to enter SERVICE_START_PENDING
    while True:
        state = win32service.QueryServiceStatus(hs)[1]
        if state == win32service.SERVICE_RUNNING:
            return True
        if state == win32service.SERVICE_STOPPED:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _terminate_service_host(hs: PySC_HANDLE) -> bool:
    """Force the service host down; the job object takes run.py with it."""
    pid = _service_status_info(hs).get("ProcessId") or 0
    if not pid:
        print(
            "The service host has no process id; cannot force it down.", file=sys.stderr
        )
        return False
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, 0, pid)
    except win32api.error as exc:
        print(f"Could not open service process {pid}: {exc}", file=sys.stderr)
        return False
    try:
        win32api.TerminateProcess(handle, 1)
        print(f"Terminated service host (pid {pid}).")
        return True
    except win32api.error as exc:
        print(f"Could not terminate service process {pid}: {exc}", file=sys.stderr)
        return False
    finally:
        win32api.CloseHandle(handle)


def _wait_until_removed(timeout: float = 10.0) -> bool:
    """Wait for the SCM to finish deleting the service."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _service_state() is None:
            return True
        time.sleep(0.5)
    return _service_state() is None


def _service_class_string() -> str:
    module = os.path.splitext(str(Path(__file__).resolve()))[0]
    return f"{module}.{MCPIndexService.__name__}"


def _print_log_tail(lines: int = 15) -> None:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        print(f"(no log yet: {LOG_FILE})", file=sys.stderr)
        return

    tail = content[-lines:]
    if tail:
        print("Recent log:", file=sys.stderr)
        for line in tail:
            print(f"  {line}", file=sys.stderr)


def _start_service(timeout: float = SERVICE_START_WAIT_SECONDS) -> bool:
    try:
        hs = _open_service(
            win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS
        )
    except win32service.error as exc:
        print(f"Could not open service {SERVICE_NAME}: {exc}", file=sys.stderr)
        return False
    try:
        try:
            win32service.StartService(hs, None)
        except win32service.error as exc:
            if exc.winerror != ERROR_SERVICE_ALREADY_RUNNING:
                print(f"Service failed to start: {exc}", file=sys.stderr)
                return False
        if _wait_for_running(hs, timeout):
            return True
        print(
            f"Service {SERVICE_NAME} did not reach the running state"
            f" within {timeout:.0f}s.",
            file=sys.stderr,
        )
        return False
    finally:
        win32service.CloseServiceHandle(hs)


def _stop_service(timeout: float = SERVICE_STOP_WAIT_SECONDS) -> bool:
    """Stop the service, force-terminating the host if it will not exit."""
    try:
        hs = _open_service(
            win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS
        )
    except win32service.error as exc:
        if exc.winerror in (ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_NOT_ACTIVE):
            return True
        print(f"Could not open service {SERVICE_NAME}: {exc}", file=sys.stderr)
        return False

    try:
        state = win32service.QueryServiceStatus(hs)[1]
        if state == win32service.SERVICE_STOPPED:
            return True

        if state == win32service.SERVICE_STOP_PENDING:
            print(
                f"Service {SERVICE_NAME} is already in SERVICE_STOP_PENDING;"
                f" waiting up to {timeout:.0f}s before forcing it down."
            )
        else:
            try:
                win32service.ControlService(hs, win32service.SERVICE_CONTROL_STOP)
            except win32service.error as exc:
                if exc.winerror not in (
                    ERROR_SERVICE_NOT_ACTIVE,
                    ERROR_SERVICE_CANNOT_ACCEPT_CTRL,
                ):
                    raise

        if _wait_for_state(hs, win32service.SERVICE_STOPPED, timeout):
            return True

        print(
            f"Service {SERVICE_NAME} did not stop within {timeout:.0f}s"
            " (it is most likely wedged in SERVICE_STOP_PENDING);"
            " forcing it down.",
            file=sys.stderr,
        )
        if not _terminate_service_host(hs):
            return False
        return _wait_for_state(hs, win32service.SERVICE_STOPPED, 10.0)
    finally:
        win32service.CloseServiceHandle(hs)


def _remove_service() -> None:
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        hs = win32service.OpenService(hscm, SERVICE_NAME, win32con.DELETE)
        try:
            win32service.DeleteService(hs)
        finally:
            win32service.CloseServiceHandle(hs)
    finally:
        win32service.CloseServiceHandle(hscm)

    try:
        import win32evtlogutil

        win32evtlogutil.RemoveSourceFromRegistry(SERVICE_NAME)
    except (ImportError, win32service.error):
        pass


def _configure_recovery() -> None:
    """Ask the SCM to restart the service host if it crashes."""
    result = subprocess.run(
        [
            "sc.exe",
            "failure",
            SERVICE_NAME,
            "reset=",
            "86400",
            "actions=",
            "restart/5000/restart/15000/restart/60000",
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        print("Warning: could not configure service recovery actions.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_enable(args: argparse.Namespace) -> int:
    _require_admin(args.elevated)

    if not RUN_PY.is_file():
        print(f"run.py not found: {RUN_PY}", file=sys.stderr)
        return 1

    python_exe = Path(sys.executable)
    _write_state(python_exe)
    print(f"Interpreter: {python_exe}")
    print(f"Script:      {RUN_PY}")

    if _service_state() is not None:
        print(f"Service {SERVICE_NAME} already exists; reinstalling...")
        if not _stop_service():
            print(
                "Could not stop the existing service; the reinstall was aborted.",
                file=sys.stderr,
            )
            return 1
        print("Removing service...")
        try:
            _remove_service()
        except win32service.error as exc:
            if exc.winerror == ERROR_SERVICE_MARKED_FOR_DELETION:
                print(
                    "The service is marked for deletion. Retry after the SCM"
                    " finishes removing it (a reboot may be required).",
                    file=sys.stderr,
                )
                return 1
            raise
        if not _wait_until_removed():
            print(
                "The service is still marked for deletion; retry in a moment.",
                file=sys.stderr,
            )
            return 1

    print(f"Installing service {SERVICE_NAME}...")
    win32serviceutil.InstallService(
        _service_class_string(),
        SERVICE_NAME,
        SERVICE_DISPLAY_NAME,
        startType=win32service.SERVICE_AUTO_START,
        description=SERVICE_DESCRIPTION,
    )
    _configure_recovery()

    print("Starting service...")
    if not _start_service():
        _print_log_tail()
        return 1

    print(
        f"Enabled: '{SERVICE_DISPLAY_NAME}' now runs run.py in the background"
        " and starts automatically on boot (as LocalSystem)."
    )
    print(f"Log: {LOG_FILE}")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    _require_admin(args.elevated)

    if _service_state() is None:
        print(f"Service {SERVICE_NAME} is not installed.")
        _remove_state()
        return 0

    print(f"Stopping service {SERVICE_NAME}...")
    if not _stop_service():
        print(
            f"Could not stop service {SERVICE_NAME}; it is still installed.",
            file=sys.stderr,
        )
        return 1

    print("Removing service...")
    try:
        _remove_service()
    except win32service.error as exc:
        if exc.winerror == ERROR_SERVICE_MARKED_FOR_DELETION:
            print(
                "The service is marked for deletion. Retry in a moment"
                " (a reboot may be required).",
                file=sys.stderr,
            )
            return 1
        raise

    _remove_state()
    print("Disabled: the service was removed and run.py no longer starts on boot.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    _require_admin(args.elevated)

    if _service_state() is None:
        print(
            f"Service {SERVICE_NAME} is not installed."
            " Run 'python service.py enable' first.",
            file=sys.stderr,
        )
        return 1

    print(f"Stopping service {SERVICE_NAME}...")
    if not _stop_service():
        print(f"Could not stop service {SERVICE_NAME}.", file=sys.stderr)
        return 1

    print("Starting service...")
    if not _start_service():
        _print_log_tail()
        return 1

    print(f"Restarted: '{SERVICE_DISPLAY_NAME}'")
    print(f"Log: {LOG_FILE}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    state = _service_state()
    if state is None:
        print(f"Service {SERVICE_NAME}: not installed")
        return 1

    print(f"Service {SERVICE_NAME}: {SERVICE_STATE_NAMES.get(state, state)}")
    pid = _service_pid()
    if pid:
        print(f"Process: {pid}")
    print(f"Script:  {RUN_PY}")
    print(f"Log:     {LOG_FILE}")
    return 0


def cmd_console(_args: argparse.Namespace) -> int:
    logger = _service_logger()
    _add_console_output(logger)
    logger.info("console mode started; press Ctrl+C to stop")

    try:
        child = _spawn_child(logger, hidden=False)
    except Exception as exc:  # noqa: BLE001 - report and exit
        logger.exception("failed to start run.py")
        print(f"Failed to start run.py: {exc}", file=sys.stderr)
        return 1

    try:
        returncode = child.wait()
    except KeyboardInterrupt:
        _stop_child(child, logger)
        returncode = 0

    logger.info("run.py exited with code %s", returncode)
    return 0 if returncode == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run mcp/run.py as a background Windows service.",
    )
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    enable = subparsers.add_parser(
        "enable", help="install and start the service (auto-start on boot)"
    )
    enable.set_defaults(func=cmd_enable)

    disable = subparsers.add_parser("disable", help="stop and remove the service")
    disable.set_defaults(func=cmd_disable)

    restart = subparsers.add_parser(
        "restart", help="stop and start the service without reinstalling"
    )
    restart.set_defaults(func=cmd_restart)

    status = subparsers.add_parser("status", help="show the service state")
    status.set_defaults(func=cmd_status)

    console = subparsers.add_parser(
        "console", help="run the supervisor in the foreground for debugging"
    )
    console.set_defaults(func=cmd_console)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except win32service.error as exc:
        if exc.winerror == ERROR_ACCESS_DENIED:
            print(
                "Access denied: run this command from an elevated terminal"
                " (or accept the UAC prompt it raises).",
                file=sys.stderr,
            )
        else:
            print(f"Windows service error: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.elevated:
            _pause_before_close()


if __name__ == "__main__":
    raise SystemExit(main())
