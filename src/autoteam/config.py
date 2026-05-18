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


def _get_float_env(name: str, default: float) -> float:
    return float(parse_env_value(os.environ.get(name, str(default))))


def _get_str_env(name: str, default: str = "") -> str:
    value = parse_env_value(os.environ.get(name, default))
    return str(value).strip()


def _normalize_chatgpt_api_transport(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"auto", "playwright", "curl_cffi"}:
        return mode
    return "playwright"


def _normalize_sub2api_ws_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"off", "ctx_pool", "passthrough"}:
        return mode
    return "off"


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
# Team 席位目标硬限制为 3 人: 1 个 owner + 2 个受管子号。
AUTO_CHECK_TARGET_SEATS = max(1, min(3, _get_int_env("AUTO_CHECK_TARGET_SEATS", 3)))
AUTO_CHECK_THRESHOLD = _get_int_env("AUTO_CHECK_THRESHOLD", 10)  # 额度低于此百分比触发轮转，默认 10%
AUTO_CHECK_MIN_LOW = _get_int_env("AUTO_CHECK_MIN_LOW", 2)  # 至少几个账号低于阈值才触发，默认 2


def _get_bool_env(name: str, default: bool) -> bool:
    raw = parse_env_value(os.environ.get(name, "1" if default else "0"))
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "t")


# Sub2API target sync configuration. Empty required fields keep the target
# disabled until explicitly configured.
SUB2API_URL = os.environ.get("SUB2API_URL", "")
SUB2API_EMAIL = os.environ.get("SUB2API_EMAIL", "")
SUB2API_PASSWORD = os.environ.get("SUB2API_PASSWORD", "")
SUB2API_GROUP = os.environ.get("SUB2API_GROUP", "")
SUB2API_PROXY = _get_str_env("SUB2API_PROXY", "")
SUB2API_CONCURRENCY = _get_int_env("SUB2API_CONCURRENCY", 10)
SUB2API_PRIORITY = _get_int_env("SUB2API_PRIORITY", 1)
SUB2API_RATE_MULTIPLIER = _get_float_env("SUB2API_RATE_MULTIPLIER", 1)
SUB2API_AUTO_PAUSE_ON_EXPIRED = _get_bool_env("SUB2API_AUTO_PAUSE_ON_EXPIRED", True)
SUB2API_MODEL_WHITELIST = _get_str_env("SUB2API_MODEL_WHITELIST", "")
SUB2API_OPENAI_WS_MODE = _normalize_sub2api_ws_mode(_get_str_env("SUB2API_OPENAI_WS_MODE", "off"))
SUB2API_OPENAI_PASSTHROUGH = _get_bool_env("SUB2API_OPENAI_PASSTHROUGH", False)
SUB2API_OVERWRITE_ACCOUNT_SETTINGS = _get_bool_env("SUB2API_OVERWRITE_ACCOUNT_SETTINGS", False)


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
PLAYWRIGHT_USER_AGENT = _get_str_env(
    "PLAYWRIGHT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
)
PLAYWRIGHT_LOCALE = _get_str_env("PLAYWRIGHT_LOCALE", "en-US")
PLAYWRIGHT_TIMEZONE_ID = _get_str_env("PLAYWRIGHT_TIMEZONE_ID", "America/Los_Angeles")
PLAYWRIGHT_VIEWPORT_WIDTH = _get_int_env("PLAYWRIGHT_VIEWPORT_WIDTH", 1280)
PLAYWRIGHT_VIEWPORT_HEIGHT = _get_int_env("PLAYWRIGHT_VIEWPORT_HEIGHT", 800)
PLAYWRIGHT_DEVICE_SCALE_FACTOR = _get_float_env("PLAYWRIGHT_DEVICE_SCALE_FACTOR", 1)
PLAYWRIGHT_COLOR_SCHEME = _get_str_env("PLAYWRIGHT_COLOR_SCHEME", "light")
PLAYWRIGHT_DISABLE_WEBRTC_NON_PROXIED_UDP = _get_bool_env("PLAYWRIGHT_DISABLE_WEBRTC_NON_PROXIED_UDP", True)

