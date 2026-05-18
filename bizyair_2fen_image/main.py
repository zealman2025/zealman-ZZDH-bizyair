# -*- coding: utf-8 -*-
"""
2分钱1图 BizyAir API 插件

支持的模式（由 UI 下拉选择）：
- gpt2_t2i        : GPT-Image-2 文生图     web_app_id=52330, 节点 56:BizyAir_GPT_IMAGE_2_T2I_API
- gpt2_i2i        : GPT-Image-2 图生图     web_app_id=52304, 节点 55:BizyAir_GPT_IMAGE_2_I2I_API
- nanobanana2_i2i : NanoBanana 2 图生图    web_app_id=47114, 节点 35:BizyAir_NanoBanana2

公共流程：
- 任务：POST /create（X-Bizyair-Task-Async: enable）→ GET /detail 轮询 → GET /outputs 取 object_url
- 图生图：本地图片 → /x/v1/upload/token → 阿里云 OSS 直传（自带 STS 签名）→ /x/v1/input_resource/commit
- 不依赖 oss2/alibabacloud SDK，自行实现 OSS V1 PUT + STS 签名（仅用 requests + hmac）

注册地址：https://bizyair.cn/
"""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3
from PIL import Image

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from plugin_utils import load_plugin_config  # noqa: E402


_PLUGIN_FILE = __file__

# --------- 常量 ---------
_API_BASE = "https://api.bizyair.cn"
_CREATE_URL = f"{_API_BASE}/w/v1/webapp/task/openapi/create"
_DETAIL_URL = f"{_API_BASE}/w/v1/webapp/task/openapi/detail"
_OUTPUTS_URL = f"{_API_BASE}/w/v1/webapp/task/openapi/outputs"
_UPLOAD_TOKEN_URL = f"{_API_BASE}/x/v1/upload/token"
_INPUT_COMMIT_URL = f"{_API_BASE}/x/v1/input_resource/commit"

# —— 模式配置 ——
# 每种模式对应一个 BizyAir AI App（web_app_id）+ 一组 LoadImage 节点 + 一些额外固定字段
# kind:
#   "t2i" = 文生图（不需要参考图）
#   "i2i" = 图生图（按映射顺序填入 LoadImage 节点）
# resolution_case:
#   "lower" → 大小写敏感地传 "1k"/"2k"/"4k"（gpt-image-2 工作流）
#   "upper" → 传 "1K"/"2K"/"4K"（nanobanana 2 工作流）
_MODE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "gpt2_t2i": {
        "label": "GPT-Image-2 文生图",
        "kind": "t2i",
        "web_app_id": 52330,
        "node_prefix": "56:BizyAir_GPT_IMAGE_2_T2I_API",
        "load_image_keys": [],
        "extra_input_values": {},
        "resolution_case": "lower",
    },
    "gpt2_i2i": {
        "label": "GPT-Image-2 图生图",
        "kind": "i2i",
        "web_app_id": 52304,
        "node_prefix": "55:BizyAir_GPT_IMAGE_2_I2I_API",
        "load_image_keys": [
            "40:LoadImage.image",
            "37:LoadImage.image",
            "39:LoadImage.image",
            "46:LoadImage.image",
            "48:LoadImage.image",
            "52:LoadImage.image",
            "58:LoadImage.image",
            "59:LoadImage.image",
            "60:LoadImage.image",
            "61:LoadImage.image",
        ],
        "extra_input_values": {},
        "resolution_case": "lower",
    },
    "nanobanana2_i2i": {
        "label": "NanoBanana 2 图生图",
        "kind": "i2i",
        "web_app_id": 47114,
        "node_prefix": "35:BizyAir_NanoBanana2",
        "load_image_keys": [
            "40:LoadImage.image",
            "37:LoadImage.image",
            "39:LoadImage.image",
            "46:LoadImage.image",
            "48:LoadImage.image",
            "52:LoadImage.image",
            "54:LoadImage.image",
            "55:LoadImage.image",
            "57:LoadImage.image",
            "56:LoadImage.image",
        ],
        # NanoBanana 2 工作流要求固定 mode=third-party
        "extra_input_values": {
            "35:BizyAir_NanoBanana2.mode": "third-party",
        },
        "resolution_case": "upper",
    },
}
_DEFAULT_MODE = "gpt2_t2i"

