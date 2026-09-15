from __future__ import annotations

import base64
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

# The wheel installs loguru sinks while it is imported. Keep those sinks in a
# throwaway directory, then remove them before handling requests.
_SDK_LOG_DIR = Path(tempfile.mkdtemp(prefix="seedance-sdk-import-"))
os.environ["LOG_DIR"] = str(_SDK_LOG_DIR)
os.environ["LOG_LEVEL"] = "CRITICAL"

from fastapi import FastAPI, File, Form, UploadFile  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from filelock import FileLock  # noqa: E402
from loguru import logger as _sdk_loguru_logger  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402
from starlette.formparsers import MultiPartException  # noqa: E402
from starlette.types import ASGIApp, Message, Receive, Scope, Send  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.rsa import (  # noqa: E402
    RSAPrivateKey,
    RSAPublicKey,
)
from maas_seedance import MaasSeedanceClient  # noqa: E402
from bytedance.volcengine_aicc_sdk import seedance_inference_client as _seedance_inference_client  # noqa: E402
from types import SimpleNamespace  # noqa: E402

# The vendor download path calls traceback.print_exc(), which would otherwise
# expose signed video URLs on stderr. Rebind only that vendor module's symbol.
_seedance_inference_client.traceback = SimpleNamespace(print_exc=lambda: None)


UPSTREAM_BASE_URL = "https://zhenze-huhehaote.cmecloud.cn/api/v3"
MODEL_NAME = "doubao-seedance-2.0"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAX_GENERATION_REQUEST_BYTES = 100 * 1024 * 1024

_SUPPORTED_VIDEO_RATIOS: Tuple[Tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("3:4", 3.0 / 4.0),
    ("4:3", 4.0 / 3.0),
    ("16:9", 16.0 / 9.0),
    ("9:16", 9.0 / 16.0),
    ("21:9", 21.0 / 9.0),
)
_SIZE_PATTERN = re.compile(r"^([1-9][0-9]*)[xX]([1-9][0-9]*)$")
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)

_client: Optional[MaasSeedanceClient] = None
_client_lock = threading.Lock()


class AdapterError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message


class KeyMaterialError(Exception):
    pass




def _silence_sdk_logging() -> None:
    logging.getLogger("MaasSeedanceClient").disabled = True
    _sdk_loguru_logger.remove()
    shutil.rmtree(_SDK_LOG_DIR, ignore_errors=True)


_silence_sdk_logging()


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message}},
        headers={"Cache-Control": "no-store"},
    )