# Per-account IPv6 proxy pool. Disabled by default so existing local/Docker
# installs do not mutate host networking unless explicitly configured.
AUTOTEAM_IPV6_POOL_ENABLED = _get_bool_env(
    "AUTOTEAM_IPV6_POOL_ENABLED",
    _get_bool_env("IPV6_ROTATE", False),
)
AUTOTEAM_IPV6_POOL_REQUIRED = _get_bool_env("AUTOTEAM_IPV6_POOL_REQUIRED", False)
IPV6_PREFIX = _get_str_env("IPV6_PREFIX", "")
IPV6_IFACE = _get_str_env("IPV6_IFACE", "eth0")
IPV6_PROXY_USE_SUDO = _get_bool_env("IPV6_PROXY_USE_SUDO", False)
IPV6_PROXY_LISTEN_HOST = _get_str_env("IPV6_PROXY_LISTEN_HOST", "0.0.0.0")
IPV6_PROXY_LOCAL_HOST = _get_str_env("IPV6_PROXY_LOCAL_HOST", "127.0.0.1")
IPV6_PROXY_PUBLIC_HOST = _get_str_env("IPV6_PROXY_PUBLIC_HOST", _get_str_env("PUBLIC_IPV4", ""))
PUBLIC_IPV4 = IPV6_PROXY_PUBLIC_HOST
IPV6_PROXY_ALLOWED_IPS = _get_str_env(
    "IPV6_PROXY_ALLOWED_IPS",
    _get_str_env("PROXY_ALLOWED_IPS", "127.0.0.1"),
)
IPV6_PROXY_PORT_START = _get_int_env("IPV6_PROXY_PORT_START", 30000)
IPV6_PROXY_PORT_END = _get_int_env("IPV6_PROXY_PORT_END", 39999)
IPV6_PROXY_TTL_SECONDS = _get_int_env("IPV6_PROXY_TTL_SECONDS", 2 * 24 * 3600)
IPV6_PROXY_POOL_FILE = _get_str_env("IPV6_PROXY_POOL_FILE", str(PROJECT_ROOT / "ipv6_pool.json"))


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


def get_chatgpt_http_proxy_url(proxy_url: str | None = None) -> str:
    proxy_url = str(proxy_url or "").strip() or _get_str_env("PLAYWRIGHT_PROXY_URL", "")
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


def get_playwright_launch_options(proxy_url: str | None = None):
    """统一的 Playwright Chromium 启动参数。"""
    width = max(800, int(PLAYWRIGHT_VIEWPORT_WIDTH or 1280))
    height = max(600, int(PLAYWRIGHT_VIEWPORT_HEIGHT or 800))
    options = {
        "headless": False,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-update",
            "--disable-crash-reporter",
            "--disable-features=Translate,MediaRouter",
            f"--lang={PLAYWRIGHT_LOCALE}",
            f"--window-size={width},{height}",
        ],
    }
    if PLAYWRIGHT_DISABLE_WEBRTC_NON_PROXIED_UDP:
        options["args"].append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")

    proxy = None
    if proxy_url:
        proxy = _parse_proxy_url(proxy_url)
    elif PLAYWRIGHT_PROXY_URL:
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


def get_playwright_context_options():
    """Shared browser context fingerprint options for ChatGPT/OAuth/register flows."""
    width = max(800, int(PLAYWRIGHT_VIEWPORT_WIDTH or 1280))
    height = max(600, int(PLAYWRIGHT_VIEWPORT_HEIGHT or 800))
    scale = max(1, float(PLAYWRIGHT_DEVICE_SCALE_FACTOR or 1))
    color_scheme = PLAYWRIGHT_COLOR_SCHEME if PLAYWRIGHT_COLOR_SCHEME in {"dark", "light", "no-preference"} else "light"
    language = PLAYWRIGHT_LOCALE.split("-")[0]

    return {
        "viewport": {"width": width, "height": height},
        "user_agent": PLAYWRIGHT_USER_AGENT,
        "locale": PLAYWRIGHT_LOCALE,
        "timezone_id": PLAYWRIGHT_TIMEZONE_ID,
        "device_scale_factor": scale,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": color_scheme,
        "extra_http_headers": {"Accept-Language": f"{PLAYWRIGHT_LOCALE},{language};q=0.9"},
    }
