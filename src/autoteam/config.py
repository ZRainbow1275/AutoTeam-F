"""配置文件 - 从 .env 文件或环境变量加载"""

import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from autoteam.textio import parse_env_line, parse_env_value, read_text

# 项目根目录（pyproject.toml 所在位置）
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 加载 .env 文件（从项目根目录）
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    for line in read_text(_env_file).splitlines():
        parsed = parse_env_line(line)
        if parsed:
            key, value = parsed
            os.environ.setdefault(key, value)


def _get_int_env(name: str, default: int) -> int:
    return int(parse_env_value(os.environ.get(name, str(default))))


def _get_str_env(name: str, default: str = "") -> str:
    value = parse_env_value(os.environ.get(name, default))
    return str(value).strip()


def _normalize_chatgpt_api_transport(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"auto", "playwright", "curl_cffi"}:
        return mode
    return "playwright"


# CloudMail 配置
CLOUDMAIL_BASE_URL = os.environ.get("CLOUDMAIL_BASE_URL", "")
CLOUDMAIL_EMAIL = os.environ.get("CLOUDMAIL_EMAIL", "")
CLOUDMAIL_PASSWORD = os.environ.get("CLOUDMAIL_PASSWORD", "")
CLOUDMAIL_DOMAIN = os.environ.get("CLOUDMAIL_DOMAIN", "")

# ChatGPT Team 配置
CHATGPT_ACCOUNT_ID = os.environ.get("CHATGPT_ACCOUNT_ID", "")

# CPA (CLIProxyAPI) 配置
CPA_URL = os.environ.get("CPA_URL", "")
CPA_KEY = os.environ.get("CPA_KEY", "")

# 轮询邮件间隔/超时（秒）
EMAIL_POLL_INTERVAL = _get_int_env("EMAIL_POLL_INTERVAL", 3)
EMAIL_POLL_TIMEOUT = _get_int_env("EMAIL_POLL_TIMEOUT", 300)

# API 鉴权（不设置则不启用）
API_KEY = os.environ.get("API_KEY", "")

# 自动巡检配置
AUTO_CHECK_INTERVAL = _get_int_env("AUTO_CHECK_INTERVAL", 300)  # 巡检间隔（秒），默认 5 分钟
AUTO_CHECK_THRESHOLD = _get_int_env("AUTO_CHECK_THRESHOLD", 10)  # 额度低于此百分比触发轮转，默认 10%
AUTO_CHECK_MIN_LOW = _get_int_env("AUTO_CHECK_MIN_LOW", 2)  # 至少几个账号低于阈值才触发，默认 2


def _get_bool_env(name: str, default: bool) -> bool:
    raw = parse_env_value(os.environ.get(name, "1" if default else "0"))
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "t")


# Round 12 S3 — auth_repair 状态机配置(cherry-pick from upstream).
# AUTO_CHECK_RETRY_ADD_PHONE=true(默认): 注册被 OpenAI 要求 add_phone 时,
#   不立即放弃,而是按指数退避(2^n * AUTO_CHECK_INTERVAL)重试 N 次,N 由
#   AUTO_CHECK_ADD_PHONE_MAX_RETRIES 控制. 关掉后 add_phone 命中即视为
#   hard failure → 立即暂停 + 释放席位.
AUTO_CHECK_RETRY_ADD_PHONE = _get_bool_env("AUTO_CHECK_RETRY_ADD_PHONE", True)
AUTO_CHECK_ADD_PHONE_MAX_RETRIES = _get_int_env("AUTO_CHECK_ADD_PHONE_MAX_RETRIES", 3)


# Round 12 S5 — 预测式抢先替换配置.
# PREDICTIVE_ENABLED=false(默认 安全): cmd_rotate 不做预测式 preempt,
#   保持 round-9~12 旧行为. 用户在前端 settings 主动开启后才参与预测.
# PREDICTIVE_LEAD_MIN=15(默认): 预测剩余时间 < 15 分钟时触发主动 standby + 替换.
# PREDICTIVE_HISTORY_FILE: quota 历史 JSONL 路径(供 QuotaPredictor 使用).
PREDICTIVE_ENABLED = _get_bool_env("PREDICTIVE_ENABLED", False)
PREDICTIVE_LEAD_MIN = _get_int_env("PREDICTIVE_LEAD_MIN", 15)
PREDICTIVE_HISTORY_FILE = PROJECT_ROOT / os.environ.get("PREDICTIVE_HISTORY_FILE", "quota_history.jsonl")

