"""AutoTeam HTTP API - 将 CLI 功能暴露为 HTTP 接口"""

import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from autoteam.config import API_KEY
from autoteam.textio import read_text

logger = logging.getLogger(__name__)


# Round 7 P2.4 — FastAPI 现代 lifespan handler 替代已废弃的 @app.on_event。
# 启动期:修复 auths 认证文件权限 + 启动 _auto_check_loop 后台线程。
# 停止期:发 _auto_check_stop 信号让线程优雅退出。
# 引用的 _auto_check_loop / _auto_check_stop / ensure_auth_file_permissions 都在
# module 后段定义,Python 闭包延迟绑定符合预期(yield 时才会解析名字)。
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    logger.info("[lifespan] starting")
    try:
        from autoteam.auth_storage import ensure_auth_file_permissions

        fixed = ensure_auth_file_permissions()
        if fixed:
            logger.info("[启动] 已修复 %d 个 auths 认证文件权限", fixed)
    except Exception as exc:
        logger.warning("[启动] 修复 auths 认证文件权限失败: %s", exc)

    thread = threading.Thread(target=_auto_check_loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        logger.info("[lifespan] stopping")
        _auto_check_stop.set()


app = FastAPI(
    title="AutoTeam API",
    description="ChatGPT Team 账号自动轮转管理 API",
    version="0.1.0",
    lifespan=app_lifespan,
)


# ---------------------------------------------------------------------------
# 版本端点 (SPEC-3 §5) - 不鉴权,纯只读
# ---------------------------------------------------------------------------


class VersionResponse(BaseModel):
    """镜像版本指纹响应 - 来自 Dockerfile build args。"""

    git_sha: str
    build_time: str


@app.get(
    "/api/version",
    response_model=VersionResponse,
    summary="返回镜像构建期注入的 git-sha 与时间戳",
    tags=["meta"],
)
def api_version() -> VersionResponse:
    return VersionResponse(
        git_sha=os.getenv("AUTOTEAM_GIT_SHA", "unknown"),
        build_time=os.getenv("AUTOTEAM_BUILD_TIME", "unknown"),
    )


# ---------------------------------------------------------------------------
# API Key 鉴权中间件
# ---------------------------------------------------------------------------

_AUTH_SKIP_PATHS = {
    "/api/auth/check",
    "/api/setup/status",
    "/api/setup/save",
    "/api/version",
    "/api/mail-provider/probe",  # SPEC-1 §3.5 — 条件鉴权:API_KEY 配置后路由内强制 Bearer
}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 不鉴权的路径：非 /api 路径、auth/check 端点
    if not path.startswith("/api/") or path in _AUTH_SKIP_PATHS:
        return await call_next(request)
    # 未配置 API_KEY 则跳过鉴权
    if not API_KEY:
        return await call_next(request)
    # 从 header 或 query param 获取 key
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("key", "")
    if token != API_KEY:
        return JSONResponse(status_code=401, content={"detail": "未授权，请提供有效的 API Key"})
    return await call_next(request)


@app.get("/api/auth/check")
def check_auth(request: Request):
    """验证 API Key 是否有效。未配置 API_KEY 时始终返回成功。"""
    if not API_KEY:
        return {"authenticated": True, "auth_required": False}
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and auth_header[7:] == API_KEY:
        return {"authenticated": True, "auth_required": True}
    return JSONResponse(status_code=401, content={"authenticated": False, "auth_required": True})


# ---------------------------------------------------------------------------
# 初始配置 API（无需鉴权）
# ---------------------------------------------------------------------------


class SetupConfig(BaseModel):
    """`/api/setup/save` 请求体。SPEC-1 §2.1 — 含 cf_temp_email + maillab 两套 mail 字段。"""

    MAIL_PROVIDER: Literal["cf_temp_email", "cloudflare_temp_email", "maillab"] = "cf_temp_email"
    CLOUDMAIL_BASE_URL: str = ""
    CLOUDMAIL_EMAIL: str = ""
    CLOUDMAIL_PASSWORD: str = ""
    CLOUDMAIL_DOMAIN: str = ""
    MAILLAB_API_URL: str = ""
    MAILLAB_USERNAME: str = ""
    MAILLAB_PASSWORD: str = ""
    MAILLAB_DOMAIN: str = ""
    CPA_URL: str = "http://127.0.0.1:8317"
    CPA_KEY: str = ""
    PLAYWRIGHT_PROXY_URL: str = ""
    PLAYWRIGHT_PROXY_BYPASS: str = ""
    API_KEY: str = ""


class ProbeErrorCode(str, Enum):
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN_DOMAIN = "FORBIDDEN_DOMAIN"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    EMPTY_DOMAIN_LIST = "EMPTY_DOMAIN_LIST"
    UNKNOWN = "UNKNOWN"


class MailProviderProbeRequest(BaseModel):
    """`/api/mail-provider/probe` 请求体。SPEC-1 §2.1。"""

    provider: Literal["cf_temp_email", "maillab"]
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    base_url: str = Field(..., min_length=1, max_length=512)
    admin_password: str = ""
    username: str = ""
    password: str = ""
    domain: str = ""
    bearer_token: str = ""

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        return (v or "").strip().lstrip("@")


class MailProviderProbeResponse(BaseModel):
    ok: bool
    step: Literal["fingerprint", "credentials", "domain_ownership"]
    error_code: ProbeErrorCode | None = None
    message: str | None = None
    hint: str | None = None
    warnings: list[str] = []
    detected_provider: Literal["cf_temp_email", "maillab", "unknown"] | None = None
    domain_list: list[str] | None = None
    add_verify_open: bool | None = None
    register_verify_open: bool | None = None
    is_admin: bool | None = None
    user_email: str | None = None
    token_preview: str | None = None
    probe_email: str | None = None
    probe_account_id: int | None = None
    cleaned: bool | None = None
    leaked_probe: dict | None = None


_CF_MAIL_KEYS = {"CLOUDMAIL_BASE_URL", "CLOUDMAIL_PASSWORD", "CLOUDMAIL_DOMAIN"}
_MAILLAB_KEYS = {"MAILLAB_API_URL", "MAILLAB_USERNAME", "MAILLAB_PASSWORD", "MAILLAB_DOMAIN"}


@app.get("/api/setup/status")
def get_setup_status():
    """检查配置是否完整 — SPEC-1 §3.6:按 provider 动态标 optional。"""
    from autoteam.setup_wizard import REQUIRED_CONFIGS, _read_env

    env = _read_env()
    provider = (env.get("MAIL_PROVIDER") or os.environ.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    fields = []
    all_ok = True
    for key, prompt, default, optional in REQUIRED_CONFIGS:
        # 不属于当前 provider 的 mail 字段强制 optional
        if provider == "maillab" and key in _CF_MAIL_KEYS:
            optional = True
        elif provider in ("cf_temp_email", "cloudflare_temp_email") and key in _MAILLAB_KEYS:
            optional = True
        val = env.get(key, "") or os.environ.get(key, "")
        ok = bool(val)
        if not ok and not optional:
            all_ok = False
        fields.append({"key": key, "prompt": prompt, "default": default, "optional": optional, "configured": ok})
    return {"configured": all_ok, "fields": fields, "provider": provider}


@app.post("/api/setup/save")
def post_setup_save(config: SetupConfig):
    """保存配置到 .env 并验证连通性"""
    import secrets as _secrets

    from autoteam.setup_wizard import REQUIRED_CONFIGS, _write_env

    data = config.model_dump()
    defaults = {key: default for key, _prompt, default, _optional in REQUIRED_CONFIGS}
    if not data.get("CPA_URL"):
        data["CPA_URL"] = defaults.get("CPA_URL", "http://127.0.0.1:8317")
    if not data.get("API_KEY"):
        data["API_KEY"] = _secrets.token_urlsafe(24)

    # SPEC-1 §3.6 — 按 provider 互斥写入,避免无关字段污染 .env
    provider = (data.get("MAIL_PROVIDER") or "cf_temp_email").strip().lower()
    if provider in ("cf_temp_email", "cloudflare_temp_email"):
        skip_keys = set(_MAILLAB_KEYS)
    elif provider == "maillab":
        # CLOUDMAIL_DOMAIN 保留作 maillab 的 fallback;仅跳过 base_url/password
        skip_keys = {"CLOUDMAIL_BASE_URL", "CLOUDMAIL_PASSWORD"}
    else:
        skip_keys = set()

    clearable_fields = {"PLAYWRIGHT_PROXY_URL", "PLAYWRIGHT_PROXY_BYPASS"}
    for key, value in data.items():
        if key in skip_keys:
            continue
        if value or key in clearable_fields:
            _write_env(key, value)
            os.environ[key] = value

    # 重新加载模块
    import importlib

    import autoteam.config

    importlib.reload(autoteam.config)
    try:
        import autoteam.cloudmail

        importlib.reload(autoteam.cloudmail)
    except Exception:
        pass

    # 验证连通性
    errors = []
    from autoteam.setup_wizard import _verify_cloudmail, _verify_cpa

    if not _verify_cloudmail():
        errors.append("CloudMail 连接失败")
    if not _verify_cpa():
        errors.append("CPA 连接失败")

    if errors:
        return JSONResponse(status_code=400, content={"message": "、".join(errors), "api_key": data["API_KEY"]})

    # 更新运行时 API_KEY
    global API_KEY
    API_KEY = data["API_KEY"]

    return {"message": "配置保存成功", "api_key": data["API_KEY"], "configured": True}


# ---------------------------------------------------------------------------
# Mail Provider 在线探测 (SPEC-1 §3.5) — 三步分阶段验证
# ---------------------------------------------------------------------------

_probe_rate_buckets: dict[str, list[float]] = {}
_probe_rate_lock = threading.Lock()


def _enforce_probe_rate_limit(request: Request, max_per_min: int = 60):
    """SPEC §NFR-速率限制:setup 阶段 (无 API_KEY) 单 IP 60 req/min,防扫描。"""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _probe_rate_lock:
        bucket = _probe_rate_buckets.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= max_per_min:
            raise HTTPException(status_code=429, detail="probe 请求过频,请稍后再试")
        bucket.append(now)


@app.post("/api/mail-provider/probe", response_model=MailProviderProbeResponse)
def post_mail_provider_probe(req: MailProviderProbeRequest, request: Request):
    """SPEC-1 §3.5 — 三步分阶段:fingerprint → credentials → domain_ownership。

    鉴权策略:
      - API_KEY 未配置(setup 阶段):放行,但走 IP 速率限制
      - API_KEY 已配置:强制 Bearer(_AUTH_SKIP_PATHS 已加白,这里手工二次校验)
    """
    from autoteam.config import API_KEY as _key

    if _key:
        auth_header = request.headers.get("authorization", "")
        if not (auth_header.startswith("Bearer ") and auth_header[7:] == _key):
            raise HTTPException(status_code=401, detail="API_KEY 已配置,请提供 Bearer token")
    else:
        _enforce_probe_rate_limit(request)

    from autoteam.mail import probe as mail_probe

    try:
        if req.step == "fingerprint":
            result = mail_probe.probe_fingerprint(req.base_url, req.provider)
        elif req.step == "credentials":
            result = mail_probe.probe_credentials(
                req.base_url,
                req.provider,
                username=req.username,
                password=req.password,
                admin_password=req.admin_password,
            )
        else:  # domain_ownership
            result = mail_probe.probe_domain_ownership(
                req.base_url,
                req.provider,
                bearer_token=req.bearer_token,
                admin_password=req.admin_password,
                domain=req.domain,
                username=req.username,
                password=req.password,
            )
    except mail_probe.ProbeError as exc:
        return MailProviderProbeResponse(
            ok=False,
            step=req.step,
            error_code=ProbeErrorCode(exc.error_code) if exc.error_code in ProbeErrorCode.__members__ else ProbeErrorCode.UNKNOWN,
            message=exc.message,
            hint=exc.hint,
        )
    except Exception as exc:  # noqa: BLE001 — 兜底
        logger.exception("[probe] %s 异常: %s", req.step, exc)
        return MailProviderProbeResponse(
            ok=False,
            step=req.step,
            error_code=ProbeErrorCode.UNKNOWN,
            message=str(exc),
        )

    # ProbeResult → ProbeResponse(忽略内部 bearer_token,前端不持有)
    payload = {k: v for k, v in vars(result).items() if k != "bearer_token"}
    if "error_code" in payload and payload["error_code"]:
        try:
            payload["error_code"] = ProbeErrorCode(payload["error_code"])
        except ValueError:
            payload["error_code"] = ProbeErrorCode.UNKNOWN
    return MailProviderProbeResponse(**payload)


# ---------------------------------------------------------------------------
# 后台任务管理
# ---------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_playwright_lock = threading.Lock()
_current_task_id: str | None = None
_admin_login_api = None
_admin_login_step: str | None = None
_main_codex_flow = None
_main_codex_step: str | None = None
_manual_account_flow = None
MAX_TASK_HISTORY = 50


# ---------------------------------------------------------------------------
# Playwright 专用线程执行器（解决跨线程调用问题）
# ---------------------------------------------------------------------------

import queue as _queue

from autoteam._playwright_guard import assert_sync_context


class _PlaywrightExecutor:
    """将 Playwright 操作派发到专用线程执行，避免跨线程错误"""

    def __init__(self):
        self._queue: _queue.Queue = _queue.Queue()
        self._thread: threading.Thread | None = None

    def _worker(self):
        # SPEC-4 §3.2: worker 线程入口必须在普通线程,不允许 asyncio loop
        assert_sync_context()
        while True:
            item = self._queue.get()
            if item is None:
                break
            func, args, kwargs, result_event, result_holder = item
            try:
                result_holder["result"] = func(*args, **kwargs)
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_event.set()

    def ensure_started(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def run(self, func, *args, **kwargs):
        """在专用线程中执行函数，阻塞等待结果(默认 5 分钟)"""
        return self.run_with_timeout(300, func, *args, **kwargs)

    def run_with_timeout(self, timeout: float, func, *args, **kwargs):
        """
        明确指定超时时间(秒)。适用于批量/长耗时操作。

        注意:超时后 worker 线程仍会继续跑完当前 func(Playwright 操作无法安全中断),
        后续通过 _pw_executor 提交的调用会在队列里等它自然完成。调用方需要自己
        确保不会越过 _playwright_lock 边界并发触发这种情况。
        """
        # SPEC-4 §3.2: 主线程前置检查,拦下 asyncio loop 误入
        assert_sync_context()
        self.ensure_started()
        result_event = threading.Event()
        result_holder: dict = {}
        self._queue.put((func, args, kwargs, result_event, result_holder))
        if not result_event.wait(timeout=timeout):
            raise TimeoutError(
                f"Playwright executor timed out after {timeout}s while running {getattr(func, '__name__', repr(func))}"
            )
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None


_pw_executor = _PlaywrightExecutor()


def _current_busy_detail(default_message: str):
    if _admin_login_api:
        return {
            "message": default_message,
            "running_task": {
                "task_id": "admin-login",
                "command": "admin-login",
                "started_at": None,
            },
        }

    if _main_codex_flow:
        return {
            "message": default_message,
            "running_task": {
                "task_id": "main-codex-sync",
                "command": "main-codex-sync",
                "started_at": None,
            },
        }

    running = _tasks.get(_current_task_id, {})
    return {
        "message": default_message,
        "running_task": {
            "task_id": _current_task_id,
            "command": running.get("command", "unknown"),
            "started_at": running.get("started_at"),
        },
    }


def _prune_tasks():
    """保留最近 MAX_TASK_HISTORY 个任务"""
    if len(_tasks) <= MAX_TASK_HISTORY:
        return
    sorted_ids = sorted(_tasks, key=lambda k: _tasks[k]["created_at"])
    for tid in sorted_ids[: len(_tasks) - MAX_TASK_HISTORY]:
        if _tasks[tid]["status"] in ("completed", "failed"):
            del _tasks[tid]


def _run_task(task_id: str, func, *args, **kwargs):
    """在后台线程中执行任务"""
    from autoteam import cancel_signal

    global _current_task_id
    task = _tasks[task_id]

    _playwright_lock.acquire()
    # 顺序很关键: 先 reset() 再暴露 _current_task_id。否则 post_task_cancel 在
    # 两行之间读到新 task_id 并 request_cancel(),随后被我们的 reset() 清掉,
    # 用户的取消请求被静默吞掉。
    cancel_signal.reset()
    _current_task_id = task_id
    task["status"] = "running"
    task["started_at"] = time.time()

    try:
        result = func(*args, **kwargs)
        # 任务完成但中途确实收到取消 → 标 cancelled
        task["status"] = "cancelled" if cancel_signal.is_cancelled() else "completed"
        task["result"] = result
    except Exception as e:
        task["status"] = "cancelled" if cancel_signal.is_cancelled() else "failed"
        task["error"] = str(e)
        logger.error("[API] 任务 %s %s: %s", task_id[:8], task["status"], e)
    finally:
        task["finished_at"] = time.time()
        _current_task_id = None
        _playwright_lock.release()


def _start_task(command: str, func, params: dict, *args, **kwargs) -> dict:
    """创建并启动后台任务，返回任务信息"""
    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再试"))
    _playwright_lock.release()

    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "command": command,
        "params": params,
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    _tasks[task_id] = task
    _prune_tasks()

    thread = threading.Thread(target=_run_task, args=(task_id, func, *args), kwargs=kwargs, daemon=True)
    thread.start()

    return task


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class TaskParams(BaseModel):
    target: int = 5
    leave_workspace: bool = False  # cmd_fill 专用：True 表示生产免费号（注册后退出 Team 走 personal OAuth）


class CleanupParams(BaseModel):
    max_seats: int | None = None


class AdminEmailParams(BaseModel):
    email: str


class AdminSessionParams(BaseModel):
    email: str
    session_token: str


class AdminPasswordParams(BaseModel):
    password: str


class AdminCodeParams(BaseModel):
    code: str


class AdminWorkspaceParams(BaseModel):
    option_id: str


class ManualAccountCallbackParams(BaseModel):
    redirect_url: str


class TeamMemberRemoveParams(BaseModel):
    email: str
    user_id: str
    type: str


class RegisterDomainParams(BaseModel):
    domain: str
    verify: bool = True  # 默认写入前试探一次 CloudMail 是否接受该域


class PreferredSeatTypeParams(BaseModel):
    """SPEC-2 FR-G — 邀请席位偏好。"default"(优先 PATCH 升级 ChatGPT 完整席位) | "codex"(锁 codex-only)"""
    value: str  # "default" | "codex"


class SyncProbeParams(BaseModel):
    """SPEC-2 FR-E — sync_account_states 被踢探测的并发上限和去重冷却。"""
    concurrency: int | None = None  # 1..16
    cooldown_minutes: int | None = None  # 1..1440


class DeleteBatchParams(BaseModel):
    emails: list[str]
    continue_on_error: bool = True  # 部分失败时继续剩余账号,False 则遇错即停


def _normalized_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_main_account_email(email: str | None) -> bool:
    from autoteam.admin_state import get_admin_email

    return bool(_normalized_email(email)) and _normalized_email(email) == _normalized_email(get_admin_email())


def _quota_snapshot_status(quota_info: dict | None) -> str:
    if not isinstance(quota_info, dict):
        return ""

    values = []
    for key in ("primary_pct", "weekly_pct"):
        value = quota_info.get(key)
        if isinstance(value, (int, float)):
            values.append(value)

    if not values:
        return ""
    return "exhausted" if any(value >= 100 for value in values) else "active"


def _resolve_status_auth_file(acc: dict) -> str:
    auth_file = (acc.get("auth_file") or "").strip()
    if auth_file and Path(auth_file).exists():
        return auth_file

    if _is_main_account_email(acc.get("email")):
        from autoteam.codex_auth import get_saved_main_auth_file

        saved_auth_file = get_saved_main_auth_file()
        if saved_auth_file and Path(saved_auth_file).exists():
            return saved_auth_file

    return ""


def _display_account_status(acc: dict, quota_snapshot: dict | None = None) -> str:
    status = acc.get("status", "")
    if not _is_main_account_email(acc.get("email")):
        return status

    quota_status = _quota_snapshot_status(quota_snapshot) or _quota_snapshot_status(acc.get("last_quota"))
    if quota_status:
        return quota_status

    return "active" if _resolve_status_auth_file(acc) else status


def _sanitize_account(acc: dict, quota_snapshot: dict | None = None) -> dict:
    """脱敏账号信息（去掉 password 等敏感字段）"""
    sanitized = {k: v for k, v in acc.items() if k not in ("password", "cloudmail_account_id")}
    sanitized["is_main_account"] = _is_main_account_email(acc.get("email"))
    sanitized["status"] = _display_account_status(acc, quota_snapshot)
    return sanitized


def _admin_status():
    from autoteam.admin_state import get_admin_state_summary

    status = get_admin_state_summary()
    status["login_step"] = _admin_login_step
    status["login_in_progress"] = _admin_login_api is not None
    if _admin_login_api and _admin_login_step == "workspace_required":
        status["workspace_options"] = getattr(_admin_login_api, "workspace_options_cache", []) or []
    else:
        status["workspace_options"] = []
    return status


def _main_codex_status():
    return {
        "in_progress": _main_codex_flow is not None,
        "step": _main_codex_step,
    }


def _manual_account_status():
    status = {
        "in_progress": False,
        "status": "idle",
        "state": "",
        "auth_url": "",
        "started_at": None,
        "message": "",
        "error": "",
        "account": None,
        "callback_received": False,
        "callback_source": "",
        "auto_callback_available": False,
        "auto_callback_error": "",
    }
    if _manual_account_flow:
        status.update(_manual_account_flow.status())
    return status


def _finish_admin_login(completed: dict):
    global _admin_login_api, _admin_login_step
    api = _admin_login_api
    info = None
    try:
        info = _pw_executor.run(api.complete_admin_login)
    finally:
        if api:
            try:
                _pw_executor.run(api.stop)
            except Exception:
                pass
        _admin_login_api = None
        _admin_login_step = None
        if info and info.get("session_token") and info.get("account_id"):
            try:
                from autoteam.codex_auth import refresh_main_auth_file

                main_auth = _pw_executor.run(refresh_main_auth_file)
                if main_auth:
                    info["main_auth"] = main_auth
                    logger.info("[API] 管理员登录后已刷新主号认证文件: %s", main_auth.get("auth_file"))
            except Exception as exc:
                info["main_auth_error"] = str(exc)
                logger.warning("[API] 管理员登录完成，但刷新主号认证文件失败: %s", exc)
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {"status": "completed", "admin": _admin_status(), "info": info}


def _set_pending_admin_login(api, step):
    global _admin_login_api, _admin_login_step
    _admin_login_api = api
    _admin_login_step = step
    return {"status": step, "admin": _admin_status()}


def _finish_main_codex_sync():
    global _main_codex_flow, _main_codex_step
    flow = _main_codex_flow
    try:
        info = _pw_executor.run(flow.complete)
    finally:
        if flow:
            try:
                _pw_executor.run(flow.stop)
            except Exception:
                pass
        _main_codex_flow = None
        _main_codex_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {
        "status": "completed",
        "message": "主号 Codex 已同步到 CPA",
        "codex": _main_codex_status(),
        "info": info,
    }


def _set_pending_main_codex_sync(flow, step):
    global _main_codex_flow, _main_codex_step
    _main_codex_flow = flow
    _main_codex_step = step
    return {"status": step, "codex": _main_codex_status()}


def _finish_manual_account_flow(result: dict):
    return {**result, "manual_account": _manual_account_status()}


def _set_pending_manual_account_flow(flow, result):
    global _manual_account_flow
    _manual_account_flow = flow
    return {**result, "manual_account": _manual_account_status()}


# ---------------------------------------------------------------------------
# 同步端点
# ---------------------------------------------------------------------------


@app.get("/api/admin/status")
def get_admin_status():
    """获取管理员登录状态。"""
    return _admin_status()


@app.post("/api/admin/fix-account-id")
def post_admin_fix_account_id():
    """
    基于当前已保存的 session_token,重新从 /backend-api/accounts 拉取真实 workspace 列表,
    覆盖写入 admin_state.account_id / workspace_name。适用于: 之前导入的 session 把
    account_id 误写成了 OAI 缓存的陈旧 UUID,导致所有 admin 接口 401。

    不需要用户手动退出重登 —— 只是重算 account_id。
    """
    from autoteam.admin_state import (
        get_admin_email,
        get_admin_session_token,
        get_chatgpt_account_id,
        update_admin_state,
    )
    from autoteam.chatgpt_api import ChatGPTTeamAPI

    if not get_admin_session_token():
        raise HTTPException(status_code=400, detail="尚未保存 session_token,请先导入")

    def _do():
        api = ChatGPTTeamAPI()
        try:
            api._launch_browser()
            logger.info("[修复 account_id] 打开 chatgpt.com 注入 session...")
            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            api._wait_for_cloudflare()
            api._inject_session(get_admin_session_token())
            # 注入 session 后可能触发一次新的 CF 挑战,再等一次避免首个 _api_fetch 碰上 challenge 页
            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            api._wait_for_cloudflare()
            api._fetch_access_token()

            team, personal = api._list_real_workspaces()
            admin_roles = ("account-owner", "admin", "org-admin", "workspace-owner")
            chosen = None
            for acc in team:
                if str(acc.get("current_user_role") or "").lower() in admin_roles:
                    chosen = acc
                    break
            if not chosen and team:
                chosen = team[0]
            if not chosen:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"当前 session ({get_admin_email()}) 没有 Team workspace,"
                        f" 只有: {[a.get('structure') for a in personal]}。"
                        f"请确认该账号已被邀请加入 Team。"
                    ),
                )

            new_account_id = str(chosen.get("id") or "")
            new_workspace_name = str(chosen.get("name") or "")

            # 用新 account_id 验证接口是否真能访问
            api.account_id = new_account_id
            verify = api._api_fetch("GET", f"/backend-api/accounts/{new_account_id}/settings")
            if verify.get("status") != 200:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"新 account_id={new_account_id} 仍不可访问 "
                        f"status={verify.get('status')},session_token 可能已过期,请重新导入。"
                    ),
                )

            old_account_id = get_chatgpt_account_id()
            update_admin_state(account_id=new_account_id, workspace_name=new_workspace_name)
            logger.info(
                "[修复 account_id] 已更新: %s -> %s (workspace=%s)",
                old_account_id,
                new_account_id,
                new_workspace_name,
            )
            return {
                "message": "已修复",
                "old_account_id": old_account_id,
                "new_account_id": new_account_id,
                "workspace_name": new_workspace_name,
                "role": chosen.get("current_user_role"),
            }
        finally:
            try:
                api.stop()
            except Exception:
                pass

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.get("/api/admin/diagnose")
def get_admin_diagnose():
    """
    用当前管理员 session_token 探测 Team admin 接口,辅助诊断 401/403。
    返回四个关键接口的状态码 + body 前 200 字:
    - /api/auth/session  → access_token 是否拿到
    - /backend-api/me    → 当前登录用户是谁
    - /backend-api/accounts/<id>/settings  → workspace 是否可读
    - /backend-api/accounts/<id>/users     → admin 权限是否生效(真正的 fill-personal 卡点)
    """
    from autoteam.admin_state import get_admin_email, get_chatgpt_account_id
    from autoteam.chatgpt_api import ChatGPTTeamAPI

    def _do():
        # 只读诊断:必须走手动 launch+inject,不调 api.start()——start() 里的
        # _auto_detect_workspace 会写 admin_state,把诊断弄成副作用操作
        from autoteam.admin_state import get_admin_session_token

        api = ChatGPTTeamAPI()
        try:
            api._launch_browser()
            api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            api._wait_for_cloudflare()
            session_token = get_admin_session_token()
            if session_token:
                api.account_id = get_chatgpt_account_id() or ""  # 让 _inject_session 把 _account cookie 带上
                api._inject_session(session_token)
                api.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
                time.sleep(2)
                api._wait_for_cloudflare()
            api._fetch_access_token()
            account_id = api.account_id or get_chatgpt_account_id() or ""
            probes = {}

            session_result = api.page.evaluate(
                "async () => { const r = await fetch('/api/auth/session'); "
                "return { status: r.status, body: (await r.text()).slice(0, 400) }; }"
            )
            probes["auth_session"] = session_result

            for name, path in [
                ("backend_me", "/backend-api/me"),
                ("backend_accounts", "/backend-api/accounts"),
                ("workspace_settings", f"/backend-api/accounts/{account_id}/settings"),
                ("workspace_users", f"/backend-api/accounts/{account_id}/users"),
            ]:
                r = api._api_fetch("GET", path)
                probes[name] = {"status": r.get("status"), "body": (r.get("body") or "")[:500]}

            # Round 8 — SPEC-2 v1.5 §6.2:diagnose 内嵌 master_subscription_state(read-only,5min cache)
            try:
                from autoteam.master_health import is_master_subscription_healthy
                healthy, reason, evidence = is_master_subscription_healthy(api)
                master_state = {
                    "healthy": healthy,
                    "reason": reason,
                    "evidence": evidence,
                }
            except Exception as exc:
                master_state = {
                    "healthy": None,
                    "reason": "probe_exception",
                    "evidence": {"detail": str(exc)[:200]},
                }

            return {
                "admin_email": get_admin_email(),
                "account_id": account_id,
                "access_token_present": bool(api.access_token),
                "access_token_prefix": (api.access_token or "")[:30],
                "probes": probes,
                "master_subscription_state": master_state,
            }
        finally:
            try:
                api.stop()
            except Exception:
                pass

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.get("/api/admin/master-health")
def get_admin_master_health(request: Request):
    """Round 8 — 母号订阅健康度独立端点。

    查询参数:
        force_refresh=1 → 跳过 5min cache 直接重测
    返回 is_master_subscription_healthy() 的完整 (healthy, reason, evidence) 三元组。
    UI 用于"立即重测"按钮。
    """
    from autoteam.chatgpt_api import ChatGPTTeamAPI
    from autoteam.master_health import is_master_subscription_healthy

    force_refresh = str(request.query_params.get("force_refresh", "")).strip().lower() in (
        "1", "true", "yes",
    )

    def _do():
        api = ChatGPTTeamAPI()
        try:
            api.start()
            healthy, reason, evidence = is_master_subscription_healthy(
                api, force_refresh=force_refresh,
            )
            return {
                "healthy": healthy,
                "reason": reason,
                "evidence": evidence,
                "force_refresh": force_refresh,
            }
        finally:
            try:
                api.stop()
            except Exception:
                pass

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.post("/api/admin/reconcile")
def post_admin_reconcile(request: Request):
    """对账 Team 实际成员 vs 本地状态,修复残废 / 错位 / 耗尽未抛弃 / ghost。

    与 /api/admin/diagnose 使用同款鉴权模式(auth_middleware 已处理 API_KEY)。
    查询参数:
        dry_run=1 → 只诊断,不 KICK、不改 accounts.json
    返回 _reconcile_team_members 的完整结果 dict。
    """
    from autoteam.manager import cmd_reconcile

    dry_run = str(request.query_params.get("dry_run", "")).strip().lower() in ("1", "true", "yes")

    def _do():
        return cmd_reconcile(dry_run=dry_run)

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行"))
    try:
        return _pw_executor.run(_do)
    finally:
        _playwright_lock.release()