# 允许的比例 / 分辨率
_ALLOWED_ASPECT = ("1:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9")
_ALLOWED_RESOLUTION = ("1k", "2k", "4k")
# 任何模式下，参考图最多 10 张（所有 i2i 工作流都是 10 个 LoadImage 节点）
_MAX_I2I_INPUTS = 10

_DEFAULT_PARAMS: Dict[str, Any] = {
    "api_key": "",
    "mode": _DEFAULT_MODE,
    "aspect_ratio": "9:16",
    "resolution": "1k",
    "image_input_mappings": [
        {"source": "首帧"},
        {"source": "图1"},
    ],
    "request_timeout": 120,
    "poll_interval_sec": 3,
    "max_wait_sec": 600,
    "suppress_preview_output": True,
}


def _get_mode_config(mode_key: str) -> Dict[str, Any]:
    return _MODE_CONFIGS.get(mode_key) or _MODE_CONFIGS[_DEFAULT_MODE]


# --------- 插件必备 ---------

def get_info():
    return {
        "name": "2分钱1图 BizyAir API 插件",
        "description": (
            "通过 BizyAir 官方 OpenAPI 调用 GPT-Image-2 文生图 / 图生图、NanoBanana 2 图生图。\n"
            "API Key 获取：https://bizyair.cn/"
        ),
        "version": "1.1.0",
        "author": "User",
    }


def get_params() -> Dict[str, Any]:
    params = _DEFAULT_PARAMS.copy()
    cfg = load_plugin_config(_PLUGIN_FILE) or {}
    params.update(cfg)
    # 容错：限制范围
    if params.get("mode") not in _MODE_CONFIGS:
        params["mode"] = _DEFAULT_MODE
    if params.get("aspect_ratio") not in _ALLOWED_ASPECT:
        params["aspect_ratio"] = _DEFAULT_PARAMS["aspect_ratio"]
    if str(params.get("resolution", "")).lower() not in _ALLOWED_RESOLUTION:
        params["resolution"] = _DEFAULT_PARAMS["resolution"]
    else:
        # 统一存为小写；下发时按 mode 决定大小写
        params["resolution"] = str(params["resolution"]).lower()
    return params


# --------- 进度回调 ---------

def _safe_progress(cb, message: str, percent: Optional[int] = None) -> None:
    if cb is None:
        return
    try:
        if percent is not None:
            cb(message, percent)
        else:
            cb(message)
    except TypeError:
        try:
            cb(message)
        except Exception:
            pass
    except Exception:
        pass


# --------- 参考图收集（与 zealman / flux2klein 思路一致） ---------

def _normalize_reference_images(context: dict) -> dict:
    reference_images = context.get("reference_images", {}) or {}

    if reference_images and "参考图片MAP" not in reference_images:
        if all(isinstance(k, int) or (isinstance(k, str) and str(k).isdigit())
               for k in reference_images.keys()):
            reference_images = {"参考图片MAP": dict(reference_images)}

    ref_map_raw = reference_images.get("参考图片MAP")
    if isinstance(ref_map_raw, dict):
        reference_images["参考图片MAP"] = {
            int(k) if isinstance(k, str) and k.isdigit() else k: v
            for k, v in ref_map_raw.items()
        }

    first_frame_path = context.get("first_frame_path")
    end_frame_path = context.get("end_frame_path")
    if first_frame_path:
        reference_images["首帧"] = first_frame_path
    if end_frame_path:
        reference_images["尾帧"] = end_frame_path

    return reference_images


def _get_reference_image_path(reference_images: dict, source: str) -> str:
    src = (source or "").strip()
    if not src:
        return ""
    if src in ("首帧", "首帧图片"):
        return str(reference_images.get("首帧") or "")
    if src in ("尾帧", "尾帧图片"):
        return str(reference_images.get("尾帧") or "")
    # 支持 "图1"~"图10" / "参考图1"~"参考图10"
    for prefix in ("参考图", "图"):
        if src.startswith(prefix):
            tail = src[len(prefix):]
            if tail.isdigit():
                idx = int(tail) - 1
                if idx < 0:
                    return ""
                ref_map = reference_images.get("参考图片MAP", {}) or {}
                return str(ref_map.get(idx) or "")
    return ""