# Round 12 S6 — 并发批量替换配置.
# ROTATE_CONCURRENCY=1(默认 向后兼容): cmd_rotate 串行处理 standby 复用,
#   行为完全等同改造前. 用户调到 N>=2 后启用 ThreadPoolExecutor 并发,
#   每席位独立 try/except,失败聚合不阻塞其他席位.
# 上限保守设 8 — Playwright + ChatGPT API 并发更高反而引入抗扰风险.
ROTATE_CONCURRENCY = max(1, min(8, _get_int_env("ROTATE_CONCURRENCY", 1)))


# 对账策略开关
# RECONCILE_KICK_ORPHAN=true: 残废成员(workspace 有 + 本地 auth_file 缺失)自动 kick。
#   关掉后改为打 STATUS_ORPHAN 标记等人工处理,避免"席位卡死"时仍被本地策略自动清理。
RECONCILE_KICK_ORPHAN = _get_bool_env("RECONCILE_KICK_ORPHAN", True)
# RECONCILE_KICK_GHOST=true: ghost 成员(workspace 有但本地完全无记录)自动 kick。
#   关掉后仅记录日志,依赖 sync_account_states 把 ghost 反向补录回本地,再走一般对账。
RECONCILE_KICK_GHOST = _get_bool_env("RECONCILE_KICK_GHOST", True)

# Playwright 代理配置
PLAYWRIGHT_PROXY_URL = os.environ.get("PLAYWRIGHT_PROXY_URL", "").strip()
PLAYWRIGHT_PROXY_SERVER = os.environ.get("PLAYWRIGHT_PROXY_SERVER", "").strip()
PLAYWRIGHT_PROXY_USERNAME = os.environ.get("PLAYWRIGHT_PROXY_USERNAME", "").strip()
PLAYWRIGHT_PROXY_PASSWORD = os.environ.get("PLAYWRIGHT_PROXY_PASSWORD", "").strip()
PLAYWRIGHT_PROXY_BYPASS = os.environ.get("PLAYWRIGHT_PROXY_BYPASS", "").strip()


def _format_proxy_host(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _parse_proxy_url(proxy_url: str):
    if "://" not in proxy_url:
        return {"server": proxy_url}

    parsed = urlsplit(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return {"server": proxy_url}

    host = _format_proxy_host(parsed.hostname)
    server = f"{parsed.scheme}://{host}"
    if parsed.port:
        server = f"{server}:{parsed.port}"

    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def get_chatgpt_api_transport() -> str:
    # Match autoteam-1: Team backend API reads may use HTTP-first transport by default.
    # Browser/OAuth flows must opt out with require_browser=True at the call site.
    return _normalize_chatgpt_api_transport(_get_str_env("CHATGPT_API_TRANSPORT", "auto"))


def get_chatgpt_api_http_timeout() -> int:
    return max(5, _get_int_env("CHATGPT_API_HTTP_TIMEOUT", 60))


def get_chatgpt_api_impersonate() -> str:
    return _get_str_env("CHATGPT_API_IMPERSONATE", "chrome136") or "chrome136"


def get_chatgpt_http_proxy_url() -> str:
    proxy_url = _get_str_env("PLAYWRIGHT_PROXY_URL", "")
    if proxy_url:
        return proxy_url

    proxy_server = _get_str_env("PLAYWRIGHT_PROXY_SERVER", "")
    if not proxy_server:
        return ""

    username = _get_str_env("PLAYWRIGHT_PROXY_USERNAME", "")
    password = _get_str_env("PLAYWRIGHT_PROXY_PASSWORD", "")
    if not (username or password):
        return proxy_server

    parsed = urlsplit(proxy_server)
    if not parsed.scheme or not parsed.hostname:
        return proxy_server

    host = _format_proxy_host(parsed.hostname)
    auth = quote(username, safe="")
    if password:
        auth = f"{auth}:{quote(password, safe='')}"

    proxy = f"{parsed.scheme}://{auth}@{host}"
    if parsed.port:
        proxy = f"{proxy}:{parsed.port}"
    return proxy


def get_playwright_launch_options():
    """统一的 Playwright Chromium 启动参数。"""
    options = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }

    proxy = None
    if PLAYWRIGHT_PROXY_URL:
        proxy = _parse_proxy_url(PLAYWRIGHT_PROXY_URL)
    elif PLAYWRIGHT_PROXY_SERVER:
        proxy = {"server": PLAYWRIGHT_PROXY_SERVER}
        if PLAYWRIGHT_PROXY_USERNAME:
            proxy["username"] = PLAYWRIGHT_PROXY_USERNAME
        if PLAYWRIGHT_PROXY_PASSWORD:
            proxy["password"] = PLAYWRIGHT_PROXY_PASSWORD

    if proxy:
        if PLAYWRIGHT_PROXY_BYPASS:
            proxy["bypass"] = PLAYWRIGHT_PROXY_BYPASS
        options["proxy"] = proxy

    return options