@app.get("/api/main-codex/status")
def get_main_codex_status():
    """获取主号 Codex 同步状态。"""
    return _main_codex_status()


@app.get("/api/manual-account/status")
def get_manual_account_status():
    """获取手动添加账号状态。"""
    return _manual_account_status()


@app.post("/api/admin/login/start")
def post_admin_login_start(params: AdminEmailParams):
    """开始管理员登录流程。"""
    global _admin_login_api, _admin_login_step

    if _admin_login_api:
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再进行管理员登录")
        )

    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        logger.info("[API] 开始管理员登录: %s", params.email.strip())

        def _do_start(email):
            api = ChatGPTTeamAPI()
            result = api.begin_admin_login(email)
            return api, result

        api, result = _pw_executor.run(_do_start, params.email.strip())
        step = result["step"]
        logger.info("[API] 管理员登录 start 返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            _admin_login_api = api
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            return _set_pending_admin_login(api, step)
        _pw_executor.run(api.stop)
        _playwright_lock.release()
        raise HTTPException(status_code=400, detail=result.get("detail") or "无法识别管理员登录步骤")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员登录 start 失败")
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/session")
def post_admin_login_session(params: AdminSessionParams):
    """手动导入管理员 session_token。"""
    global _admin_login_api, _admin_login_step

    if _admin_login_api:
        post_admin_login_cancel()

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=_current_busy_detail("有任务正在执行，请等待完成后再导入管理员 session_token"),
        )

    try:
        from autoteam.chatgpt_api import ChatGPTTeamAPI

        logger.info("[API] 导入管理员 session_token: %s", params.email.strip())

        def _do_import(email, session_token):
            api = ChatGPTTeamAPI()
            try:
                return api.import_admin_session(email, session_token)
            finally:
                api.stop()

        info = _pw_executor.run(_do_import, params.email.strip(), params.session_token.strip())
        if info.get("session_token") and info.get("account_id"):
            try:
                from autoteam.codex_auth import refresh_main_auth_file

                main_auth = _pw_executor.run(refresh_main_auth_file)
                if main_auth:
                    info["main_auth"] = main_auth
                    logger.info("[API] session_token 导入后已刷新主号认证文件: %s", main_auth.get("auth_file"))
            except Exception as exc:
                info["main_auth_error"] = str(exc)
                logger.warning("[API] session_token 导入完成，但刷新主号认证文件失败: %s", exc)
        _admin_login_api = None
        _admin_login_step = None
        return {"status": "completed", "admin": _admin_status(), "info": info}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 导入管理员 session_token 失败")
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if _playwright_lock.locked():
            _playwright_lock.release()


