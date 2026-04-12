"""管理员登录态持久化。

统一使用项目根目录下的 `state.json` 文件保存：
- session_token
- email
- password
- account_id
- workspace_name
- updated_at

兼容：
- 旧的纯文本 `session`（仅保存 session token）
"""

import json
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
STATE_FILE = PROJECT_ROOT / "state.json"
LEGACY_SESSION_FILE = PROJECT_ROOT / "session"


def _normalize_state(data):
    if not isinstance(data, dict):
        return {}
    return {
        "email": data.get("email", "") or "",
        "session_token": data.get("session_token", "") or "",
        "password": data.get("password", "") or "",
        "account_id": data.get("account_id", "") or "",
        "workspace_name": data.get("workspace_name", "") or "",
        "updated_at": data.get("updated_at"),
    }


def _load_state_from_file(path: Path):
    if not path.exists():
        return {}

    try:
        raw = path.read_text().strip()
    except Exception:
        return {}

    if not raw:
        return {}

    try:
        return _normalize_state(json.loads(raw))
    except Exception:
        # 兼容旧版纯文本 session 文件
        return {
            "email": "",
            "session_token": raw,
            "account_id": "",
            "workspace_name": "",
            "updated_at": path.stat().st_mtime,
        }


def _save_state(state):
    STATE_FILE.write_text(json.dumps(_normalize_state(state), indent=2, ensure_ascii=False))
    os.chmod(STATE_FILE, 0o600)


def _migrate_legacy_state():
    if STATE_FILE.exists():
        return
    state = _load_state_from_file(LEGACY_SESSION_FILE)
    if state:
        _save_state(state)
        try:
            LEGACY_SESSION_FILE.unlink()
        except Exception:
            pass


def load_admin_state():
    _migrate_legacy_state()
    return _load_state_from_file(STATE_FILE)


def save_admin_state(state):
    _save_state(state)


def update_admin_state(**kwargs):
    state = load_admin_state()
    state.update(kwargs)
    state["updated_at"] = time.time()
    save_admin_state(state)
    return state


def clear_admin_state():
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    if LEGACY_SESSION_FILE.exists():
        LEGACY_SESSION_FILE.unlink()


def get_admin_email():
    return load_admin_state().get("email", "")


def get_admin_session_token():
    return load_admin_state().get("session_token", "")


def _is_valid_uuid(value: str) -> bool:
    """检查是否为有效的 UUID 格式"""
    import re

    return bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value, re.I))


def get_chatgpt_account_id():
    state = load_admin_state()
    state_id = state.get("account_id", "")
    # state.json 里的值必须是 UUID 格式才有效（user-xxx 是 user ID 不是 account ID）
    if state_id and _is_valid_uuid(state_id):
        return state_id
    return os.environ.get("CHATGPT_ACCOUNT_ID", "")


def get_admin_password():
    return load_admin_state().get("password", "")


def get_chatgpt_workspace_name():
    state = load_admin_state()
    return state.get("workspace_name", "")


def get_admin_state_summary():
    state = load_admin_state()
    return {
        "configured": bool(state.get("session_token") and state.get("account_id")),
        "email": state.get("email", ""),
        "account_id": state.get("account_id", ""),
        "workspace_name": state.get("workspace_name", ""),
        "session_present": bool(state.get("session_token")),
        "password_saved": bool(state.get("password")),
        "updated_at": state.get("updated_at"),
    }