async def _send_error(send: Send, status_code: int, message: str) -> None:
    body = json.dumps(
        {"error": {"message": message}}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _headers(scope: Scope) -> Dict[str, str]:
    return {
        name.decode("latin-1").lower(): value.decode("latin-1").strip()
        for name, value in scope.get("headers", [])
    }


def _local_host(host: str) -> bool:
    try:
        parsed = urlsplit("//" + host)
        if (
            parsed.netloc != host
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            return False
        _ = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return False
    if not hostname:
        return False
    hostname = hostname.lower().rstrip(".")
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (address.version == 4 and hostname == "127.0.0.1") or (
        address.version == 6 and hostname == "::1"
    )


def _same_origin(origin: str, host: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
        and parsed.netloc.lower() == host.lower()
    )


class RequestGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        if path == "/v1" or path.startswith("/v1/"):
            headers = _headers(scope)
            if headers.get("x-canvas-sdk") != "1":
                await _send_error(send, 403, "缺少有效的 X-Canvas-SDK 请求头")
                return
            host = headers.get("host", "")
            if not _local_host(host):
                await _send_error(send, 403, "仅允许本机访问 Seedance 服务")
                return
            origin = headers.get("origin")
            if origin and not _same_origin(origin, host):
                await _send_error(send, 403, "请求来源与主机不匹配")
                return
        await self.app(scope, receive, send)


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method", "").upper() == "POST"
            and scope.get("path", "").rstrip("/") == "/v1/videos"
        ):
            await self.app(scope, receive, send)
            return

        content_length = _headers(scope).get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                await _send_error(send, 400, "Content-Length 请求头无效")
                return
            if declared < 0:
                await _send_error(send, 400, "Content-Length 请求头无效")
                return
            if declared > MAX_GENERATION_REQUEST_BYTES:
                await _send_error(send, 413, "生成请求不能超过 100 MiB")
                return

        total = 0
        exceeded = False
        response_started = False
        replacement_sent = False

        async def limited_send(message: Message) -> None:
            nonlocal response_started, replacement_sent
            if exceeded:
                if message.get("type") == "http.response.start" and not replacement_sent:
                    replacement_sent = True
                    await _send_error(send, 413, "生成请求不能超过 100 MiB")
                return
            response_started = response_started or message.get("type") == "http.response.start"
            await send(message)

        async def limited_receive() -> Message:
            nonlocal total, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > MAX_GENERATION_REQUEST_BYTES:
                    exceeded = True
                    raise MultiPartException("request body exceeds 100 MiB")
            return message

        try:
            await self.app(scope, limited_receive, limited_send)
        except MultiPartException:
            if exceeded and not response_started and not replacement_sent:
                await _send_error(send, 413, "生成请求不能超过 100 MiB")

def _validate_key_pair(public_path: Path, private_path: Path) -> None:
    try:
        if not public_path.is_file() or public_path.is_symlink():
            raise KeyMaterialError("public key is not a regular file")
        if not private_path.is_file() or private_path.is_symlink():
            raise KeyMaterialError("private key is not a regular file")
        public_key = serialization.load_pem_public_key(public_path.read_bytes())
        private_key = serialization.load_pem_private_key(
            private_path.read_bytes(), password=None
        )
    except KeyMaterialError:
        raise
    except Exception as exc:
        raise KeyMaterialError("key files are invalid") from exc
    if not isinstance(public_key, RSAPublicKey) or not isinstance(private_key, RSAPrivateKey):
        raise KeyMaterialError("key files are not RSA keys")
    if public_key.public_numbers() != private_key.public_key().public_numbers():
        raise KeyMaterialError("key files do not match")


def _ensure_video_key_pair(client: MaasSeedanceClient) -> None:
    data_dir = DATA_DIR
    key_dir = data_dir / "seedance-keys"
    public_path = key_dir / "public.pem"
    private_path = key_dir / "private.pem"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(data_dir / ".seedance-keys.lock")):
            if key_dir.exists() or key_dir.is_symlink():
                if key_dir.is_symlink() or not key_dir.is_dir():
                    raise KeyMaterialError("key directory is invalid")
                public_exists = public_path.exists() or public_path.is_symlink()
                private_exists = private_path.exists() or private_path.is_symlink()
                if public_exists != private_exists or not public_exists:
                    raise KeyMaterialError("key pair is incomplete")
                _validate_key_pair(public_path, private_path)
                public_path.chmod(0o644)
                private_path.chmod(0o600)
                client.set_video_file_encrypt_key(str(public_path), str(private_path))
                return

            temporary_dir = Path(tempfile.mkdtemp(prefix=".seedance-keys-", dir=str(data_dir)))
            try:
                client.set_video_file_encrypt_key(
                    str(temporary_dir / "public.pem"),
                    str(temporary_dir / "private.pem"),
                )
                temporary_public = temporary_dir / "public.pem"
                temporary_private = temporary_dir / "private.pem"
                _validate_key_pair(temporary_public, temporary_private)
                temporary_public.chmod(0o644)
                temporary_private.chmod(0o600)
                os.replace(str(temporary_dir), str(key_dir))
                temporary_dir = Path()
            finally:
                if temporary_dir != Path():
                    shutil.rmtree(temporary_dir, ignore_errors=True)
    except KeyMaterialError:
        raise
    except Exception as exc:
        raise KeyMaterialError("key pair could not be prepared") from exc


def _get_sdk_client() -> MaasSeedanceClient:
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("MAAS_API_KEY", "").strip()
    if not api_key:
        raise AdapterError(503, "服务端未配置 MAAS_API_KEY")
    with _client_lock:
        if _client is not None:
            return _client
        api_key = os.environ.get("MAAS_API_KEY", "").strip()
        if not api_key:
            raise AdapterError(503, "服务端未配置 MAAS_API_KEY")
        try:
            client = MaasSeedanceClient(
                maas_base_url=UPSTREAM_BASE_URL,
                maas_api_key=api_key,
                maas_model=MODEL_NAME,
                enable_video_encrypt=True,
            )
            _silence_sdk_logging()
            _ensure_video_key_pair(client)
            _silence_sdk_logging()
            _client = client
            return client
        except KeyMaterialError:
            _silence_sdk_logging()
            raise AdapterError(500, "服务端视频加密密钥不可用") from None
        except Exception:
            _silence_sdk_logging()
            raise AdapterError(502, "上游视频服务初始化失败") from None