@app.post("/api/admin/login/password")
def post_admin_login_password(params: AdminPasswordParams):
    """提交管理员密码。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "password_required":
        raise HTTPException(status_code=409, detail="当前没有等待密码的管理员登录流程")

    try:
        logger.info("[API] 提交管理员密码 | current_step=%s", _admin_login_step)
        result = _pw_executor.run(_admin_login_api.submit_admin_password, params.password)
        step = result["step"]
        logger.info("[API] 管理员密码提交返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员密码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员密码提交失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/code")
def post_admin_login_code(params: AdminCodeParams):
    """提交管理员验证码。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "code_required":
        raise HTTPException(status_code=409, detail="当前没有等待验证码的管理员登录流程")

    try:
        logger.info("[API] 提交管理员验证码 | current_step=%s code_len=%d", _admin_login_step, len(params.code.strip()))
        result = _pw_executor.run(_admin_login_api.submit_admin_code, params.code.strip())
        step = result["step"]
        logger.info("[API] 管理员验证码提交返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员验证码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员验证码提交失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/workspace")
def post_admin_login_workspace(params: AdminWorkspaceParams):
    """提交管理员 workspace 选择。"""
    global _admin_login_api, _admin_login_step
    if not _admin_login_api or _admin_login_step != "workspace_required":
        raise HTTPException(status_code=409, detail="当前没有等待组织选择的管理员登录流程")

    try:
        logger.info("[API] 提交管理员 workspace 选择 | option_id=%s", params.option_id)
        result = _pw_executor.run(_admin_login_api.select_workspace_option, params.option_id)
        step = result["step"]
        logger.info("[API] 管理员 workspace 选择返回: step=%s detail=%s", step, result.get("detail"))
        if step == "completed":
            return _finish_admin_login(result)
        if step in ("password_required", "code_required", "workspace_required"):
            _admin_login_step = step
            return {"status": step, "admin": _admin_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "管理员组织选择失败")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[API] 管理员 workspace 选择失败")
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/login/cancel")
def post_admin_login_cancel():
    """取消管理员登录流程。"""
    global _admin_login_api, _admin_login_step
    if _admin_login_api:
        try:
            _pw_executor.run(_admin_login_api.stop)
        except Exception:
            pass
        _admin_login_api = None
        _admin_login_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {"message": "管理员登录已取消", "admin": _admin_status()}


