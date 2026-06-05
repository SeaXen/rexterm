from __future__ import annotations

import json
import mimetypes
import os
import base64
import hmac
import secrets
from http.cookies import SimpleCookie
import fcntl
import hashlib
import select
import shlex
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
import shutil
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("REXTERM_BACKEND_PORT", "8080"))
TOKEN = os.environ.get("REXTERM_SHARED_TOKEN", "change-me")
AUTH_USERNAME = os.environ.get("REXTERM_AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("REXTERM_AUTH_PASSWORD", "")
AUTH_PASSWORD_SHA256 = os.environ.get("REXTERM_AUTH_PASSWORD_SHA256", "")
AUTH_REQUIRED = os.environ.get("REXTERM_AUTH_REQUIRED", "1").lower() not in {"0", "false", "no", "off"}
AUTH_SESSION_COOKIE = "rexterm_session"
AUTH_SESSION_TTL = int(os.environ.get("REXTERM_AUTH_SESSION_TTL", "604800"))
DATA_DIR = os.environ.get("REXTERM_DATA_DIR", "/data")
STATIC_DIR = os.environ.get("REXTERM_STATIC_DIR", "/app/static")
SHELL = os.environ.get("REXTERM_SHELL", "/bin/bash")
HISTORY_LIMIT = int(os.environ.get("REXTERM_HISTORY_LIMIT", "300"))


def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)


def tmux_socket_path() -> str:
    return os.environ.get("REXTERM_TMUX_SOCKET", data_path("tmux", "rexterm.sock"))


def resolve_tmux_binary() -> str:
    explicit = os.environ.get("REXTERM_TMUX_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("tmux")
    if found:
        return found
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(app_root, "vendor-rootfs-tmux", "usr", "bin", "tmux"),
        os.path.join(app_root, "vendor", "tmux", "tmux"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "tmux"


def tmux_env() -> dict[str, str]:
    env = os.environ.copy()
    tmux_bin = resolve_tmux_binary()
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tmux_bin.startswith(app_root):
        lib_candidates = [
            os.path.join(app_root, "vendor-rootfs-tmux", "usr", "lib", "x86_64-linux-gnu"),
            os.path.join(app_root, "vendor-rootfs-tmux", "lib", "x86_64-linux-gnu"),
        ]
        existing = [part for part in str(env.get("LD_LIBRARY_PATH") or "").split(":") if part]
        for lib_dir in reversed(lib_candidates):
            if os.path.isdir(lib_dir) and lib_dir not in existing:
                existing.insert(0, lib_dir)
        if existing:
            env["LD_LIBRARY_PATH"] = ":".join(existing)
    return env


def tmux_base_cmd() -> list[str]:
    return [resolve_tmux_binary(), "-S", tmux_socket_path()]


def tmux_run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    cmd = tmux_base_cmd() + list(args)
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=tmux_env())


def session_meta_path(session_id: str) -> str:
    return data_path("sessions", f"{sanitize_session_id(session_id)}.json")


def load_session_meta(session_id: str) -> dict[str, Any]:
    raw = load_json(session_meta_path(session_id), {})
    return raw if isinstance(raw, dict) else {}


def save_session_meta(session_id: str, label: str, created_at: float | None = None) -> dict[str, Any]:
    payload = {
        "session_id": sanitize_session_id(session_id),
        "label": (label or session_id).strip()[:64],
        "created_at": float(created_at or now_ts()),
    }
    write_json(session_meta_path(session_id), payload)
    return payload


def delete_session_meta(session_id: str) -> None:
    try:
        os.remove(session_meta_path(session_id))
    except FileNotFoundError:
        pass


def rename_session_meta(old_session_id: str, new_session_id: str, label: str | None = None) -> dict[str, Any]:
    old_meta = load_session_meta(old_session_id)
    created_at = float(old_meta.get("created_at") or now_ts())
    payload = save_session_meta(new_session_id, label or str(old_meta.get("label") or new_session_id), created_at=created_at)
    if sanitize_session_id(old_session_id) != sanitize_session_id(new_session_id):
        delete_session_meta(old_session_id)
    return payload


def tmux_session_exists(session_id: str) -> bool:
    clean_id = sanitize_session_id(session_id)
    result = tmux_run("has-session", "-t", clean_id)
    return result.returncode == 0


def tmux_list_sessions() -> list[dict[str, Any]]:
    result = tmux_run("list-sessions", "-F", "#{session_name}\t#{session_created}")
    if result.returncode != 0:
        return []
    sessions: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, created = line.partition("\t")
        session_id = sanitize_session_id(name)
        meta = load_session_meta(session_id)
        created_at = float(meta.get("created_at") or (float(created) if created.strip().isdigit() else now_ts()))
        label = str(meta.get("label") or session_id)[:64]
        sessions.append({"id": session_id, "label": label, "created_at": created_at})
    return sessions


def tmux_capture(session_id: str, lines: int = 400) -> str:
    clean_id = sanitize_session_id(session_id)
    result = tmux_run("capture-pane", "-p", "-S", f"-{max(50, min(lines, 5000))}", "-t", clean_id)
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def tmux_shell_command() -> str:
    env_parts = [
        "env",
        "TERM=xterm-256color",
        "HOSTNAME=DietPi",
        "PROMPT_COMMAND=",
        shlex.quote("PS1=root@DietPi: "),
        shlex.quote(SHELL),
        "--noprofile",
        "--norc",
        "-i",
    ]
    return " ".join(env_parts)


def ensure_tmux_session(session_id: str, label: str | None = None) -> dict[str, Any]:
    clean_id = sanitize_session_id(session_id)
    meta = load_session_meta(clean_id)
    created_at = float(meta.get("created_at") or now_ts())
    final_label = (label or str(meta.get("label") or clean_id)).strip()[:64]
    if not tmux_session_exists(clean_id):
        os.makedirs(os.path.dirname(tmux_socket_path()), exist_ok=True)
        tmux_run("new-session", "-d", "-s", clean_id, "-c", DATA_DIR, tmux_shell_command(), check=True)
        tmux_run("set-option", "-t", clean_id, "status", "off")
        tmux_run("set-option", "-t", clean_id, "allow-rename", "off")
        tmux_run("set-option", "-t", clean_id, "detach-on-destroy", "off")
        tmux_run("set-option", "-t", clean_id, "history-limit", "50000")
    meta = save_session_meta(clean_id, final_label, created_at=created_at)
    return meta


def ensure_storage() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(data_path("sessions"), exist_ok=True)
    os.makedirs(data_path("uploads"), exist_ok=True)
    os.makedirs(data_path("tmux"), exist_ok=True)
    if not os.path.exists(data_path("terminal_history.json")):
        with open(data_path("terminal_history.json"), "w", encoding="utf-8") as fh:
            json.dump([], fh)


def now_ts() -> float:
    return time.time()


def sanitize_session_id(raw: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (raw or "").strip().lower())
    cleaned = cleaned.strip("-._")
    return (cleaned or "session")[:48]


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)