def _collect_mapped_images(params: dict, context: dict) -> List[str]:
    mappings = params.get("image_input_mappings") or []
    reference_images = _normalize_reference_images(context)
    out: List[str] = []
    for m in mappings:
        if not isinstance(m, dict):
            continue
        source = str(m.get("source") or "").strip()
        if not source:
            continue
        p = _get_reference_image_path(reference_images, source)
        if p and os.path.isfile(p) and p not in out:
            out.append(p)
        if len(out) >= _MAX_I2I_INPUTS:
            break
    return out


# --------- BizyAir API：通用 ---------

def _bz_headers(api_key: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _bz_request(method: str, url: str, api_key: str,
                params: Optional[dict] = None,
                json_body: Optional[dict] = None,
                extra_headers: Optional[Dict[str, str]] = None,
                timeout: int = 60) -> dict:
    resp = requests.request(
        method,
        url,
        params=params,
        json=json_body,
        headers=_bz_headers(api_key, extra_headers),
        timeout=timeout,
        verify=False,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"BizyAir API {method} {url} 失败 HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"BizyAir API 响应不是 JSON: {resp.text[:300]}") from e


# --------- 阿里云 OSS V1 简单 PUT（自带 STS 签名） ---------

def _oss_gmt_now() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _oss_put_with_sts(
    *,
    bucket: str,
    endpoint: str,
    object_key: str,
    file_bytes: bytes,
    content_type: str,
    access_key_id: str,
    access_key_secret: str,
    security_token: str,
    timeout: int = 120,
) -> None:
    """
    阿里云 OSS V1 简单上传（含 STS 临时凭证签名）

    string_to_sign = HTTP-Verb + "\n"
                   + Content-MD5 + "\n"
                   + Content-Type + "\n"
                   + Date + "\n"
                   + CanonicalizedOSSHeaders
                   + CanonicalizedResource
    """
    date_str = _oss_gmt_now()
    canonical_oss_headers = f"x-oss-security-token:{security_token}\n"
    canonical_resource = f"/{bucket}/{object_key}"

    string_to_sign = (
        f"PUT\n\n{content_type}\n{date_str}\n"
        f"{canonical_oss_headers}{canonical_resource}"
    )
    signature = base64.b64encode(
        hmac.new(
            access_key_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    authorization = f"OSS {access_key_id}:{signature}"

    url = f"https://{bucket}.{endpoint}/{object_key}"
    headers = {
        "Date": date_str,
        "Content-Type": content_type,
        "Content-Length": str(len(file_bytes)),
        "x-oss-security-token": security_token,
        "Authorization": authorization,
    }
    resp = requests.put(url, data=file_bytes, headers=headers, timeout=timeout)
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"OSS 上传失败 HTTP {resp.status_code}: {resp.text[:500]}"
        )


# --------- BizyAir API：图片上传（token → OSS → commit） ---------

def _guess_content_type(file_path: str) -> str:
    ct, _ = mimetypes.guess_type(file_path)
    if not ct:
        ext = os.path.splitext(file_path)[1].lower()
        ct = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(ext, "application/octet-stream")
    return ct


def _bizyair_upload_image(api_key: str, image_path: str, timeout: int = 120) -> str:
    """把本地图片上传到 BizyAir，返回 storage.bizyair.cn 的 URL（用于工作流 LoadImage 节点）。"""
    if not os.path.isfile(image_path):
        raise RuntimeError(f"图片不存在: {image_path}")

    file_name = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        file_bytes = f.read()
    content_type = _guess_content_type(image_path)

    # 1) 获取上传凭证
    token_resp = _bz_request(
        "GET", _UPLOAD_TOKEN_URL, api_key,
        params={"file_name": file_name, "file_type": "inputs"},
        timeout=timeout,
    )
    data = (token_resp or {}).get("data") or {}
    file_info = (data.get("file") or {})
    storage = (data.get("storage") or {})

    object_key = file_info.get("object_key") or ""
    access_key_id = file_info.get("access_key_id") or ""
    access_key_secret = file_info.get("access_key_secret") or ""
    security_token = file_info.get("security_token") or ""
    endpoint = storage.get("endpoint") or ""
    bucket = storage.get("bucket") or ""

    missing = [k for k, v in {
        "object_key": object_key,
        "access_key_id": access_key_id,
        "access_key_secret": access_key_secret,
        "security_token": security_token,
        "endpoint": endpoint,
        "bucket": bucket,
    }.items() if not v]
    if missing:
        raise RuntimeError(
            f"BizyAir upload/token 响应缺少字段 {missing}，原始响应: {str(token_resp)[:500]}"
        )

    # 2) OSS 直传
    _oss_put_with_sts(
        bucket=bucket,
        endpoint=endpoint,
        object_key=object_key,
        file_bytes=file_bytes,
        content_type=content_type,
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        security_token=security_token,
        timeout=timeout,
    )

    # 3) commit 获取最终 URL
    commit_resp = _bz_request(
        "POST", _INPUT_COMMIT_URL, api_key,
        json_body={"name": file_name, "object_key": object_key},
        timeout=timeout,
    )
    url = ((commit_resp or {}).get("data") or {}).get("url") or ""
    if not url:
        raise RuntimeError(
            f"BizyAir input_resource/commit 未返回 url：{str(commit_resp)[:500]}"
        )
    return url


# --------- BizyAir API：任务 ---------

def _bizyair_create_task(api_key: str,
                         web_app_id: int,
                         input_values: Dict[str, Any],
                         suppress_preview_output: bool,
                         timeout: int) -> str:
    body = {
        "web_app_id": web_app_id,
        "suppress_preview_output": bool(suppress_preview_output),
        "input_values": input_values,
    }
    payload = _bz_request(
        "POST", _CREATE_URL, api_key,
        json_body=body,
        extra_headers={"X-Bizyair-Task-Async": "enable"},
        timeout=timeout,
    )
    request_id = (
        (payload or {}).get("requestId")
        or (payload or {}).get("request_id")
        or ((payload or {}).get("data") or {}).get("request_id")
    )
    if not request_id:
        raise RuntimeError(
            f"BizyAir create 未返回 requestId：{str(payload)[:500]}"
        )
    return str(request_id)


_RUNNING_STATUSES = {"queuing", "preparing", "running"}
_TERMINAL_OK = {"success"}
_TERMINAL_BAD = {"failed", "canceled", "cancelled"}


def _bizyair_poll(api_key: str,
                  request_id: str,
                  poll_interval: float,
                  max_wait: float,
                  request_timeout: int,
                  progress_callback) -> dict:
    started = time.monotonic()
    last_status = ""
    while time.monotonic() - started < max_wait:
        payload = _bz_request(
            "GET", _DETAIL_URL, api_key,
            params={"requestId": request_id},
            timeout=request_timeout,
        )
        data = (payload or {}).get("data") or {}
        status_raw = str(data.get("status") or "").strip()
        status = status_raw.lower()
        if status != last_status:
            print(f"[BizyAir] requestId={request_id} status={status_raw}")
            last_status = status
        if status in _TERMINAL_OK:
            _safe_progress(progress_callback, "任务完成，获取结果", 95)
            return data
        if status in _TERMINAL_BAD:
            # BizyAir 不同接口字段名不一致，全部尝试一遍
            err_candidates = []
            for k in (
                "error_message", "errorMessage", "errMsg", "errmsg",
                "error", "fail_reason", "failReason", "reason",
                "message", "msg", "errCode", "error_code", "code",
            ):
                v = data.get(k)
                if v not in (None, "", 0):
                    err_candidates.append(f"{k}={v}")
            # 把 detail 的全部 keys 打印到控制台，便于后续排查
            try:
                import json as _json
                print(f"[BizyAir] FAIL detail dump = {_json.dumps(data, ensure_ascii=False)[:2000]}")
            except Exception:
                print(f"[BizyAir] FAIL detail dump (raw) = {data!r}")
            err_msg = "; ".join(err_candidates) if err_candidates else status_raw
            raise RuntimeError(f"BizyAir 任务{status_raw}：{err_msg}")
        # running 类
        if status == "queuing":
            qinfo = data.get("queue_info") or {}
            queue_count = qinfo.get("queue_count")
            extra = f"，前面还有 {queue_count} 个任务" if isinstance(queue_count, int) and queue_count >= 0 else ""
            _safe_progress(progress_callback, f"排队中{extra}", None)
        elif status == "preparing":
            _safe_progress(progress_callback, "服务准备中", None)
        elif status in _RUNNING_STATUSES:
            _safe_progress(progress_callback, "生成中", None)
        else:
            _safe_progress(progress_callback, f"状态：{status_raw or '未知'}", None)
        time.sleep(max(0.5, float(poll_interval)))
    raise RuntimeError(f"BizyAir 任务轮询超时（>{max_wait}s）requestId={request_id}")


def _bizyair_fetch_outputs(api_key: str, request_id: str, request_timeout: int) -> List[str]:
    payload = _bz_request(
        "GET", _OUTPUTS_URL, api_key,
        params={"requestId": request_id},
        timeout=request_timeout,
    )
    data = (payload or {}).get("data") or {}
    outputs = data.get("outputs") or []
    urls: List[str] = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        url = str(item.get("object_url") or "").strip()
        if not url:
            continue
        audit_status = item.get("audit_status")
        # 1:未审核, 2:通过, 3:不通过, 4:报错；我们允许除"不通过"以外的都尝试下载
        if audit_status == 3:
            print(f"[BizyAir] 跳过审核不通过的输出: {url}")
            continue
        urls.append(url)
    if not urls:
        # 任务可能 Success 了但 outputs 为空（比如审核全部不通过）
        raise RuntimeError(
            f"BizyAir 任务返回成功但无可用输出图片：{str(payload)[:500]}"
        )
    return urls


# --------- 下载 / 保存 ---------

def _download_bytes(url: str, timeout: int) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; bizyair-plugin/1.0)",
        "Accept": "image/*,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"下载图片失败 HTTP {resp.status_code}: {url}")
    return resp.content


def _save_bytes_as_png(raw: bytes, out_path: str) -> str:
    try:
        img = Image.open(BytesIO(raw))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.save(out_path, "PNG")
    except Exception:
        with open(out_path, "wb") as f:
            f.write(raw)
    return out_path


# --------- 主入口 ---------

def generate(context) -> List[str]:
    print("\n" + "=" * 60)
    print("2分钱1图 BizyAir API 插件开始生成")
    print("=" * 60)

    plugin_params = context.get("plugin_params") or get_params()
    progress_callback = context.get("progress_callback")
    prompt = str(context.get("prompt", "") or "").strip()
    output_dir = context.get("output_dir") or context.get("project_path") or "."
    project_path = context.get("project_path") or output_dir
    unique_name = context.get("unique_name", "bizyair")
    generation_round = int(context.get("generation_round", 0))
    output_position = context.get("output_position") or []
    batch_num = max(1, int(context.get("batch_num", 1)))
    viewer_index = int(context.get("viewer_index", 0))

    api_key = str(plugin_params.get("api_key") or "").strip()
    if not api_key:
        raise Exception("PLUGIN_ERROR:::未设置 BizyAir API Key，请在插件设置中填写（注册：https://bizyair.cn/）")

    mode_key = str(plugin_params.get("mode") or _DEFAULT_MODE)
    if mode_key not in _MODE_CONFIGS:
        mode_key = _DEFAULT_MODE
    mode_cfg = _get_mode_config(mode_key)

    aspect_ratio = str(plugin_params.get("aspect_ratio") or "9:16")
    if aspect_ratio not in _ALLOWED_ASPECT:
        aspect_ratio = "9:16"
    resolution_lower = str(plugin_params.get("resolution") or "1k").lower()
    if resolution_lower not in _ALLOWED_RESOLUTION:
        resolution_lower = "1k"
    # 按 mode 决定 resolution 大小写（gpt-image-2 用 "1k"；nanobanana 2 用 "1K"）
    resolution_value = (
        resolution_lower.upper() if mode_cfg.get("resolution_case") == "upper"
        else resolution_lower
    )
    request_timeout = int(plugin_params.get("request_timeout") or 120)
    poll_interval = float(plugin_params.get("poll_interval_sec") or 3)
    max_wait = float(plugin_params.get("max_wait_sec") or 600)
    suppress_preview = bool(plugin_params.get("suppress_preview_output", True))

    web_app_id = int(mode_cfg["web_app_id"])
    node_prefix = str(mode_cfg["node_prefix"])
    is_i2i = mode_cfg["kind"] == "i2i"

    if is_i2i:
        image_paths = _collect_mapped_images(plugin_params, context)
        if not image_paths:
            raise Exception(
                "PLUGIN_ERROR:::已选择图生图模式但未配置任何参考图映射，请在插件设置里至少映射 1 张参考图"
            )
    else:
        image_paths = []

    print(f"[BizyAir] mode={mode_key} ({mode_cfg.get('label')}), web_app_id={web_app_id}")
    print(f"[BizyAir] aspect={aspect_ratio}, resolution={resolution_value}, "
          f"images={len(image_paths)}, batch={batch_num}")
    print(f"[BizyAir] prompt={prompt[:120]}...")

    os.makedirs(project_path, exist_ok=True)
    generated_files: List[str] = []

    for round_idx in range(batch_num):
        try:
            input_values: Dict[str, Any] = {}

            if is_i2i:
                _safe_progress(progress_callback, "上传参考图到 BizyAir", 5)
                uploaded_urls: List[str] = []
                for ip in image_paths:
                    print(f"[BizyAir] 上传参考图: {ip}")
                    url = _bizyair_upload_image(api_key, ip, timeout=request_timeout)
                    print(f"[BizyAir] 已上传 -> {url}")
                    uploaded_urls.append(url)

                load_image_keys = mode_cfg["load_image_keys"]
                # BizyAir 规则：inputcount=N 时，input_values 必须正好挂 N 个 LoadImage.image，
                # 多余的 key 一个都不能出现（否则任务直接 Failed）。
                # 这里按工作流节点顺序，取前 N 个 LoadImage 槽位填入用户上传的 URL。
                use_count = min(len(uploaded_urls), len(load_image_keys))
                for i in range(use_count):
                    input_values[load_image_keys[i]] = uploaded_urls[i]
                input_values[f"{node_prefix}.inputcount"] = use_count

            input_values[f"{node_prefix}.prompt"] = prompt
            input_values[f"{node_prefix}.aspect_ratio"] = aspect_ratio
            input_values[f"{node_prefix}.resolution"] = resolution_value
            # 模式自带的固定额外字段（例如 NanoBanana2.mode=third-party）
            extras = mode_cfg.get("extra_input_values") or {}
            for k, v in extras.items():
                input_values[k] = v

            _safe_progress(progress_callback, "提交任务", 15)
            try:
                import json as _json
                # 把 input_values 完整打印到控制台（生成失败时可贴出来排查）
                print(
                    f"[BizyAir] 提交 input_values = "
                    f"{_json.dumps(input_values, ensure_ascii=False)[:2000]}"
                )
            except Exception:
                pass
            request_id = _bizyair_create_task(
                api_key, web_app_id, input_values,
                suppress_preview_output=suppress_preview,
                timeout=request_timeout,
            )
            print(f"[BizyAir] 任务已提交 requestId={request_id}")

            _bizyair_poll(
                api_key, request_id,
                poll_interval=poll_interval,
                max_wait=max_wait,
                request_timeout=request_timeout,
                progress_callback=progress_callback,
            )

            urls = _bizyair_fetch_outputs(api_key, request_id, request_timeout=request_timeout)
            _safe_progress(progress_callback, f"下载 {len(urls)} 张图", 95)

            for sub_idx, url in enumerate(urls):
                raw = _download_bytes(url, timeout=request_timeout)
                # 命名跟其他插件保持一致
                position = output_position[round_idx] if round_idx < len(output_position) else round_idx
                suffix = f"_n{sub_idx + 1}" if len(urls) > 1 else ""
                name = f"{viewer_index:04d}_{unique_name}_{generation_round}_{position}{suffix}.png"
                out_path = os.path.join(project_path, name)
                _save_bytes_as_png(raw, out_path)
                generated_files.append(out_path)
                print(f"[BizyAir] [OK] 保存: {out_path}")

            _safe_progress(progress_callback, "完成", 100)
        except RuntimeError as e:
            msg = f"生成失败: {e}"
            print(f"[BizyAir] [FAIL] {msg}")
            raise Exception(f"PLUGIN_ERROR:::{msg}")
        except requests.exceptions.RequestException as e:
            msg = f"网络异常: {e}"
            print(f"[BizyAir] [FAIL] {msg}")
            raise Exception(f"PLUGIN_ERROR:::{msg}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception(f"PLUGIN_ERROR:::未知错误: {type(e).__name__}: {e}")

    print(f"[BizyAir] 共生成 {len(generated_files)} 张图")
    return generated_files


# --------- 启动期初始化（仅日志） ---------
try:
    _init_params = _DEFAULT_PARAMS.copy()
    _init_params.update(load_plugin_config(_PLUGIN_FILE) or {})
    _has_key = bool(str(_init_params.get("api_key", "")).strip())
    print(f"[BizyAir 2分钱1图] 插件初始化完成，API Key: {'已设置' if _has_key else '未设置'}")
except Exception as _init_err:
    print(f"[BizyAir 2分钱1图] 初始化警告: {_init_err}")