@app.post("/api/admin/logout")
def post_admin_logout():
    """清除已保存的管理员登录态。"""
    from autoteam.admin_state import clear_admin_state

    if _admin_login_api:
        post_admin_login_cancel()
    clear_admin_state()
    return {"message": "管理员登录态已清除", "admin": _admin_status()}


@app.post("/api/main-codex/start")
def post_main_codex_start():
    """开始主号 Codex 登录并同步到 CPA。"""
    global _main_codex_flow, _main_codex_step

    if _main_codex_flow:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()

    from autoteam.codex_auth import get_saved_main_auth_file
    from autoteam.cpa_sync import sync_main_codex_to_cpa

    saved_auth_file = get_saved_main_auth_file()
    if saved_auth_file:
        sync_main_codex_to_cpa(saved_auth_file)
        return {
            "status": "completed",
            "message": "主号 Codex 已同步到 CPA",
            "codex": _main_codex_status(),
            "info": {"auth_file": saved_auth_file},
        }

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再同步主号 Codex")
        )

    try:
        from autoteam.codex_auth import MainCodexSyncFlow

        def _do_start():
            flow = MainCodexSyncFlow()
            result = flow.start()
            return flow, result

        flow, result = _pw_executor.run(_do_start)
        step = result["step"]
        if step == "completed":
            _main_codex_flow = flow
            return _finish_main_codex_sync()
        if step in ("password_required", "code_required"):
            return _set_pending_main_codex_sync(flow, step)
        _pw_executor.run(flow.stop)
        _playwright_lock.release()
        raise HTTPException(status_code=400, detail=result.get("detail") or "无法识别主号 Codex 登录步骤")
    except HTTPException:
        raise
    except Exception as exc:
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/password")
def post_main_codex_password(params: AdminPasswordParams):
    """提交主号 Codex 登录密码。"""
    global _main_codex_flow, _main_codex_step
    if not _main_codex_flow or _main_codex_step != "password_required":
        raise HTTPException(status_code=409, detail="当前没有等待密码的主号 Codex 登录流程")

    try:
        result = _pw_executor.run(_main_codex_flow.submit_password, params.password)
        step = result["step"]
        if step == "completed":
            return _finish_main_codex_sync()
        if step in ("password_required", "code_required"):
            _main_codex_step = step
            return {"status": step, "codex": _main_codex_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "主号 Codex 密码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/code")
def post_main_codex_code(params: AdminCodeParams):
    """提交主号 Codex 登录验证码。"""
    global _main_codex_flow, _main_codex_step
    if not _main_codex_flow or _main_codex_step != "code_required":
        raise HTTPException(status_code=409, detail="当前没有等待验证码的主号 Codex 登录流程")

    try:
        result = _pw_executor.run(_main_codex_flow.submit_code, params.code.strip())
        step = result["step"]
        if step == "completed":
            return _finish_main_codex_sync()
        if step in ("password_required", "code_required"):
            _main_codex_step = step
            return {"status": step, "codex": _main_codex_status()}
        raise HTTPException(status_code=400, detail=result.get("detail") or "主号 Codex 验证码登录失败")
    except HTTPException:
        raise
    except Exception as exc:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/main-codex/cancel")
def post_main_codex_cancel():
    """取消主号 Codex 登录流程。"""
    global _main_codex_flow, _main_codex_step
    if _main_codex_flow:
        try:
            _pw_executor.run(_main_codex_flow.stop)
        except Exception:
            pass
        _main_codex_flow = None
        _main_codex_step = None
        if _playwright_lock.locked():
            _playwright_lock.release()
    return {"message": "主号 Codex 登录已取消", "codex": _main_codex_status()}


@app.post("/api/manual-account/start")
def post_manual_account_start():
    """开始手动添加账号流程，返回 OAuth 链接。"""
    global _manual_account_flow

    if _manual_account_flow:
        try:
            _manual_account_flow.stop()
        except Exception:
            pass
        _manual_account_flow = None

    try:
        from autoteam.manual_account import ManualAccountFlow

        flow = ManualAccountFlow()
        result = flow.start()
        return _set_pending_manual_account_flow(flow, result)
    except HTTPException:
        raise
    except Exception as exc:
        if _manual_account_flow:
            try:
                _manual_account_flow.stop()
            except Exception:
                pass
            _manual_account_flow = None
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/manual-account/callback")
def post_manual_account_callback(params: ManualAccountCallbackParams):
    """提交 OAuth 回调 URL，完成手动添加账号。"""
    global _manual_account_flow
    if not _manual_account_flow:
        raise HTTPException(status_code=409, detail="当前没有等待回调的手动添加账号流程")

    try:
        result = _manual_account_flow.submit_callback(params.redirect_url)
        return _finish_manual_account_flow(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/manual-account/cancel")
def post_manual_account_cancel():
    """取消手动添加账号流程。"""
    global _manual_account_flow
    if _manual_account_flow:
        try:
            _manual_account_flow.stop()
        except Exception:
            pass
        _manual_account_flow = None
    return {"message": "手动添加账号流程已取消", "manual_account": _manual_account_status()}


@app.get("/api/accounts")
def get_accounts():
    """获取所有账号列表"""
    from autoteam.accounts import load_accounts

    accounts = load_accounts()
    return [_sanitize_account(a) for a in accounts]


@app.get("/api/accounts/{email}/codex-auth")
def get_codex_auth(email: str):
    """导出账号的 Codex CLI 格式认证文件（~/.codex/auth.json）"""
    from autoteam.accounts import find_account, load_accounts
    from autoteam.codex_auth import get_saved_main_auth_file

    email = email.strip().lower()
    auth_file = ""

    if _is_main_account_email(email):
        auth_file = get_saved_main_auth_file()
        if not auth_file or not Path(auth_file).exists():
            raise HTTPException(status_code=404, detail="主号没有可导出的认证文件")
    else:
        acc = find_account(load_accounts(), email)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        auth_file = acc.get("auth_file") or ""
        if not auth_file or not Path(auth_file).exists():
            raise HTTPException(status_code=404, detail="该账号没有认证文件")

    auth_data = json.loads(Path(auth_file).read_text())

    # 转换为 Codex CLI 的 auth.json 格式
    codex_auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": auth_data.get("id_token", ""),
            "access_token": auth_data.get("access_token", ""),
            "refresh_token": auth_data.get("refresh_token", ""),
            "account_id": auth_data.get("account_id", ""),
        },
        "last_refresh": auth_data.get("last_refresh", ""),
    }

    return {
        "email": email,
        "codex_auth": codex_auth,
        "hint": "将内容保存到 ~/.codex/auth.json（Linux/macOS）或 %APPDATA%\\codex\\auth.json（Windows）",
    }