def _ratio_from_size(size: str) -> str:
    match = _SIZE_PATTERN.fullmatch(size.strip())
    if not match:
        raise AdapterError(400, "size 必须是宽x高格式，例如 1280x720")
    try:
        width, height = int(match.group(1)), int(match.group(2))
        target = width / height
    except (OverflowError, ValueError):
        raise AdapterError(400, "size 必须是宽x高格式，例如 1280x720") from None
    return min(_SUPPORTED_VIDEO_RATIOS, key=lambda item: abs(item[1] - target))[0]


def _media_data_url(upload: UploadFile, field_name: str) -> str:
    try:
        content = upload.file.read()
    except Exception:
        raise AdapterError(400, f"{field_name} 文件无法读取") from None
    if not content:
        raise AdapterError(400, f"{field_name} 文件不能为空")
    media_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    media_type = media_type or mimetypes.guess_type(upload.filename or "")[0] or "application/octet-stream"
    if not _MEDIA_TYPE_PATTERN.fullmatch(media_type):
        raise AdapterError(400, f"{field_name} 文件类型无效")
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def _validate_task_id(task_id: str) -> str:
    if not task_id or any(ord(char) < 0x20 or char in "/?#\\" for char in task_id):
        raise AdapterError(400, "任务 ID 无效")
    return task_id


def _build_content(
    prompt: str,
    mode: str,
    first_frame: Optional[UploadFile],
    last_frame: Optional[UploadFile],
    images: Sequence[UploadFile],
    videos: Sequence[UploadFile],
    audios: Sequence[UploadFile],
) -> List[Dict[str, Any]]:
    if not prompt.strip():
        raise AdapterError(400, "prompt 不能为空")
    if mode == "frames":
        if images:
            raise AdapterError(400, "frames 模式不能使用 image[]，请使用首尾帧字段")
        if last_frame is not None and first_frame is None:
            raise AdapterError(400, "last_frame 需要同时提供 first_frame")
    elif mode == "reference":
        if first_frame is not None or last_frame is not None:
            raise AdapterError(400, "reference 模式请使用 image[] 参考图")
    else:
        raise AdapterError(400, "mode 必须是 frames 或 reference")

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    if first_frame is not None:
        content.append({
            "type": "image_url",
            "image_url": {"url": _media_data_url(first_frame, "first_frame")},
            "role": "first_frame",
        })
    if last_frame is not None:
        content.append({
            "type": "image_url",
            "image_url": {"url": _media_data_url(last_frame, "last_frame")},
            "role": "last_frame",
        })
    if mode == "reference":
        for upload in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": _media_data_url(upload, "image[]")},
                "role": "reference_image",
            })
    for upload in videos:
        content.append({
            "type": "video_url",
            "video_url": {"url": _media_data_url(upload, "video[]")},
            "role": "reference_video",
        })
    for upload in audios:
        content.append({
            "type": "audio_url",
            "audio_url": {"url": _media_data_url(upload, "audio[]")},
            "role": "reference_audio",
        })
    return content


def _sdk_error() -> AdapterError:
    _silence_sdk_logging()
    return AdapterError(502, "上游视频服务请求失败")


def _query_task(task_id: str) -> Dict[str, Any]:
    task_id = _validate_task_id(task_id)
    client = _get_sdk_client()
    try:
        result = client.query_video_generation_task(task_id)
    except AdapterError:
        raise
    except Exception:
        raise _sdk_error() from None
    _silence_sdk_logging()
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise _sdk_error()
    return result