def auth_account_path() -> str:
    return data_path("auth_account.json")


def load_auth_account() -> dict[str, Any]:
    stored = load_json(auth_account_path(), {})
    if isinstance(stored, dict) and stored.get("username") and stored.get("password_sha256"):
        return {"username": str(stored.get("username")), "password_sha256": str(stored.get("password_sha256")).strip().lower(), "source": "server"}
    if AUTH_PASSWORD or AUTH_PASSWORD_SHA256:
        return {"username": AUTH_USERNAME, "password_sha256": (AUTH_PASSWORD_SHA256.strip().lower() or _sha256(AUTH_PASSWORD)), "source": "env"}
    return {}


def save_auth_account(username: str, password: str) -> dict[str, Any]:
    username = str(username or "").strip()
    password = str(password or "")
    if not username:
        raise ValueError("Username is required")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    payload = {"username": username[:80], "password_sha256": _sha256(password), "updated_at": now_ts()}
    write_json(auth_account_path(), payload)
    save_auth_sessions({})
    return {"username": payload["username"], "source": "server"}


def update_auth_username(current_username: str, new_username: str) -> dict[str, Any]:
    account = load_auth_account()
    if not account:
        raise ValueError("No account configured")
    current_username = str(current_username or "").strip()
    new_username = str(new_username or "").strip()
    if not new_username:
        raise ValueError("New username is required")
    if not hmac.compare_digest(current_username, str(account.get("username") or "")):
        raise ValueError("Login session is stale; please log in again")
    payload = {
        "username": new_username[:80],
        "password_sha256": str(account.get("password_sha256") or "").strip().lower(),
        "updated_at": now_ts(),
    }
    write_json(auth_account_path(), payload)
    save_auth_sessions({})
    return {"username": payload["username"], "source": "server"}


def update_auth_password(current_username: str, new_password: str) -> dict[str, Any]:
    account = load_auth_account()
    if not account:
        raise ValueError("No account configured")
    current_username = str(current_username or "").strip()
    new_password = str(new_password or "")
    if len(new_password) < 8:
        raise ValueError("New password must be at least 8 characters")
    if not hmac.compare_digest(current_username, str(account.get("username") or "")):
        raise ValueError("Login session is stale; please log in again")
    payload = {
        "username": str(account.get("username") or "")[:80],
        "password_sha256": _sha256(new_password),
        "updated_at": now_ts(),
    }
    write_json(auth_account_path(), payload)
    save_auth_sessions({})
    return {"username": payload["username"], "source": "server"}


def auth_enabled() -> bool:
    return bool(AUTH_REQUIRED and load_auth_account())