@app.get("/api/accounts/active")
def get_active():
    """获取活跃账号"""
    from autoteam.accounts import get_active_accounts

    return [_sanitize_account(a) for a in get_active_accounts()]


@app.get("/api/accounts/standby")
def get_standby():
    """获取待命账号"""
    from autoteam.accounts import get_standby_accounts

    accounts = get_standby_accounts()
    return [_sanitize_account(a) for a in accounts]


@app.delete("/api/accounts/{email}")
def delete_account(email: str):
    """删除本地管理账号及其关联资源。"""
    if not _playwright_lock.acquire(blocking=False):
        running = _tasks.get(_current_task_id, {})
        raise HTTPException(
            status_code=409,
            detail={
                "message": "有任务正在执行，请等待完成后再删除账号",
                "running_task": {
                    "task_id": _current_task_id,
                    "command": running.get("command", "unknown"),
                    "started_at": running.get("started_at"),
                },
            },
        )

    try:
        from autoteam.account_ops import delete_managed_account
        from autoteam.accounts import load_accounts

        if _is_main_account_email(email):
            raise HTTPException(status_code=400, detail="主号不允许删除")

        accounts = load_accounts()
        if not any(a["email"].lower() == email.lower() for a in accounts):
            raise HTTPException(status_code=404, detail="账号不存在")

        cleanup = _pw_executor.run(delete_managed_account, email)
        return {
            "message": "账号删除完成",
            "deleted_email": email,
            "cleanup": cleanup,
        }
    finally:
        _playwright_lock.release()


@app.post("/api/accounts/delete-batch")
def delete_accounts_batch(params: DeleteBatchParams):
    """
    批量删除本地管理账号。整批共享一个 chatgpt_api + mail_client,
    Team 成员/邀请状态只拉一次,CPA 在整批结束后同步一次,避免重复开销。
    """
    from autoteam.account_ops import delete_managed_account, fetch_team_state
    from autoteam.accounts import load_accounts
    from autoteam.chatgpt_api import ChatGPTTeamAPI
    from autoteam.cloudmail import CloudMailClient
    from autoteam.cpa_sync import sync_to_cpa

    raw_emails = [(e or "").strip() for e in (params.emails or [])]
    emails = [e for e in raw_emails if e]
    if not emails:
        raise HTTPException(status_code=400, detail="emails 不能为空")

    # 去重,保留首次出现顺序
    seen = set()
    dedup = []
    for e in emails:
        low = e.lower()
        if low in seen:
            continue
        seen.add(low)
        dedup.append(e)
    emails = dedup

    main_emails = [e for e in emails if _is_main_account_email(e)]
    if main_emails:
        raise HTTPException(status_code=400, detail=f"主号不允许删除: {main_emails}")

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再批量删除"))

    def _run():
        from autoteam.accounts import STATUS_AUTH_INVALID, STATUS_PERSONAL

        accounts = load_accounts()
        existing = {(a.get("email") or "").lower(): a for a in accounts}

        # SPEC-2 §3.5.2 + Round 6 PRD-5 FR-P1.4:批量删除若整批都是 personal/auth_invalid,
        # 整批跳过 ChatGPTTeamAPI 启动 — 这两类不需要 remote_state(成员/邀请),纯本地清理。
        # 关键:bool(targets_in_pool) 守卫空 list,避免 all([]) == True 误判;
        # 同时如果传入 emails 全部不存在(existing 没匹配),也不应短路(走原 chatgpt_api 路径
        # 让循环里给每条返回"账号不存在")。
        targets_in_pool = [
            existing[e.lower()]
            for e in emails
            if e.lower() in existing
        ]
        all_local_only = bool(targets_in_pool) and all(
            (a.get("status") in (STATUS_PERSONAL, STATUS_AUTH_INVALID))
            for a in targets_in_pool
        )

        chatgpt_api = None
        mail_client = None
        results = []
        try:
            remote_state = None
            if not all_local_only:
                # 至少一个号需要 Team 远端同步 — 启动共享 ChatGPTTeamAPI / mail_client
                chatgpt_api = ChatGPTTeamAPI()
                chatgpt_api.start()
                mail_client = CloudMailClient()
                mail_client.login()
                # 整批共享一次 Team 状态快照,避免每个删除都重查一次
                remote_state = fetch_team_state(chatgpt_api)
            else:
                # 全 personal/auth_invalid 路径:cloudmail 仍可能要清理(personal 有 cloudmail_account_id),
                # 但 mail_client 由 delete_managed_account 内部按需懒加载(own_mail_client 路径),
                # 这里不预启动 ChatGPTTeamAPI。
                logger.info(
                    "[批量删除] 整批 %d 个账号均为 personal/auth_invalid,跳过 ChatGPTTeamAPI 启动(FR-P1.4 短路)",
                    len(targets_in_pool),
                )

            for email in emails:
                if email.lower() not in existing:
                    results.append({"email": email, "ok": False, "error": "账号不存在"})
                    if not params.continue_on_error:
                        break
                    continue
                try:
                    cleanup = delete_managed_account(
                        email,
                        chatgpt_api=chatgpt_api,        # all_local_only=True 时为 None
                        mail_client=mail_client,        # 同上,delete_managed_account 内部懒加载
                        remote_state=remote_state,      # 同上
                        sync_cpa_after=False,            # 整批结束后统一同步
                    )
                    results.append({"email": email, "ok": True, "cleanup": cleanup})
                except Exception as exc:
                    logger.error("[批量删除] %s 失败: %s", email, exc)
                    results.append({"email": email, "ok": False, "error": str(exc)})
                    if not params.continue_on_error:
                        break
        finally:
            if chatgpt_api:
                try:
                    chatgpt_api.stop()
                except Exception as exc:
                    logger.debug("[批量删除] 关闭 chatgpt_api 异常: %s", exc)
            try:
                sync_to_cpa()
            except Exception as exc:
                logger.warning("[批量删除] 结尾 sync_to_cpa 失败: %s", exc)

        ok_count = sum(1 for r in results if r["ok"])
        return {
            "results": results,
            "summary": {
                "total": len(emails),
                "ok": ok_count,
                "failed": len(results) - ok_count,
                "skipped": len(emails) - len(results),
            },
        }

    try:
        # 每个账号平均 30s (拉取 team 状态 + kick + delete cloudmail),再给 120s 兜底余量。
        # 若仍超时会抛 TimeoutError,worker 线程会在后台继续跑完,但锁会释放 → 用户可以再提。
        timeout = max(300, 30 * len(emails) + 120)
        return _pw_executor.run_with_timeout(timeout, _run)
    finally:
        _playwright_lock.release()


@app.post("/api/accounts/{email}/kick")
def post_kick_account(email: str):
    """将账号从 Team 中移出，状态变为 standby"""
    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再操作"))

    try:
        from autoteam.accounts import find_account, load_accounts, update_account
        from autoteam.manager import remove_from_team

        email = email.strip().lower()
        if _is_main_account_email(email):
            raise HTTPException(status_code=400, detail="主号不允许移出 Team")
        accounts = load_accounts()
        acc = find_account(accounts, email)
        if not acc:
            raise HTTPException(status_code=404, detail="账号不存在")
        if acc["status"] != "active":
            raise HTTPException(status_code=400, detail=f"账号状态为 {acc['status']}，不是 active")

        def _do_kick():
            from autoteam.chatgpt_api import ChatGPTTeamAPI

            chatgpt = ChatGPTTeamAPI()
            chatgpt.start()
            try:
                return remove_from_team(chatgpt, email)
            finally:
                chatgpt.stop()

        ok = _pw_executor.run(_do_kick)
        if ok:
            update_account(email, status="standby")
            return {"message": f"已将 {email} 移出 Team", "email": email, "status": "standby"}
        raise HTTPException(status_code=500, detail=f"移出 {email} 失败")
    finally:
        _playwright_lock.release()


class LoginAccountParams(BaseModel):
    email: str


@app.post("/api/accounts/login", status_code=202)
def post_account_login(params: LoginAccountParams):
    """触发单个账号的 Codex 登录（后台执行）"""
    from autoteam.accounts import find_account, load_accounts

    email = params.email.strip().lower()
    if _is_main_account_email(email):
        raise HTTPException(status_code=400, detail="主号不属于账号池登录对象")
    accounts = load_accounts()
    acc = find_account(accounts, email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    def _run():
        from autoteam.accounts import STATUS_ACTIVE, STATUS_PERSONAL, update_account
        from autoteam.cloudmail import CloudMailClient
        from autoteam.codex_auth import (
            check_codex_quota,
            login_codex_via_browser,
            quota_result_quota_info,
            quota_result_resets_at,
            save_auth_file,
        )
        from autoteam.invite import RegisterBlocked
        from autoteam.register_failures import record_failure

        # 账号状态决定登录模式：PERSONAL 走 use_personal=True 补个人号 OAuth；其他走 Team 模式
        use_personal = acc.get("status") == STATUS_PERSONAL

        mail_client = CloudMailClient()
        mail_client.login()
        # SPEC-2 §3.5.3 + Round 6 PRD-5 FR-P1.3:RegisterBlocked 转 409 phone_required / register_blocked
        # 注意:此函数在 _run_task 后台线程中运行,raise HTTPException 由 _run_task 转录到 task["error"];
        # task error 字符串携带 "phone_required" / "register_blocked" 关键字供前端解析(api.ts §SPEC-2 §1)
        try:
            bundle = login_codex_via_browser(
                email,
                acc.get("password", ""),
                mail_client=mail_client,
                use_personal=use_personal,
            )
        except RegisterBlocked as blocked:
            if blocked.is_phone:
                record_failure(
                    email,
                    category="oauth_phone_blocked",
                    reason=f"补登录触发 add-phone (step={blocked.step})",
                    step=blocked.step,
                    stage="api_login",
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "phone_required",
                        "step": blocked.step,
                        "reason": blocked.reason,
                    },
                )
            record_failure(
                email,
                category="exception",
                reason=f"补登录意外 RegisterBlocked: {blocked.reason}",
                step=blocked.step,
                stage="api_login",
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "register_blocked",
                    "step": blocked.step,
                    "reason": blocked.reason,
                },
            )
        if bundle:
            auth_file = save_auth_file(bundle)
            update_account(email, auth_file=auth_file, last_active_at=time.time())
            plan_type = (bundle.get("plan_type") or "").lower()

            if use_personal:
                # personal 补登录：不改状态（保持 PERSONAL），只刷新 auth_file
                update_account(email, status=STATUS_PERSONAL)
            elif plan_type == "team":
                from autoteam.admin_state import get_chatgpt_account_id

                update_account(email, status=STATUS_ACTIVE, workspace_account_id=get_chatgpt_account_id() or None)
                token = bundle.get("access_token")
                if token:
                    st, info = check_codex_quota(token)
                    if st == "ok" and isinstance(info, dict):
                        update_account(email, last_quota=info)
                    elif st == "exhausted":
                        quota_info = quota_result_quota_info(info)
                        if quota_info:
                            update_account(email, last_quota=quota_info)
                        update_account(
                            email,
                            status="exhausted",
                            quota_exhausted_at=time.time(),
                            quota_resets_at=quota_result_resets_at(info) or int(time.time() + 18000),
                        )
            # 同步到 CPA
            from autoteam.cpa_sync import sync_to_cpa

            sync_to_cpa()
            return {
                "email": email,
                "plan": bundle.get("plan_type"),
                "auth_file": auth_file,
                "mode": "personal" if use_personal else "team",
            }
        raise RuntimeError(f"Codex 登录失败: {email}")

    task = _start_task(f"login:{email}", _run, {"email": email})
    return task


