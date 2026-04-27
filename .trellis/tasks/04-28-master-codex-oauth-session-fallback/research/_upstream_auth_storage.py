"""认证文件目录与权限辅助。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
AUTH_DIR = PROJECT_ROOT / "auths"

# Docker bind mount 下文件常由容器用户写入；给宿主机用户保留可读写权限。
AUTH_FILE_MODE = 0o666


def ensure_auth_dir() -> Path:
    AUTH_DIR.mkdir(exist_ok=True)
    return AUTH_DIR


def ensure_auth_file_permissions(filepath: str | Path | None = None) -> int:
    """统一修复 auths 目录下认证文件权限。"""
    ensure_auth_dir()

    if filepath is None:
        candidates = list(AUTH_DIR.glob("codex-*.json"))
    else:
        candidates = [Path(filepath)]

    updated = 0
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            path.chmod(AUTH_FILE_MODE)
            updated += 1
        except Exception:
            continue
    return updated