def _public_task(task_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    raw = result["status"].strip().lower()
    if raw in {"succeeded", "success", "completed"}:
        status = "completed"
    elif raw in {"failed", "error", "cancelled", "canceled", "expired"}:
        status = "failed"
    elif raw in {"queued", "pending", "created", "submitted"}:
        status = "queued"
    else:
        status = "in_progress"
    response: Dict[str, Any] = {"id": task_id, "status": status}
    if status == "failed":
        response["error"] = {"message": "视频生成失败"}
    return response


class _CleanupFileResponse(FileResponse):
    def __init__(self, path: Path, temporary_dir: Path, **kwargs: Any) -> None:
        self._temporary_dir = temporary_dir
        super().__init__(path, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self._temporary_dir, ignore_errors=True)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# Guard runs before the body counter, so an untrusted Host cannot turn a large
# request into a different response path.
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(RequestGuardMiddleware)


@app.exception_handler(AdapterError)
async def adapter_error_handler(_: Any, exc: AdapterError) -> JSONResponse:
    return _error(exc.status_code, exc.message)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_: Any, __: RequestValidationError) -> JSONResponse:
    return _error(400, "请求字段校验失败，请检查必填字段和字段格式")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Any, exc: StarletteHTTPException) -> JSONResponse:
    message = "接口不存在" if exc.status_code == 404 else "不支持的请求方法" if exc.status_code == 405 else "请求无效"
    return _error(exc.status_code, message)


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Any, __: Exception) -> JSONResponse:
    _silence_sdk_logging()
    return _error(500, "服务内部错误")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
def models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "mobilecloud"}],
    }


@app.post("/v1/videos")
def create_video(
    model: str = Form(...),
    prompt: str = Form(...),
    seconds: int = Form(...),
    size: str = Form(...),
    resolution_name: str = Form(...),
    generate_audio: bool = Form(...),
    watermark: bool = Form(...),
    mode: str = Form(...),
    first_frame: Optional[UploadFile] = File(default=None),
    last_frame: Optional[UploadFile] = File(default=None),
    images: Optional[List[UploadFile]] = File(default=None, alias="image[]"),
    videos: Optional[List[UploadFile]] = File(default=None, alias="video[]"),
    audios: Optional[List[UploadFile]] = File(default=None, alias="audio[]"),
) -> Dict[str, str]:
    if model != MODEL_NAME:
        raise AdapterError(400, f"只支持模型 {MODEL_NAME}")
    if seconds <= 0:
        raise AdapterError(400, "seconds 必须是正整数")
    if not isinstance(size, str) or not size.strip():
        raise AdapterError(400, "size 不能为空")
    if not isinstance(resolution_name, str) or not resolution_name.strip():
        raise AdapterError(400, "resolution_name 不能为空")
    if mode not in {"frames", "reference"}:
        raise AdapterError(400, "mode 必须是 frames 或 reference")

    request_data: Dict[str, Any] = {
        "model": MODEL_NAME,
        "content": _build_content(prompt, mode, first_frame, last_frame, images or [], videos or [], audios or []),
        "ratio": _ratio_from_size(size),
        "duration": seconds,
        "resolution": resolution_name.strip(),
        "generate_audio": generate_audio,
        "watermark": watermark,
    }
    try:
        task_id = _get_sdk_client().create_video_generation_task(request_data)
    except AdapterError:
        raise
    except Exception:
        raise _sdk_error() from None
    _silence_sdk_logging()
    if not isinstance(task_id, str) or not task_id:
        raise _sdk_error()
    return {"id": task_id, "status": "queued"}


@app.get("/v1/videos/{task_id}")
def video_status(task_id: str) -> Dict[str, Any]:
    return _public_task(task_id, _query_task(task_id))

@app.get("/v1/videos/{task_id}/content")
def video_content(task_id: str) -> FileResponse:
    task_id = _validate_task_id(task_id)
    public = _public_task(task_id, _query_task(task_id))
    if public["status"] == "failed":
        raise AdapterError(409, "视频生成失败，无法下载")
    if public["status"] != "completed":
        raise AdapterError(409, "视频尚未生成完成")

    temporary_dir = Path(tempfile.mkdtemp(prefix="seedance-download-"))
    video_path = temporary_dir / "video.mp4"
    try:
        success = _get_sdk_client().download_video(task_id, str(video_path))
    except Exception:
        _silence_sdk_logging()
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise _sdk_error() from None
    _silence_sdk_logging()
    if not success or not video_path.is_file():
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise _sdk_error()
    try:
        size = video_path.stat().st_size
    except OSError:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise _sdk_error()
    if size <= 0:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise _sdk_error()
    return _CleanupFileResponse(
        video_path,
        temporary_dir,
        media_type="video/mp4",
        filename="video.mp4",
        headers={"Cache-Control": "no-store"},
    )