@app.get("/api/status")
def get_status():
    """获取所有账号状态 + active 账号实时额度"""
    from autoteam.accounts import (
        STATUS_ACTIVE,
        STATUS_AUTH_INVALID,
        STATUS_EXHAUSTED,
        STATUS_ORPHAN,
        STATUS_PENDING,
        STATUS_PERSONAL,
        STATUS_STANDBY,
        load_accounts,
    )
    from autoteam.codex_auth import check_codex_quota, quota_result_quota_info

    accounts = load_accounts()
    quota_cache = {}

    for acc in accounts:
        if acc["status"] not in (STATUS_ACTIVE, STATUS_PERSONAL) and not _is_main_account_email(acc.get("email")):
            continue

        auth_file = _resolve_status_auth_file(acc)
        if not auth_file:
            continue

        try:
            auth_data = json.loads(read_text(Path(auth_file)))
            access_token = auth_data.get("access_token")
            if access_token:
                status, info = check_codex_quota(access_token)
                if status == "ok" and isinstance(info, dict):
                    quota_cache[acc["email"]] = info
                elif status == "exhausted":
                    quota_info = quota_result_quota_info(info)
                    if quota_info:
                        quota_cache[acc["email"]] = quota_info
        except Exception:
            pass

    sanitized_accounts = [_sanitize_account(a, quota_cache.get(a.get("email"))) for a in accounts]

    summary = {
        "active": sum(1 for a in sanitized_accounts if a["status"] == STATUS_ACTIVE),
        "standby": sum(1 for a in sanitized_accounts if a["status"] == STATUS_STANDBY),
        "exhausted": sum(1 for a in sanitized_accounts if a["status"] == STATUS_EXHAUSTED),
        "pending": sum(1 for a in sanitized_accounts if a["status"] == STATUS_PENDING),
        "personal": sum(1 for a in sanitized_accounts if a["status"] == STATUS_PERSONAL),
        "auth_invalid": sum(1 for a in sanitized_accounts if a["status"] == STATUS_AUTH_INVALID),
        "orphan": sum(1 for a in sanitized_accounts if a["status"] == STATUS_ORPHAN),
        "total": len(sanitized_accounts),
    }

    return {
        "accounts": sanitized_accounts,
        "summary": summary,
        "quota_cache": quota_cache,
    }


@app.post("/api/sync")
def post_sync():
    """同步认证文件到 CPA"""
    from autoteam.cpa_sync import sync_to_cpa

    sync_to_cpa()
    return {"message": "同步完成"}


@app.post("/api/sync/from-cpa")
def post_sync_from_cpa():
    """从 CPA 反向同步认证文件到本地。"""
    from autoteam.cpa_sync import sync_from_cpa

    result = sync_from_cpa()
    return {"message": "已从 CPA 同步到本地", "result": result}


@app.get("/api/register-failures")
def get_register_failures_api(limit: int = 50):
    """返回最近的注册/OAuth 失败明细，前端用来展示"为什么账号没生产出来"。"""
    from autoteam.register_failures import count_by_category, list_failures

    return {
        "items": list_failures(limit=max(1, min(limit, 500))),
        "counts": count_by_category(),
    }


@app.get("/api/config/register-domain")
def get_register_domain_api():
    """读取当前子号注册使用的 CloudMail 域名。"""
    from autoteam.config import CLOUDMAIL_DOMAIN
    from autoteam.runtime_config import get, get_register_domain

    override = (get("register_domain") or "").strip()
    return {
        "domain": get_register_domain(),
        "override": override,
        "env_default": (CLOUDMAIL_DOMAIN or "").lstrip("@").strip(),
    }


@app.put("/api/config/register-domain")
def put_register_domain_api(params: RegisterDomainParams):
    """
    更新子号注册域名。verify=True（默认）会试探性调用 CloudMail new_address 验证服务端是否接受此域，
    成功则立即删除探测地址再保存；失败把 CloudMail 原始错误透传给前端。

    SPEC-1 §FR-005 — 与 `/api/mail-provider/probe?step=domain_ownership` 共享回收语义:
    本路径走 `CloudMailClient` 抽象(已对齐 maillab/cf_temp_email),
    `probe.probe_domain_ownership` 走无状态 HTTP 直连,二者最终行为一致(创建 + 立即 DELETE,
    回收失败 leaked_probe 透传)。改 probe 时需同步检查本函数。
    """
    from autoteam.cloudmail import CloudMailClient
    from autoteam.runtime_config import set_register_domain

    cleaned = (params.domain or "").strip().lstrip("@").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="域名不能为空")

    leaked_probe = None
    if params.verify:
        probe_prefix = f"probe{int(time.time())}"
        acct_id = None
        probe_email = None
        try:
            client = CloudMailClient()
            client.login()
            acct_id, probe_email = client.create_temp_email(prefix=probe_prefix, domain=cleaned)
        except Exception as exc:
            # CloudMail 返回 "Invalid domain" 等错误直接透传
            raise HTTPException(status_code=400, detail=f"域名验证失败: {exc}") from exc
        # 探测地址用完立即回收;删除失败也要让前端看到,否则 CloudMail 会积压僵尸地址
        try:
            if acct_id is not None:
                client.delete_account(acct_id)
        except Exception as exc:
            logger.warning("[config] 删除域名探测邮箱失败 (%s, id=%s): %s", probe_email, acct_id, exc)
            leaked_probe = {"email": probe_email, "acct_id": acct_id, "error": str(exc)}

    set_register_domain(cleaned)
    logger.info("[config] register_domain 已切换为 @%s", cleaned)
    resp = {"message": f"注册域名已切换为 @{cleaned}", "domain": cleaned}
    if leaked_probe:
        resp["warning"] = (
            f"域名已保存,但探测邮箱 {leaked_probe['email']} 回收失败,请手动在 CloudMail 删除"
            f" (id={leaked_probe['acct_id']}): {leaked_probe['error']}"
        )
        resp["leaked_probe"] = leaked_probe
    return resp


@app.get("/api/config/preferred-seat-type")
def get_preferred_seat_type_api():
    """SPEC-2 FR-G — 读取邀请席位偏好。"""
    from autoteam.runtime_config import get_preferred_seat_type
    return {"value": get_preferred_seat_type()}


@app.put("/api/config/preferred-seat-type")
def put_preferred_seat_type_api(params: PreferredSeatTypeParams):
    """SPEC-2 FR-G — 切换邀请席位偏好(default | codex)。"""
    from autoteam.runtime_config import set_preferred_seat_type
    val = (params.value or "").strip().lower()
    if val not in ("default", "codex"):
        raise HTTPException(status_code=400, detail="value 必须为 'default' 或 'codex'")
    saved = set_preferred_seat_type(val)
    logger.info("[config] preferred_seat_type 已切换为 %s", saved)
    return {"value": saved, "message": f"邀请席位偏好已设为 {saved}"}


@app.get("/api/config/sync-probe")
def get_sync_probe_api():
    """SPEC-2 FR-E — 读取 sync_account_states 被踢探测的并发/冷却配置。"""
    from autoteam.runtime_config import get_sync_probe_concurrency, get_sync_probe_cooldown_minutes
    return {
        "concurrency": get_sync_probe_concurrency(),
        "cooldown_minutes": get_sync_probe_cooldown_minutes(),
    }


@app.put("/api/config/sync-probe")
def put_sync_probe_api(params: SyncProbeParams):
    """SPEC-2 FR-E — 更新 sync_account_states 被踢探测的并发/冷却(任一非空字段都生效)。"""
    from autoteam.runtime_config import (
        get_sync_probe_concurrency,
        get_sync_probe_cooldown_minutes,
        set_sync_probe_concurrency,
        set_sync_probe_cooldown_minutes,
    )
    if params.concurrency is not None:
        set_sync_probe_concurrency(params.concurrency)
    if params.cooldown_minutes is not None:
        set_sync_probe_cooldown_minutes(params.cooldown_minutes)
    return {
        "concurrency": get_sync_probe_concurrency(),
        "cooldown_minutes": get_sync_probe_cooldown_minutes(),
        "message": "sync 探测配置已更新",
    }


@app.post("/api/sync/accounts")
def post_sync_accounts():
    """从 auths 目录和 Team 成员同步账号到 accounts.json"""
    from autoteam.manager import sync_account_states

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再同步"))

    try:
        _pw_executor.run(sync_account_states)
    finally:
        _playwright_lock.release()

    from autoteam.accounts import load_accounts

    accounts = load_accounts()
    return {"message": f"同步完成，共 {len(accounts)} 个账号", "total": len(accounts)}