def auth_config() -> dict[str, Any]:
    account = load_auth_account()
    return {
        "auth_required": bool(AUTH_REQUIRED),
        "auth_configured": bool(account),
        "setup_required": bool(AUTH_REQUIRED and not account),
        "username": str(account.get("username") or AUTH_USERNAME or "admin"),
        "account_source": str(account.get("source") or "none"),
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_password(username: str, password: str) -> bool:
    if not AUTH_REQUIRED:
        return True
    account = load_auth_account()
    if not account:
        return False
    if not hmac.compare_digest(str(username or ""), str(account.get("username") or "")):
        return False
    expected = str(account.get("password_sha256") or "").strip().lower()
    return bool(expected) and hmac.compare_digest(_sha256(str(password or "")), expected)


def auth_sessions_path() -> str:
    return data_path("auth_sessions.json")


def load_auth_sessions() -> dict[str, Any]:
    raw = load_json(auth_sessions_path(), {})
    return raw if isinstance(raw, dict) else {}


def save_auth_sessions(sessions: dict[str, Any]) -> None:
    write_json(auth_sessions_path(), sessions)


def prune_auth_sessions(sessions: dict[str, Any]) -> dict[str, Any]:
    current = now_ts()
    changed = False
    clean = {}
    for token, record in sessions.items():
        if not isinstance(record, dict):
            changed = True
            continue
        if float(record.get("expires_at") or 0) <= current:
            changed = True
            continue
        clean[str(token)] = record
    if changed:
        save_auth_sessions(clean)
    return clean


def create_auth_session(username: str) -> tuple[str, dict[str, Any]]:
    sessions = prune_auth_sessions(load_auth_sessions())
    token = secrets.token_urlsafe(32)
    record = {"username": username, "created_at": now_ts(), "expires_at": now_ts() + AUTH_SESSION_TTL}
    sessions[token] = record
    save_auth_sessions(sessions)
    return token, record


def delete_auth_session(token: str) -> None:
    if not token:
        return
    sessions = load_auth_sessions()
    if token in sessions:
        sessions.pop(token, None)
        save_auth_sessions(sessions)


def valid_auth_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    sessions = prune_auth_sessions(load_auth_sessions())
    record = sessions.get(token)
    return record if isinstance(record, dict) else None


def safe_data_path(raw: str = "") -> str:
    rel = str(raw or "").strip().lstrip("/")
    root = os.path.abspath(DATA_DIR)
    path = os.path.abspath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("Path escapes data dir")
    return path


def rel_data_path(path: str) -> str:
    rel = os.path.relpath(path, os.path.abspath(DATA_DIR))
    return "" if rel == "." else rel.replace(os.sep, "/")


def file_record(path: str) -> dict[str, Any]:
    st = os.stat(path)
    return {
        "name": os.path.basename(path) or "/data",
        "path": rel_data_path(path),
        "is_dir": os.path.isdir(path),
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
    }


_LAST_CPU_SAMPLE: tuple[int, int] | None = None


def _read_cpu_totals() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            first = fh.readline().split()
    except OSError:
        return None
    if not first or first[0] != "cpu":
        return None
    values = [int(v) for v in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def _cpu_percent() -> float | None:
    global _LAST_CPU_SAMPLE
    sample = _read_cpu_totals()
    if not sample:
        return None
    if not _LAST_CPU_SAMPLE:
        _LAST_CPU_SAMPLE = sample
        return None
    total, idle = sample
    prev_total, prev_idle = _LAST_CPU_SAMPLE
    _LAST_CPU_SAMPLE = sample
    total_delta = max(0, total - prev_total)
    idle_delta = max(0, idle - prev_idle)
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)


def _mem_info() -> dict[str, Any]:
    vals: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                vals[key] = int((rest.strip().split() or [0])[0])
    except (OSError, ValueError):
        return {"percent": None, "used_mb": None, "total_mb": None}
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", 0)
    if total <= 0:
        return {"percent": None, "used_mb": None, "total_mb": None}
    used = max(0, total - available)
    return {
        "percent": round(100.0 * used / total, 1),
        "used_mb": round(used / 1024),
        "total_mb": round(total / 1024),
    }


def _temperature_c() -> float | None:
    import glob

    candidates: list[str] = []
    for pattern in (
        "/host-sys/class/thermal/thermal_zone*/temp",
        "/host-sys/class/hwmon/hwmon*/temp*_input",
        "/sys/class/thermal/thermal_zone*/temp",
        "/sys/class/hwmon/hwmon*/temp*_input",
    ):
        candidates.extend(glob.glob(pattern))
    readings: list[float] = []
    for path in candidates[:80]:
        try:
            raw = open(path, "r", encoding="utf-8").read().strip()
            value = float(raw)
            if value > 1000:
                value = value / 1000.0
            if -20 <= value <= 130:
                readings.append(value)
        except (OSError, ValueError):
            continue
    if not readings:
        return None
    return round(max(readings), 1)


def system_info() -> dict[str, Any]:
    load1 = None
    try:
        load1 = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    disk = os.statvfs(DATA_DIR)
    disk_total = disk.f_blocks * disk.f_frsize
    disk_free = disk.f_bavail * disk.f_frsize
    disk_used = max(0, disk_total - disk_free)
    disk_percent = round((disk_used / disk_total * 100.0), 1) if disk_total else None
    return {
        "ok": True,
        "cpu_percent": _cpu_percent(),
        "load1": load1,
        "memory": _mem_info(),
        "temperature_c": _temperature_c(),
        "disk_percent": disk_percent,
        "ts": now_ts(),
    }


@dataclass
class TerminalSession:
    session_id: str
    label: str
    process: subprocess.Popen
    master_fd: int
    created_at: float = field(default_factory=now_ts)
    buffer: bytearray = field(default_factory=bytearray)
    transcript: bytearray = field(default_factory=bytearray)
    lock: threading.Lock = field(default_factory=threading.Lock)
    alive: bool = True
    cols: int = 120
    rows: int = 32

    def append(self, data: bytes) -> None:
        with self.lock:
            self.buffer.extend(data)
            if len(self.buffer) > 1024 * 1024:
                del self.buffer[:-512 * 1024]
            self.transcript.extend(data)
            if len(self.transcript) > 2 * 1024 * 1024:
                del self.transcript[:-1024 * 1024]

    def read_buffer(self, max_bytes: int = 65536) -> str:
        with self.lock:
            data = bytes(self.buffer[:max_bytes])
            del self.buffer[:max_bytes]
        return data.decode("utf-8", errors="replace")

    def read_transcript(self, max_bytes: int = 262144) -> str:
        with self.lock:
            data = bytes(self.transcript[-max_bytes:])
        return data.decode("utf-8", errors="replace")

    def resize(self, cols: int, rows: int) -> bool:
        cols = max(20, min(int(cols or self.cols), 300))
        rows = max(5, min(int(rows or self.rows), 120))
        try:
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
            try:
                os.killpg(self.process.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass
            self.cols = cols
            self.rows = rows
            return True
        except OSError:
            return False


def parse_resize_frame(data: str) -> tuple[int, int] | None:
    """Parse Hermes-style terminal resize control frames: ESC[RESIZE:<cols>;<rows>]."""
    if not data.startswith("\x1b[RESIZE:") or not data.endswith("]"):
        return None
    try:
        dims = data[len("\x1b[RESIZE:") : -1]
        cols_s, rows_s = dims.split(";", 1)
        cols = max(20, min(int(cols_s), 300))
        rows = max(5, min(int(rows_s), 120))
        return cols, rows
    except (TypeError, ValueError):
        return None


class HistoryStore:
    def __init__(self, path: str, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()

    def _normalize(self, items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in items[: self.limit]:
            if not isinstance(item, dict):
                continue
            cmd = str(item.get("command") or "").strip()
            if not cmd:
                continue
            normalized.append(
                {
                    "command": cmd,
                    "count": int(item.get("count") or 1),
                    "last_used_at": float(item.get("last_used_at") or now_ts()),
                }
            )
        return normalized

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            items = self._normalize(load_json(self.path, []))
        return [{**item, "cmd": item.get("command", ""), "last_used": item.get("last_used_at", 0)} for item in items]

    def _write(self, items: list[dict[str, Any]]) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(items[: self.limit], fh, indent=2)

    def add(self, command: str) -> dict[str, Any] | None:
        cmd = (command or "").strip()
        if not cmd:
            return None
        with self.lock:
            items = self._normalize(load_json(self.path, []))
            found = None
            for item in items:
                if item["command"] == cmd:
                    found = item
                    break
            if found is None:
                found = {"command": cmd, "count": 1, "last_used_at": now_ts()}
                items.insert(0, found)
            else:
                found["count"] = int(found.get("count") or 0) + 1
                found["last_used_at"] = now_ts()
                items = [found] + [item for item in items if item["command"] != cmd]
            self._write(items)
            return found

    def clear(self) -> None:
        with self.lock:
            self._write([])

    def delete(self, command: str) -> None:
        cmd = (command or "").strip()
        with self.lock:
            items = [item for item in self._normalize(load_json(self.path, [])) if item.get("command") != cmd]
            self._write(items)


class PreferencesStore:
    DEFAULTS = {
        "history_view": "all",
        "command_click_mode": "stage",
        "focus_mode": False,
        "default_cwd": DATA_DIR,
        "quick_commands": [
            "pwd",
            "ls -lah",
            "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'",
        ],
        "pinned_commands": [],
        "preset_sections": [],
        "launch_profiles": [
            {"title": "Main shell", "session": "main", "startup_command": "pwd"},
            {"title": "Root shell", "session": "root-shell", "startup_command": "cd /root && pwd"},
            {"title": "Repo shell", "session": "repo-shell", "startup_command": "cd /mnt/tb && pwd"},
            {"title": "Data shell", "session": "data-shell", "startup_command": "cd /data && pwd"},
        ],
        "terminal_settings": {
            "theme": "hermes",
            "font_size": 12.5,
            "line_height": 1.08,
            "scrollback": 5000,
            "cursor_blink": True,
        },
    }

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()

    def get(self) -> dict[str, Any]:
        with self.lock:
            raw = load_json(self.path, {})
            if not isinstance(raw, dict):
                raw = {}
            data = dict(self.DEFAULTS)
            data.update({k: v for k, v in raw.items() if k in self.DEFAULTS})
            if data.get("history_view") not in {"6", "10", "25", "50", "all", "custom"}:
                data["history_view"] = "all"
            data["history_view_mode"] = data.get("history_view")
            data["history_view_custom"] = int(raw.get("history_view_custom") or 15) if str(raw.get("history_view_custom") or "").isdigit() else 15
            if data.get("command_click_mode") not in {"stage", "run"}:
                data["command_click_mode"] = "stage"
            data["focus_mode"] = bool(data.get("focus_mode"))
            data["default_cwd"] = str(data.get("default_cwd") or DATA_DIR)[:240]
            if not isinstance(data.get("quick_commands"), list):
                data["quick_commands"] = list(self.DEFAULTS["quick_commands"])
            data["quick_commands"] = [str(x).strip()[:500] for x in data["quick_commands"] if str(x).strip()][:25]
            if not isinstance(data.get("pinned_commands"), list):
                data["pinned_commands"] = []
            data["pinned_commands"] = [str(x).strip()[:500] for x in data["pinned_commands"] if str(x).strip()][:50]
            if not isinstance(data.get("preset_sections"), list):
                data["preset_sections"] = []
            sections = []
            for section in data["preset_sections"][:30]:
                if not isinstance(section, dict):
                    continue
                title = str(section.get("title") or "Commands").strip()[:80]
                commands = section.get("commands") if isinstance(section.get("commands"), list) else []
                commands = [str(cmd).strip()[:500] for cmd in commands if str(cmd).strip()][:80]
                if title and commands:
                    sections.append({"title": title, "commands": commands})
            data["preset_sections"] = sections
            if not isinstance(data.get("launch_profiles"), list):
                data["launch_profiles"] = list(self.DEFAULTS["launch_profiles"])
            profiles = []
            for profile in data["launch_profiles"][:20]:
                if not isinstance(profile, dict):
                    continue
                title = str(profile.get("title") or "Profile").strip()[:80]
                session = sanitize_session_id(str(profile.get("session") or title or "session"))
                startup_command = str(profile.get("startup_command") or "pwd").strip()[:500]
                if title and session:
                    profiles.append({"title": title, "session": session, "startup_command": startup_command})
            data["launch_profiles"] = profiles or list(self.DEFAULTS["launch_profiles"])
            raw_terminal = data.get("terminal_settings") if isinstance(data.get("terminal_settings"), dict) else {}
            data["terminal_settings"] = {
                "theme": str(raw_terminal.get("theme") or "hermes") if str(raw_terminal.get("theme") or "hermes") in {"hermes", "green", "amber", "mono"} else "hermes",
                "font_size": max(10.0, min(float(raw_terminal.get("font_size") or 12.5), 24.0)),
                "line_height": max(1.0, min(float(raw_terminal.get("line_height") or 1.08), 1.8)),
                "scrollback": max(1000, min(int(raw_terminal.get("scrollback") or 5000), 50000)),
                "cursor_blink": bool(raw_terminal.get("cursor_blink", True)),
            }
            return data

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            current = self.get()
            mapped = dict(updates)
            if "history_view_mode" in mapped and "history_view" not in mapped:
                mapped["history_view"] = mapped.get("history_view_mode")
            for key in self.DEFAULTS:
                if key in mapped:
                    current[key] = mapped[key]
            if "history_view_custom" in mapped:
                current["history_view_custom"] = mapped.get("history_view_custom")
            write_json(self.path, current)
            return self.get()


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, TerminalSession] = {}
        self.lock = threading.Lock()

    def _spawn_client(self, session_id: str, label: str | None = None) -> TerminalSession:
        clean_id = sanitize_session_id(session_id)
        meta = ensure_tmux_session(clean_id, label=label)
        master_fd, slave_fd = os.openpty()
        env = tmux_env()
        env["TERM"] = "xterm-256color"
        env["HOSTNAME"] = "DietPi"
        proc = subprocess.Popen(
            tmux_base_cmd() + ["attach-session", "-t", clean_id],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            cwd=DATA_DIR,
            env=env,
            start_new_session=True,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        session = TerminalSession(session_id=clean_id, label=str(meta.get("label") or clean_id)[:64], process=proc, master_fd=master_fd, created_at=float(meta.get("created_at") or now_ts()))
        snapshot = tmux_capture(clean_id, lines=200)
        if snapshot:
            session.append(snapshot.encode("utf-8", errors="replace"))
        session.resize(session.cols, session.rows)
        self.sessions[clean_id] = session
        threading.Thread(target=self._pump_output, args=(session,), daemon=True).start()
        return session

    def _ensure_attached(self, session_id: str, label: str | None = None) -> TerminalSession | None:
        clean_id = sanitize_session_id(session_id)
        with self.lock:
            existing = self.sessions.get(clean_id)
            if existing and existing.process.poll() is None and existing.alive:
                if label:
                    existing.label = label[:64]
                return existing
            if not tmux_session_exists(clean_id):
                if existing:
                    self.sessions.pop(clean_id, None)
                return None
            return self._spawn_client(clean_id, label=label)

    def list_sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in tmux_list_sessions():
            current = self._ensure_attached(item["id"], label=item.get("label"))
            rows.append(
                {
                    "id": item["id"],
                    "label": current.label if current else item.get("label") or item["id"],
                    "alive": True,
                    "created_at": float(item.get("created_at") or now_ts()),
                    "pid": current.process.pid if current else None,
                    "cols": current.cols if current else 120,
                    "rows": current.rows if current else 32,
                }
            )
        return rows

    def get(self, session_id: str) -> TerminalSession | None:
        clean_id = sanitize_session_id(session_id)
        with self.lock:
            existing = self.sessions.get(clean_id)
            if existing and existing.process.poll() is None and existing.alive:
                return existing
        return self._ensure_attached(clean_id)

    def create(self, session_id: str, label: str | None = None) -> TerminalSession:
        clean_id = sanitize_session_id(session_id)
        ensure_tmux_session(clean_id, label=label)
        current = self._ensure_attached(clean_id, label=label)
        if current is None:
            raise RuntimeError(f"Failed to attach tmux session: {clean_id}")
        current.label = (label or current.label or clean_id)[:64]
        save_session_meta(clean_id, current.label, created_at=current.created_at)
        return current

    def _pump_output(self, session: TerminalSession) -> None:
        while True:
            if session.process.poll() is not None:
                session.alive = False
                try:
                    data = os.read(session.master_fd, 65536)
                    if data:
                        session.append(data)
                except OSError:
                    pass
                if tmux_session_exists(session.session_id):
                    session.append(b"\n[tmux client detached; session preserved]\n")
                else:
                    session.append(f"\n[tmux session ended]\n".encode())
                    delete_session_meta(session.session_id)
                try:
                    os.close(session.master_fd)
                except OSError:
                    pass
                break
            try:
                ready, _, _ = select.select([session.master_fd], [], [], 0.25)
                if ready:
                    data = os.read(session.master_fd, 65536)
                    if data:
                        session.append(data)
            except OSError:
                time.sleep(0.1)

    def write(self, session_id: str, data: str) -> bool:
        session = self.get(sanitize_session_id(session_id))
        if not session:
            return False
        resize = parse_resize_frame(data)
        if resize:
            cols, rows = resize
            return session.resize(cols, rows)
        os.write(session.master_fd, data.encode("utf-8", errors="ignore"))
        return True

    def resize(self, session_id: str, cols: int, rows: int) -> bool:
        session = self.get(sanitize_session_id(session_id))
        if not session:
            return False
        return session.resize(cols, rows)

    def read(self, session_id: str, max_bytes: int = 65536) -> dict[str, Any] | None:
        session = self.get(sanitize_session_id(session_id))
        if not session:
            return None
        return {
            "id": session.session_id,
            "label": session.label,
            "alive": tmux_session_exists(session.session_id),
            "cols": session.cols,
            "rows": session.rows,
            "output": session.read_buffer(max_bytes=max_bytes),
        }

    def transcript(self, session_id: str, max_bytes: int = 262144) -> dict[str, Any] | None:
        session = self.get(sanitize_session_id(session_id))
        if not session:
            return None
        transcript = session.read_transcript(max_bytes=max_bytes)
        if len(transcript.encode("utf-8", errors="replace")) < max_bytes:
            captured = tmux_capture(session.session_id, lines=max(200, min(max_bytes // 120, 4000)))
            if captured:
                transcript = captured[-max_bytes:]
        return {
            "id": session.session_id,
            "label": session.label,
            "alive": tmux_session_exists(session.session_id),
            "cols": session.cols,
            "rows": session.rows,
            "transcript": transcript,
        }

    def rename(self, session_id: str, label: str, new_session_id: str | None = None) -> bool:
        old_id = sanitize_session_id(session_id)
        if not tmux_session_exists(old_id):
            return False
        new_id = sanitize_session_id(new_session_id or old_id)
        final_label = (label or new_id).strip()[:64]
        if new_id != old_id:
            if tmux_session_exists(new_id):
                return False
            result = tmux_run("rename-session", "-t", old_id, new_id)
            if result.returncode != 0:
                return False
        rename_session_meta(old_id, new_id, final_label)
        with self.lock:
            session = self.sessions.pop(old_id, None)
            if session:
                session.session_id = new_id
                session.label = final_label
                self.sessions[new_id] = session
        return True

    def kill(self, session_id: str) -> bool:
        clean_id = sanitize_session_id(session_id)
        result = tmux_run("kill-session", "-t", clean_id)
        if result.returncode != 0:
            return False
        with self.lock:
            session = self.sessions.pop(clean_id, None)
        if session:
            session.alive = False
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        delete_session_meta(clean_id)
        return True


MANAGER = SessionManager()
HISTORY = HistoryStore(data_path("terminal_history.json"), HISTORY_LIMIT)
PREFERENCES = PreferencesStore(data_path("terminal_preferences.json"))


class Handler(BaseHTTPRequestHandler):
    server_version = "RextermHTTP/0.3"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Rexterm-Token, X-Hermes-Session-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json(404, {"ok": False, "error": "File not found"})
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_data_file(self, path: str, download: bool = False) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json(404, {"ok": False, "error": "File not found"})
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f"attachment; filename=\"{os.path.basename(path)}\"")
        self.end_headers()
        self.wfile.write(body)

    def _handle_files_list(self, parsed: Any) -> None:
        q = parse_qs(parsed.query)
        raw = (q.get("path") or [""])[0]
        try:
            target = safe_data_path(raw)
        except ValueError as exc:
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not os.path.exists(target):
            self._send_json(404, {"ok": False, "error": "Path not found"})
            return
        if os.path.isfile(target):
            current = os.path.dirname(target)
            items = [file_record(target)]
        else:
            current = target
            names = sorted(os.listdir(target), key=lambda n: (not os.path.isdir(os.path.join(target, n)), n.lower()))[:300]
            items = [file_record(os.path.join(target, name)) for name in names]
        parent = rel_data_path(os.path.dirname(current)) if os.path.abspath(current) != os.path.abspath(DATA_DIR) else ""
        self._send_json(200, {"ok": True, "cwd": rel_data_path(current), "parent": parent, "items": items})

    def _serve_static(self, parsed_path: str) -> None:
        rel = parsed_path.lstrip("/") or "index.html"
        safe_root = os.path.abspath(STATIC_DIR)
        requested = os.path.abspath(os.path.join(safe_root, rel))
        if not requested.startswith(safe_root):
            self._send_json(403, {"ok": False, "error": "Forbidden"})
            return
        if os.path.isdir(requested):
            requested = os.path.join(requested, "index.html")
        if not os.path.exists(requested):
            requested = os.path.join(safe_root, "index.html")
        self._send_file(requested)

    def _cookie_value(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(name)
        return morsel.value if morsel else ""

    def _auth_session(self) -> dict[str, Any] | None:
        if not AUTH_REQUIRED:
            return {"username": "local", "auth_disabled": True}
        if not auth_enabled():
            return None
        return valid_auth_session(self._cookie_value(AUTH_SESSION_COOKIE))

    def _set_auth_cookie(self, token: str, max_age: int | None = None) -> None:
        parts = [f"{AUTH_SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _send_auth_json(self, code: int, payload: dict[str, Any], cookie_token: str | None = None, clear_cookie: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie_token is not None:
            self._set_auth_cookie(cookie_token, AUTH_SESSION_TTL)
        if clear_cookie:
            self._set_auth_cookie("", 0)
        self.end_headers()
        self.wfile.write(body)

    def _handle_auth_get(self, parsed: Any) -> bool:
        if parsed.path not in {"/api/auth/me", "/api/auth/config"}:
            return False
        session = self._auth_session()
        self._send_auth_json(200, {"ok": True, **auth_config(), "authenticated": bool(session), "user": session.get("username") if session else None})
        return True

    def _handle_auth_post(self, parsed: Any) -> bool:
        if parsed.path == "/api/auth/logout":
            delete_auth_session(self._cookie_value(AUTH_SESSION_COOKIE))
            self._send_auth_json(200, {"ok": True, "authenticated": False}, clear_cookie=True)
            return True
        if parsed.path == "/api/auth/setup":
            existing = load_auth_account()
            session = self._auth_session()

            if existing:
                # Account exists → require login + current password
                if not session:
                    self._send_auth_json(401, {"ok": False, "error": "Login required to change account", **auth_config(), "authenticated": False})
                    return True
                body = self._read_json_body()
                current_pass = str(body.get("current_password") or "")
                if not verify_password(session.get("username", ""), current_pass):
                    self._send_auth_json(401, {"ok": False, "error": "Current password is incorrect", **auth_config(), "authenticated": True})
                    return True
            else:
                # First run — allow unauthenticated creation
                body = self._read_json_body()

            try:
                account = save_auth_account(str(body.get("username") or ""), str(body.get("password") or ""))
            except ValueError as exc:
                self._send_auth_json(400, {"ok": False, "error": str(exc), **auth_config()})
                return True

            token, record = create_auth_session(account["username"])
            self._send_auth_json(200, {"ok": True, **auth_config(), "authenticated": True, "user": record["username"]}, cookie_token=token)
            return True

        if parsed.path == "/api/auth/recover":
            body = self._read_json_body()
            code = str(body.get("recovery_code") or "")
            if not consume_recovery_code(code):
                self._send_auth_json(401, {"ok": False, "error": "Invalid or expired recovery code", **auth_config()})
                return True
            try:
                account = save_auth_account(str(body.get("username") or ""), str(body.get("password") or ""))
            except ValueError as exc:
                self._send_auth_json(400, {"ok": False, "error": str(exc), **auth_config()})
                return True
            token, record = create_auth_session(account["username"])
            self._send_auth_json(200, {"ok": True, **auth_config(), "authenticated": True, "user": record["username"], "recovered": True}, cookie_token=token)
            return True

        if parsed.path in {"/api/auth/change-username", "/api/auth/change-password"}:
            session = self._auth_session()
            if not session:
                self._send_auth_json(401, {"ok": False, "error": "Login required", **auth_config(), "authenticated": False})
                return True
            body = self._read_json_body()
            try:
                if parsed.path == "/api/auth/change-username":
                    account = update_auth_username(session.get("username", ""), str(body.get("new_username") or ""))
                else:
                    current_pass = str(body.get("current_password") or "")
                    if not verify_password(session.get("username", ""), current_pass):
                        self._send_auth_json(401, {"ok": False, "error": "Current password is incorrect", **auth_config(), "authenticated": True})
                        return True
                    account = update_auth_password(session.get("username", ""), str(body.get("new_password") or ""))
            except ValueError as exc:
                self._send_auth_json(400, {"ok": False, "error": str(exc), **auth_config(), "authenticated": True})
                return True
            token, record = create_auth_session(account["username"])
            self._send_auth_json(200, {"ok": True, **auth_config(), "authenticated": True, "user": record["username"]}, cookie_token=token)
            return True

        if parsed.path != "/api/auth/login":
            return False
        body = self._read_json_body()
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not auth_enabled():
            self._send_auth_json(409, {"ok": False, "error": "Create an account first", **auth_config(), "authenticated": False})
            return True
        if not verify_password(username, password):
            self._send_auth_json(401, {"ok": False, "error": "Invalid username or password", **auth_config(), "authenticated": False})
            return True
        token, record = create_auth_session(username)
        self._send_auth_json(200, {"ok": True, **auth_config(), "authenticated": True, "user": record["username"]}, cookie_token=token)
        return True

    def _authorized(self) -> bool:
        return bool(self._auth_session()) or self.headers.get("X-Rexterm-Token") == TOKEN or self.headers.get("X-Hermes-Session-Token") == TOKEN

    def _authorized_query(self, parsed: Any) -> bool:
        q = parse_qs(parsed.query)
        return self._authorized() or ((q.get("token") or [""])[0] == TOKEN)

    def _ws_send_text(self, text: str) -> None:
        payload = text.encode("utf-8", errors="replace")
        header = bytearray([0x81])
        size = len(payload)
        if size < 126:
            header.append(size)
        elif size < 65536:
            header.extend([126, (size >> 8) & 0xFF, size & 0xFF])
        else:
            header.extend([127])
            header.extend(size.to_bytes(8, "big"))
        self.connection.sendall(bytes(header) + payload)

    def _ws_recv_frame(self) -> str | None:
        try:
            header = self.connection.recv(2)
        except socket.timeout:
            return None
        if not header or len(header) < 2:
            raise ConnectionError("websocket closed")
        opcode = header[0] & 0x0F
        if opcode == 0x8:
            raise ConnectionError("websocket close frame")
        masked = bool(header[1] & 0x80)
        size = header[1] & 0x7F
        if size == 126:
            size = int.from_bytes(self.connection.recv(2), "big")
        elif size == 127:
            size = int.from_bytes(self.connection.recv(8), "big")
        mask = self.connection.recv(4) if masked else b""
        payload = bytearray()
        while len(payload) < size:
            chunk = self.connection.recv(size - len(payload))
            if not chunk:
                raise ConnectionError("websocket payload closed")
            payload.extend(chunk)
        if masked:
            payload = bytearray(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        if opcode == 0x9:
            return None
        return bytes(payload).decode("utf-8", errors="replace")

    def _handle_ws(self, parsed: Any) -> None:
        if not self._authorized_query(parsed):
            self._send_json(401, {"ok": False, "error": "Unauthorized"})
            return
        q = parse_qs(parsed.query)
        session_id = sanitize_session_id((q.get("session") or ["main"])[0])
        session = MANAGER.create(session_id, session_id)
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send_json(400, {"ok": False, "error": "Missing websocket key"})
            return
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.connection.settimeout(0.05)
        while True:
            current = MANAGER.get(session.session_id)
            if not current or current.process.poll() is not None:
                self._ws_send_text("\r\n[backend shell ended]\r\n")
                break
            output = current.read_buffer(max_bytes=65536)
            if output:
                self._ws_send_text(output)
            try:
                incoming = self._ws_recv_frame()
                if incoming:
                    resize = parse_resize_frame(incoming)
                    if resize:
                        cols, rows = resize
                        MANAGER.resize(session.session_id, cols, rows)
                        continue
                    MANAGER.write(session.session_id, incoming)
            except socket.timeout:
                pass
            except ConnectionError:
                break

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Rexterm-Token, X-Hermes-Session-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._serve_static(parsed.path)
            return
        if self._handle_auth_get(parsed):
            return
        if parsed.path in {"/api/terminal/ws", "/api/pty"}:
            self._handle_ws(parsed)
            return
        if parsed.path == "/api/health":
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            self._send_json(200, {"ok": True, "app": "rexterm-backend", "port": PORT, "data_dir": DATA_DIR})
            return
        if parsed.path in {"/api/files/download", "/api/terminal/download"}:
            if not self._authorized_query(parsed):
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            q = parse_qs(parsed.query)
            try:
                target = safe_data_path((q.get("path") or [""])[0])
            except ValueError as exc:
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            if not os.path.isfile(target):
                self._send_json(404, {"ok": False, "error": "File not found"})
                return
            self._send_data_file(target, download=True)
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "Unauthorized"})
            return
        if parsed.path == "/api/system":
            self._send_json(200, system_info())
            return
        if parsed.path == "/api/terminal/sessions":
            self._send_json(200, {"ok": True, "screen_available": False, "prefix": "rexterm-", "sessions": [{**s, "state": "attached", "raw": s.get("id")} for s in MANAGER.list_sessions()]})
            return
        if parsed.path == "/api/terminal/read":
            q = parse_qs(parsed.query)
            session_id = (q.get("session") or [""])[0]
            max_bytes = int((q.get("max_bytes") or ["65536"])[0])
            data = MANAGER.read(session_id, max_bytes=max_bytes)
            if data is None:
                self._send_json(404, {"ok": False, "error": "Session not found"})
                return
            self._send_json(200, {"ok": True, **data})
            return
        if parsed.path == "/api/terminal/transcript":
            q = parse_qs(parsed.query)
            session_id = (q.get("session") or [""])[0]
            max_bytes = int((q.get("max_bytes") or ["262144"])[0])
            data = MANAGER.transcript(session_id, max_bytes=max_bytes)
            if data is None:
                self._send_json(404, {"ok": False, "error": "Session not found"})
                return
            self._send_json(200, {"ok": True, **data})
            return
        if parsed.path == "/api/terminal/history":
            self._send_json(200, {"ok": True, "items": HISTORY.list()})
            return
        if parsed.path == "/api/terminal/preferences":
            preferences = PREFERENCES.get()
            self._send_json(200, {"ok": True, "preferences": preferences, **preferences})
            return
        if parsed.path == "/api/files/list":
            self._handle_files_list(parsed)
            return
        if parsed.path == "/api/files/download":
            q = parse_qs(parsed.query)
            try:
                target = safe_data_path((q.get("path") or [""])[0])
            except ValueError as exc:
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            if not os.path.isfile(target):
                self._send_json(404, {"ok": False, "error": "File not found"})
                return
            self._send_data_file(target, download=True)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if self._handle_auth_post(parsed):
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "Unauthorized"})
            return
        body = self._read_json_body()
        if parsed.path == "/api/system":
            self._send_json(200, system_info())
            return
        if parsed.path == "/api/terminal/sessions":
            raw_id = str(body.get("id") or body.get("label") or "session")
            label = str(body.get("label") or raw_id)
            session = MANAGER.create(raw_id, label)
            time.sleep(0.15)
            self._send_json(200, {"ok": True, "session": {"id": session.session_id, "label": session.label, "alive": True}})
            return
        if parsed.path == "/api/terminal/write":
            session_id = str(body.get("session") or "")
            data = str(body.get("data") or "")
            if not session_id:
                self._send_json(400, {"ok": False, "error": "Missing session"})
                return
            if not MANAGER.write(session_id, data):
                self._send_json(404, {"ok": False, "error": "Session not found or dead"})
                return
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/api/terminal/resize":
            session_id = str(body.get("session") or "")
            try:
                cols = int(body.get("cols") or 120)
                rows = int(body.get("rows") or 32)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "Invalid cols/rows"})
                return
            if not session_id:
                self._send_json(400, {"ok": False, "error": "Missing session"})
                return
            if not MANAGER.resize(session_id, cols, rows):
                self._send_json(404, {"ok": False, "error": "Session not found or dead"})
                return
            self._send_json(200, {"ok": True, "session": sanitize_session_id(session_id), "cols": max(20, min(cols, 300)), "rows": max(5, min(rows, 120))})
            return
        if parsed.path == "/api/terminal/rename":
            session_id = str(body.get("session") or body.get("old_session") or "")
            new_session = str(body.get("new_session") or "").strip() or None
            label = str(body.get("label") or new_session or session_id or "")
            if not session_id or not label:
                self._send_json(400, {"ok": False, "error": "Missing session or label"})
                return
            if not MANAGER.rename(session_id, label, new_session):
                self._send_json(404, {"ok": False, "error": "Session not found or target exists"})
                return
            self._send_json(200, {"ok": True, "renamed": True, "old_session": session_id, "new_session": new_session or sanitize_session_id(session_id)})
            return
        if parsed.path == "/api/terminal/kill":
            session_id = str(body.get("session") or "")
            if not session_id:
                self._send_json(400, {"ok": False, "error": "Missing session"})
                return
            removed = MANAGER.kill(session_id)
            if not removed:
                self._send_json(404, {"ok": False, "error": "Session not found"})
                return
            self._send_json(200, {"ok": True, "removed": True, "still_present": False, "session": sanitize_session_id(session_id)})
            return
        if parsed.path == "/api/terminal/history":
            command = str(body.get("command") or body.get("cmd") or "")
            item = HISTORY.add(command)
            if item is None:
                self._send_json(400, {"ok": False, "error": "Missing command"})
                return
            self._send_json(200, {"ok": True, "item": item, "items": HISTORY.list()})
            return
        if parsed.path == "/api/terminal/history/clear":
            HISTORY.clear()
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/api/files/write":
            raw_path = str(body.get("path") or "")
            content = str(body.get("content") or "")
            try:
                target = safe_data_path(raw_path)
            except ValueError as exc:
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._send_json(200, {"ok": True, "file": file_record(target)})
            return
        if parsed.path == "/api/files/upload":
            raw_dir = str(body.get("path") or "")
            name = os.path.basename(str(body.get("name") or "upload.txt")) or "upload.txt"
            content_b64 = str(body.get("content_base64") or "")
            try:
                target_dir = safe_data_path(raw_dir)
                target = safe_data_path(os.path.join(rel_data_path(target_dir), name))
            except ValueError as exc:
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            os.makedirs(target_dir, exist_ok=True)
            try:
                data = base64.b64decode(content_b64.encode(), validate=False)
            except Exception:
                self._send_json(400, {"ok": False, "error": "Invalid base64 upload"})
                return
            with open(target, "wb") as fh:
                fh.write(data)
            self._send_json(200, {"ok": True, "file": file_record(target)})
            return
        if parsed.path == "/api/files/mkdir":
            raw_path = str(body.get("path") or "")
            try:
                target = safe_data_path(raw_path)
            except ValueError as exc:
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            os.makedirs(target, exist_ok=True)
            self._send_json(200, {"ok": True, "file": file_record(target)})
            return
        if parsed.path == "/api/files/delete":
            raw_path = str(body.get("path") or "")
            try:
                target = safe_data_path(raw_path)
            except ValueError as exc:
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            if os.path.abspath(target) == os.path.abspath(DATA_DIR):
                self._send_json(400, {"ok": False, "error": "Refusing to delete /data root"})
                return
            if os.path.isdir(target):
                if os.listdir(target):
                    self._send_json(400, {"ok": False, "error": "Directory is not empty"})
                    return
                os.rmdir(target)
            else:
                os.remove(target)
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/api/terminal/preferences":
            preferences = PREFERENCES.update(body)
            self._send_json(200, {"ok": True, "preferences": preferences, **preferences})
            return
        if parsed.path == "/api/terminal/cleanup":
            self._send_json(200, {"ok": True, "removed_hidden": 0, "removed_empty": 0})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})


    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "Unauthorized"})
            return
        if parsed.path == "/api/terminal/history":
            q = parse_qs(parsed.query)
            cmd = (q.get("cmd") or [""])[0]
            if cmd:
                HISTORY.delete(cmd)
            else:
                HISTORY.clear()
            self._send_json(200, {"ok": True, "items": HISTORY.list()})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})


if __name__ == "__main__":
    ensure_storage()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Rexterm backend listening on {PORT}", flush=True)
    server.serve_forever()
