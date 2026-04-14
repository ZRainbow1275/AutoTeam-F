"""配置文件 - 从 .env 文件或环境变量加载"""

import os
from pathlib import Path

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