@app.get("/api/team/members")
def get_team_members():
    """获取 Team 全部成员（包括手动添加的外部成员）"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id

    if not get_admin_session_token() or not get_chatgpt_account_id():
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再查询"))

    try:

        def _fetch_team_members():
            from autoteam.account_ops import fetch_team_state
            from autoteam.accounts import load_accounts
            from autoteam.chatgpt_api import ChatGPTTeamAPI

            chatgpt = ChatGPTTeamAPI()
            chatgpt.start()
            try:
                members, invites = fetch_team_state(chatgpt)
                local_emails = {a["email"].lower() for a in load_accounts()}

                result = []
                for m in members:
                    email = (m.get("email") or "").lower()
                    result.append(
                        {
                            "email": m.get("email", ""),
                            "role": m.get("role", ""),
                            "user_id": m.get("user_id") or m.get("id", ""),
                            "is_local": email in local_emails,
                            "type": "member",
                        }
                    )
                for inv in invites:
                    email = (inv.get("email_address") or inv.get("email") or "").lower()
                    result.append(
                        {
                            "email": email,
                            "role": inv.get("role", ""),
                            "user_id": inv.get("id", ""),
                            "is_local": email in local_emails,
                            "type": "invite",
                        }
                    )
                return {"members": result, "total": len(members), "invites": len(invites)}
            finally:
                chatgpt.stop()

        return _pw_executor.run(_fetch_team_members)
    finally:
        _playwright_lock.release()


@app.post("/api/team/members/remove")
def post_team_member_remove(params: TeamMemberRemoveParams):
    """移出 Team 成员或取消邀请。"""
    from autoteam.admin_state import get_admin_session_token, get_chatgpt_account_id

    if not get_admin_session_token() or not get_chatgpt_account_id():
        raise HTTPException(status_code=400, detail="请先完成管理员登录")

    if not _playwright_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=_current_busy_detail("有任务正在执行，请等待完成后再操作"))

    try:
        from autoteam.accounts import find_account, load_accounts, update_account

        email = params.email.strip().lower()
        user_id = params.user_id.strip()
        member_type = params.type.strip().lower()

        if not email or not user_id:
            raise HTTPException(status_code=400, detail="缺少必要参数")
        if _is_main_account_email(email):
            raise HTTPException(status_code=400, detail="主号不允许从 Team 成员页移出")
        if member_type not in ("member", "invite"):
            raise HTTPException(status_code=400, detail="无效的成员类型")

        account_id = get_chatgpt_account_id()

        def _do_remove_team_member():
            from autoteam.chatgpt_api import ChatGPTTeamAPI

            chatgpt = ChatGPTTeamAPI()
            chatgpt.start()
            try:
                if member_type == "invite":
                    path = f"/backend-api/accounts/{account_id}/invites/{user_id}"
                    action_text = "取消邀请"
                else:
                    path = f"/backend-api/accounts/{account_id}/users/{user_id}"
                    action_text = "移出 Team"

                result = chatgpt._api_fetch("DELETE", path)
                return result, action_text
            finally:
                chatgpt.stop()

        result, action_text = _pw_executor.run(_do_remove_team_member)
        if result["status"] not in (200, 204):
            raise HTTPException(status_code=500, detail=f"{action_text}失败: HTTP {result['status']}")

        accounts = load_accounts()
        acc = find_account(accounts, email)
        if acc:
            update_account(email, status="standby")

        return {
            "message": f"已{action_text}: {email}",
            "email": email,
            "type": member_type,
        }
    finally:
        _playwright_lock.release()


# ---------------------------------------------------------------------------
# 日志收集
# ---------------------------------------------------------------------------

_log_buffer: list[dict] = []
_LOG_BUFFER_MAX = 500


class _LogCollector(logging.Handler):
    """收集日志到内存 buffer，供前端查询"""

    def emit(self, record):
        entry = {
            "time": record.created,
            "level": record.levelname,
            "message": self.format(record),
        }
        _log_buffer.append(entry)
        if len(_log_buffer) > _LOG_BUFFER_MAX:
            del _log_buffer[: len(_log_buffer) - _LOG_BUFFER_MAX]


_log_collector = _LogCollector()
_log_collector.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_log_collector)


@app.get("/api/logs")
def get_logs(limit: int = 100, since: float = 0):
    """获取最近的日志"""
    if since > 0:
        entries = [e for e in _log_buffer if e["time"] > since]
    else:
        entries = _log_buffer[-limit:]
    return {"logs": entries, "total": len(_log_buffer)}


@app.post("/api/sync/main-codex")
def post_sync_main_codex():
    """兼容旧接口：开始主号 Codex 登录并同步到 CPA。"""
    return post_main_codex_start()


@app.get("/api/cpa/files")
def get_cpa_files():
    """获取 CPA 中的认证文件列表"""
    from autoteam.cpa_sync import list_cpa_files

    return list_cpa_files()


# ---------------------------------------------------------------------------
# 后台任务端点
# ---------------------------------------------------------------------------


class CheckParams(BaseModel):
    include_standby: bool = False  # True 时额外探测 standby 池(限速+24h 去重)


@app.post("/api/tasks/check", status_code=202)
def post_check(params: CheckParams = CheckParams()):
    """检查所有 active 账号额度（后台执行）。include_standby=True 时追加探测 standby 池。"""
    from autoteam.manager import cmd_check

    include_standby = bool(params.include_standby)

    def _run():
        exhausted = cmd_check(include_standby=include_standby)
        return {"exhausted": [a["email"] for a in exhausted]}

    task = _start_task("check", _run, {"include_standby": include_standby})
    return task


@app.post("/api/tasks/rotate", status_code=202)
def post_rotate(params: TaskParams = TaskParams()):
    """智能轮转（后台执行）"""
    from autoteam.manager import cmd_rotate

    task = _start_task("rotate", cmd_rotate, {"target": params.target}, params.target)
    return task


class ReplaceParams(BaseModel):
    email: str
    reason: str = "manual"


@app.post("/api/tasks/replace", status_code=202)
def post_replace(params: ReplaceParams):
    """定点替换一个 Team 子号:kick + 补一个(标准行为:优先 standby 复用,否则新号)。

    失效一个立即轮换一个的手动触发入口,也可由 auto-check 自动调用 cmd_replace_batch。
    """
    from autoteam.manager import cmd_replace_one

    email = (params.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email 不能为空")
    task = _start_task(
        "replace",
        cmd_replace_one,
        {"email": email, "reason": params.reason},
        email,
        params.reason,
    )
    return task


@app.post("/api/tasks/add", status_code=202)
def post_add():
    """添加新账号（后台执行）"""
    from autoteam.manager import cmd_add

    task = _start_task("add", cmd_add, {})
    return task


@app.post("/api/tasks/fill", status_code=202)
def post_fill(params: TaskParams = TaskParams()):
    """补满 Team 成员（后台执行）。leave_workspace=True 时切换为"生产免费号"模式

    fill-personal 模式下额外做一次轻量预检:Team 子号已满 TEAM_SUB_ACCOUNT_HARD_CAP
    则直接返回 409,不启动后台任务(队列化拒绝,Solution C)。本地状态足够用,无需启动
    Playwright 远程查询,避免给前端按错按钮带来额外开销。
    """
    from autoteam.manager import TEAM_SUB_ACCOUNT_HARD_CAP, cmd_fill

    if params.leave_workspace:
        from autoteam.accounts import STATUS_ACTIVE, STATUS_EXHAUSTED, load_accounts

        in_team_local = sum(1 for a in load_accounts() if a.get("status") in (STATUS_ACTIVE, STATUS_EXHAUSTED))
        if in_team_local >= TEAM_SUB_ACCOUNT_HARD_CAP:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Team 子号已满 {in_team_local}/{TEAM_SUB_ACCOUNT_HARD_CAP},"
                    "fill-personal 拒绝执行。请先等子号自然 exhausted 或手动腾位置后再试"
                ),
            )

        # Round 8 — SPEC-2 v1.5 M-T3:fill-personal 入口先验母号订阅(5min cache,read-only)。
        # cancel 状态下整批 fill 必拿 plan_type=team,直接 503 fail-fast 不启动 task。
        try:
            from autoteam.chatgpt_api import ChatGPTTeamAPI
            from autoteam.master_health import is_master_subscription_healthy

            def _probe_master():
                api = ChatGPTTeamAPI()
                try:
                    api.start()
                    return is_master_subscription_healthy(api)
                finally:
                    try:
                        api.stop()
                    except Exception:
                        pass

            if _playwright_lock.acquire(blocking=False):
                try:
                    healthy, reason, evidence = _pw_executor.run(_probe_master)
                finally:
                    _playwright_lock.release()
                if not healthy and reason == "subscription_cancelled":
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "error": "master_subscription_degraded",
                            "reason": reason,
                            "evidence": evidence,
                            "message": (
                                "母号 ChatGPT Team 订阅已 cancel(eligible_for_auto_reactivation=true),"
                                "fill-personal 必拿 plan_type=team。请先续订或更换母号"
                            ),
                        },
                    )
            else:
                # playwright 锁拿不到 → 别阻塞 fill-personal,放行让任务自身的 M-T1 兜底
                pass
        except HTTPException:
            raise
        except Exception:
            # probe 异常按 spec §6.1 不阻塞 — M-T1 在 _run_post_register_oauth 兜底
            pass

    command = "fill-personal" if params.leave_workspace else "fill"
    task = _start_task(
        command,
        cmd_fill,
        {"target": params.target, "leave_workspace": params.leave_workspace},
        params.target,
        leave_workspace=params.leave_workspace,
    )
    return task


@app.post("/api/tasks/cleanup", status_code=202)
def post_cleanup(params: CleanupParams = CleanupParams()):
    """清理多余成员（后台执行）"""
    from autoteam.manager import cmd_cleanup

    task = _start_task("cleanup", cmd_cleanup, {"max_seats": params.max_seats}, params.max_seats)
    return task


@app.get("/api/tasks")
def get_tasks():
    """查看所有任务"""
    sorted_tasks = sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)
    return sorted_tasks


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """查看任务状态"""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/api/tasks/cancel", status_code=202)
def post_task_cancel():
    """
    请求当前正在运行的任务在下一个安全点退出。
    协作式:后台 worker 在每个批次/账号边界检查 cancel_signal.is_cancelled(),
    调用这里后等 10-30s 让当前步骤跑完,任务状态会在 task["status"] 里显示为 "cancelled"。
    """
    from autoteam import cancel_signal

    if not _current_task_id:
        raise HTTPException(status_code=404, detail="当前没有正在运行的任务")
    task = _tasks.get(_current_task_id) or {}
    if task.get("status") not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"任务当前状态 {task.get('status')} 无法取消")
    cancel_signal.request_cancel(f"手动停止 task={_current_task_id[:8]}")
    task["cancel_requested"] = True
    return {
        "message": "已请求中止,等待当前步骤安全退出",
        "task_id": _current_task_id,
        "command": task.get("command"),
    }


# ---------------------------------------------------------------------------
# 后台自动巡检
# ---------------------------------------------------------------------------

from autoteam.config import (
    AUTO_CHECK_INTERVAL as _DEFAULT_INTERVAL,
)
from autoteam.config import (
    AUTO_CHECK_MIN_LOW as _DEFAULT_MIN_LOW,
)
from autoteam.config import (
    AUTO_CHECK_THRESHOLD as _DEFAULT_THRESHOLD,
)

# 运行时可修改的巡检配置
_auto_check_config = {
    "interval": _DEFAULT_INTERVAL,
    "threshold": _DEFAULT_THRESHOLD,
    "min_low": _DEFAULT_MIN_LOW,
}
_auto_check_stop = threading.Event()
_auto_check_restart = threading.Event()  # 配置变更时通知线程重启

# auto-fill watchdog 冷却:防止反复触发 cmd_rotate 导致 OpenAI 对短时间内
# 多次 invite/kick 的子号批量 revoke token。30 分钟内只触发一次,给 OpenAI
# 风控系统冷却时间。0 表示从未触发过。
_auto_fill_last_trigger_ts = 0.0
_AUTO_FILL_COOLDOWN_SECONDS = 1800  # 30 min


def _auto_check_loop():
    """后台巡检线程：定期检查额度，多个账号低于阈值时自动轮转"""
    from autoteam.accounts import STATUS_ACTIVE, load_accounts
    from autoteam.codex_auth import check_codex_quota

    while not _auto_check_stop.is_set():
        cfg = _auto_check_config
        logger.info(
            "[巡检] 等待 %d 分钟后执行下一轮检查（阈值: %d%%, 模式: 任意失效立即 1v1 替换）",
            cfg["interval"] // 60,
            cfg["threshold"],
        )

        # 等待 interval 秒，期间可被 restart 或 stop 唤醒
        _auto_check_restart.clear()
        if _auto_check_stop.wait(cfg["interval"]):
            break
        if _auto_check_restart.is_set():
            continue  # 配置变更，跳到下一轮重新读取配置

        try:
            cfg = _auto_check_config  # 重新读取
            accounts = load_accounts()
            active = [
                a
                for a in accounts
                if a["status"] == STATUS_ACTIVE
                and not _is_main_account_email(a.get("email"))
                and a.get("auth_file")
                and Path(a["auth_file"]).exists()
            ]

            # Watchdog:active 账号数 < TEAM_SUB_ACCOUNT_HARD_CAP 时自动补位。
            # 之前的 `if not active: continue` 在 4 个 active 全 kick 进 standby
            # 之后会让 Team 永远萎缩。但触发频率必须节制 —— OpenAI 对短时间内反复
            # invite/kick 同一批子号会 revoke token(token_revoked 错误),所以加
            # 30 分钟冷却,避免巡检每 5 分钟无脑触发 cmd_rotate 把账号全洗成废号。
            from autoteam.manager import TEAM_SUB_ACCOUNT_HARD_CAP

            global _auto_fill_last_trigger_ts
            if len(active) < TEAM_SUB_ACCOUNT_HARD_CAP:
                now_ts = time.time()
                cooldown_remaining = (_auto_fill_last_trigger_ts + _AUTO_FILL_COOLDOWN_SECONDS) - now_ts
                if cooldown_remaining > 0:
                    logger.info(
                        "[巡检] active=%d < %d,但 auto-fill 冷却中(还剩 %d 分钟)",
                        len(active),
                        TEAM_SUB_ACCOUNT_HARD_CAP,
                        int(cooldown_remaining / 60),
                    )
                    # 冷却期内仍然继续做"低额度替换"(下面的 low_accounts 逻辑),
                    # 只是不触发全量 cmd_rotate
                else:
                    if not _playwright_lock.acquire(blocking=False):
                        logger.info(
                            "[巡检] active=%d < %d 但有任务在跑,本轮先跳过自动补位",
                            len(active),
                            TEAM_SUB_ACCOUNT_HARD_CAP,
                        )
                        continue
                    _playwright_lock.release()
                    logger.warning(
                        "[巡检] active 账号 %d < %d,触发 auto-fill(cmd_rotate 全流程补位)",
                        len(active),
                        TEAM_SUB_ACCOUNT_HARD_CAP,
                    )
                    from autoteam.manager import cmd_rotate

                    try:
                        _start_task(
                            "auto-fill",
                            cmd_rotate,
                            {"target_seats": TEAM_SUB_ACCOUNT_HARD_CAP + 1},
                            TEAM_SUB_ACCOUNT_HARD_CAP + 1,
                        )
                        _auto_fill_last_trigger_ts = now_ts
                    except Exception as e:
                        logger.error("[巡检] auto-fill 启动失败: %s", e)
                    # 触发后本轮不再做"低额度替换",免得跟 cmd_rotate 抢锁
                    continue

            if not active:
                continue

            low_accounts = []
            for acc in active:
                try:
                    auth_data = json.loads(read_text(Path(acc["auth_file"])))
                    access_token = auth_data.get("access_token")
                    if not access_token:
                        continue
                    status, info = check_codex_quota(access_token)
                    if status == "ok" and isinstance(info, dict):
                        remaining = 100 - info.get("primary_pct", 0)
                        if remaining < cfg["threshold"]:
                            low_accounts.append((acc["email"], remaining))
                    elif status == "exhausted":
                        low_accounts.append((acc["email"], 0))
                except Exception:
                    pass

            if low_accounts:
                logger.info(
                    "[巡检] %d 个账号额度不足: %s", len(low_accounts), ", ".join(f"{e}({r}%)" for e, r in low_accounts)
                )

                # 有任务在跑则本轮跳过(下轮再替换,避免重复 kick)
                if not _playwright_lock.acquire(blocking=False):
                    logger.info("[巡检] 有任务正在执行，本轮跳过即时替换")
                    continue
                _playwright_lock.release()

                # 先标记 exhausted,cmd_check 入口的对账在此之后再看到就会补 kick(双保险)。
                # 必须同时写 quota_resets_at —— 否则 get_standby_accounts() 看到 None 就默认
                # _quota_recovered=True,导致后续 rotate/replace 立刻把这个 0% 账号当可复用号
                # 反复 reinvite 进 Team,席位来回洗同一批耗尽账号永远不换新鲜的。
                # 阈值默认 5h(18000s),与 check_codex_quota 无返回 resets_at 时的 fallback 一致。
                from autoteam.accounts import STATUS_EXHAUSTED, update_account

                now_ts = time.time()
                emails_to_replace = []
                for email, remaining in low_accounts:
                    logger.info("[巡检] %s 剩余 %d%%，立即替换", email, remaining)
                    update_account(
                        email,
                        status=STATUS_EXHAUSTED,
                        quota_exhausted_at=now_ts,
                        quota_resets_at=now_ts + 18000,
                    )
                    emails_to_replace.append(email)

                # 失效一个立即轮换一个:逐个 kick+补一个,不等凑 min_low 也不走全量 cmd_rotate。
                # min_low 字段保留作兼容(当前不参与判断),前端可继续配置但无语义效果。
                logger.info("[巡检] 触发即时替换 (%d 个)...", len(emails_to_replace))
                from autoteam.manager import cmd_replace_batch

                try:
                    _start_task(
                        "auto-replace",
                        cmd_replace_batch,
                        {"emails": emails_to_replace, "trigger": "auto-check"},
                        emails_to_replace,
                        "auto-check",
                    )
                except Exception as e:
                    logger.error("[巡检] 即时替换启动失败: %s", e)
            else:
                logger.info("[巡检] 额度正常，无需替换")

        except Exception as e:
            logger.error("[巡检] 巡检异常: %s", e)


class AutoCheckConfig(BaseModel):
    interval: int = 300  # 巡检间隔（秒）
    threshold: int = 10  # 额度阈值（%）
    min_low: int = 2  # 触发轮转的最少账号数


@app.get("/api/config/auto-check")
def get_auto_check_config():
    """获取巡检配置"""
    return _auto_check_config.copy()


@app.put("/api/config/auto-check")
def set_auto_check_config(cfg: AutoCheckConfig):
    """修改巡检配置（运行时生效）"""
    _auto_check_config["interval"] = max(60, cfg.interval)  # 最少 1 分钟
    _auto_check_config["threshold"] = max(1, min(100, cfg.threshold))
    _auto_check_config["min_low"] = max(1, cfg.min_low)
    _auto_check_restart.set()  # 唤醒巡检线程，立即应用新配置
    logger.info(
        "[巡检] 配置已更新: 间隔=%ds 阈值=%d%%（min_low 已废弃,任意失效立即 1v1 替换）",
        _auto_check_config["interval"],
        _auto_check_config["threshold"],
    )
    return _auto_check_config.copy()


# Round 7 P2.4 — startup/shutdown 已迁移到顶部 app_lifespan,这里不再重复挂 handler。


# ---------------------------------------------------------------------------
# 前端静态文件
# ---------------------------------------------------------------------------

DIST_DIR = Path(__file__).parent / "web" / "dist"

if DIST_DIR.exists():
    # Vite 构建的 assets 目录
    assets_dir = DIST_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str):
        """兜底路由：serve 前端 SPA"""
        file = DIST_DIR / path
        if file.is_file() and ".." not in path:
            return FileResponse(str(file))
        return FileResponse(str(DIST_DIR / "index.html"))


class _QuietAccessLog(logging.Filter):
    """过滤前端轮询产生的高频访问日志"""

    _quiet_paths = (
        "/api/status",
        "/api/tasks",
        "/api/config/auto-check",
        "/api/admin/status",
        "/api/main-codex/status",
        "/api/manual-account/status",
        "/api/auth/check",
        "/api/setup/status",
    )

    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self._quiet_paths)


def start_server(host: str = "0.0.0.0", port: int = 8787):
    """启动 API 服务器"""
    import uvicorn

    # 过滤轮询日志，避免刷屏
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLog())
    # 首次启动检查配置
    from autoteam.setup_wizard import check_and_setup

    check_and_setup(interactive=True)

    # 重新读取 API_KEY（可能刚刚被向导写入）
    global API_KEY
    from autoteam.config import API_KEY as _fresh_key

    API_KEY = _fresh_key or os.environ.get("API_KEY", "")
    if API_KEY:
        logger.info("[API] API Key 鉴权已启用")
    else:
        logger.warning("[API] 未设置 API_KEY，所有接口无需认证")
    logger.info("[API] 启动 AutoTeam API 服务器 http://%s:%d", host, port)
    if DIST_DIR.exists():
        logger.info("[API] 前端面板 http://%s:%d", host, port)
    logger.info("[API] API 文档 http://%s:%d/docs", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
