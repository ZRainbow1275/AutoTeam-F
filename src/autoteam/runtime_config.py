"""运行时可变配置（由面板写入，重启后仍生效）。

与 admin_state.py 区分：admin_state 只放管理员登录态（session/password/...），白名单字段严格；
本模块放"用户在面板里可以调的业务配置"，目前只有 register_domain（子号注册用的 CloudMail 域名），
将来可以扩 batch_size、cool_down 等。持久化到项目根 `runtime_config.json`。
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from autoteam.textio import read_text, write_text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
RUNTIME_CONFIG_FILE = PROJECT_ROOT / "runtime_config.json"
RUNTIME_CONFIG_MODE = 0o666

_LOCK = threading.Lock()


def _load():
    if not RUNTIME_CONFIG_FILE.exists():
        return {}
    try:
        raw = read_text(RUNTIME_CONFIG_FILE).strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        # 静默吞会把用户在面板里设的 register_domain 等覆盖值丢掉,下一轮 _save 会把
        # 损坏文件写回空 dict。保留一份 .corrupt-<ts>.json 便于事后排查。
        corrupt_path = RUNTIME_CONFIG_FILE.with_suffix(f".corrupt-{int(time.time())}.json")
        try:
            RUNTIME_CONFIG_FILE.rename(corrupt_path)
            logger.error("[runtime_config] 解析失败, 已保留原文件为 %s: %s", corrupt_path.name, exc)
        except Exception as rename_exc:
            logger.error("[runtime_config] 解析失败且无法重命名 (%s): %s", exc, rename_exc)
        return {}


def _save(data):
    target = RUNTIME_CONFIG_FILE.resolve()
    write_text(target, json.dumps(data, indent=2, ensure_ascii=False))
    try:
        os.chmod(target, RUNTIME_CONFIG_MODE)
    except Exception:
        pass


def get(key, default=None):
    with _LOCK:
        return _load().get(key, default)


def set_value(key, value):
    with _LOCK:
        data = _load()
        data[key] = value
        _save(data)
        return data


def get_register_domain():
    """返回用于子号注册的 CloudMail 域名。

    优先级：runtime_config.json → 环境变量 CLOUDMAIL_DOMAIN（向后兼容）。
    返回值已 lstrip "@"。
    """
    from autoteam.config import CLOUDMAIL_DOMAIN

    override = (get("register_domain") or "").strip()
    if override:
        return override.lstrip("@").strip()
    return (CLOUDMAIL_DOMAIN or "").lstrip("@").strip()


def set_register_domain(domain):
    """写入 register_domain 覆盖值。空串表示清除 override 走环境变量。"""
    cleaned = (domain or "").strip().lstrip("@").strip()
    set_value("register_domain", cleaned)
    return cleaned


# SPEC-2 FR-E2/E3 — sync_account_states 探测被踢识别的并发上限 + 去重冷却。
# 默认 concurrency=5(单次 sync 最多 5 个账号并发探测 wham/usage),
# cooldown=30 分钟(同一账号 30 分钟内不重复探测,避免抖动)。
# 上下界:concurrency [1, 16],cooldown [1, 1440] 分钟。
_SYNC_PROBE_CONCURRENCY_DEFAULT = 5
_SYNC_PROBE_COOLDOWN_MINUTES_DEFAULT = 30


def get_sync_probe_concurrency():
    """返回 sync_account_states 内并发探测被踢账号的最大 worker 数。"""
    raw = get("sync_probe_concurrency", _SYNC_PROBE_CONCURRENCY_DEFAULT)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _SYNC_PROBE_CONCURRENCY_DEFAULT
    return max(1, min(16, n))


def set_sync_probe_concurrency(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _SYNC_PROBE_CONCURRENCY_DEFAULT
    n = max(1, min(16, n))
    set_value("sync_probe_concurrency", n)
    return n


def get_sync_probe_cooldown_minutes():
    """返回同一账号被探测后多久内不重复探测(分钟)。"""
    raw = get("sync_probe_cooldown_minutes", _SYNC_PROBE_COOLDOWN_MINUTES_DEFAULT)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _SYNC_PROBE_COOLDOWN_MINUTES_DEFAULT
    return max(1, min(1440, n))


def set_sync_probe_cooldown_minutes(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _SYNC_PROBE_COOLDOWN_MINUTES_DEFAULT
    n = max(1, min(1440, n))
    set_value("sync_probe_cooldown_minutes", n)
    return n


# SPEC-2 FR-G — 邀请席位偏好。
#   "default" 走 default→usage_based 兜底 + PATCH 升级,优先 ChatGPT 完整席位(老行为,默认)
#   "codex"   直接 usage_based 邀请,跳过 PATCH,锁 codex-only 席位(节约 ChatGPT 席位时使用)
#   "chatgpt" 别名,Round 7 P2.1 转移期支持,setter/getter 内部归一化为 "default"
_PREFERRED_SEAT_TYPE_DEFAULT = "default"
_PREFERRED_SEAT_TYPE_VALID = {"default", "chatgpt", "codex"}
_PREFERRED_SEAT_TYPE_NORMALIZE = {"chatgpt": "default"}


def _normalize_preferred_seat_type(raw):
    """把任意输入归一化为 {default, codex} 之一(chatgpt 别名 → default,非法/空 → default)。"""
    val = (str(raw or "") or _PREFERRED_SEAT_TYPE_DEFAULT).strip().lower()
    if val not in _PREFERRED_SEAT_TYPE_VALID:
        return _PREFERRED_SEAT_TYPE_DEFAULT
    return _PREFERRED_SEAT_TYPE_NORMALIZE.get(val, val)


def get_preferred_seat_type():
    """返回邀请席位偏好。'default'(默认/优先 PATCH 升级 ChatGPT 席位) 或 'codex'(锁 codex-only)。

    Round 7 P2.1:已落盘的 'chatgpt' 旧值在读取时也归一化为 'default'。
    """
    raw = get("preferred_seat_type", _PREFERRED_SEAT_TYPE_DEFAULT)
    return _normalize_preferred_seat_type(raw)


def set_preferred_seat_type(value):
    """写入席位偏好;接受 'chatgpt' 作为 'default' 的转移期别名(Round 7 P2.1)。"""
    val = _normalize_preferred_seat_type(value)
    set_value("preferred_seat_type", val)
    return val
