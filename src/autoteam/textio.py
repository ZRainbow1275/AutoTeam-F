"""跨平台文本文件读写辅助。"""

import re
from pathlib import Path

_ENV_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")

UTF8_READ_ENCODING = "utf-8-sig"
UTF8_WRITE_ENCODING = "utf-8"


def read_text(path: str | Path) -> str:
    """以 UTF-8（兼容 BOM）读取文本文件。"""
    return Path(path).read_text(encoding=UTF8_READ_ENCODING)


def write_text(path: str | Path, content: str) -> None:
    """以 UTF-8 写入文本文件。"""
    Path(path).write_text(content, encoding=UTF8_WRITE_ENCODING)


def parse_env_value(raw: str) -> str:
    """解析 .env 值，兼容尾部内联注释。"""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return _ENV_INLINE_COMMENT_RE.sub("", value).strip()


def parse_env_line(line: str) -> tuple[str, str] | None:
    """解析单行 .env，返回 (key, value)。"""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    return key, parse_env_value(value)
