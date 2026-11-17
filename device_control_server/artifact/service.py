"""Artifact 服务。

职责：读取 device_config.json 的 artifact 段、解析目录路径、扫描目录汇总用量。
"""

import json
import os
from pathlib import Path
from typing import Any

from django.conf import settings

CONFIG_FILENAME = 'device_config.json'

# blocked_reason 取值
REASON_CONFIG_INVALID = 'config_invalid'
REASON_FOLDER_MISSING = 'folder_missing'
REASON_NOT_READABLE = 'folder_not_readable'
REASON_NOT_WRITABLE = 'folder_not_writable'
REASON_QUOTA_EXCEEDED = 'quota_exceeded'


def load_artifact_config() -> tuple[dict[str, Any], str | None]:
    """每次请求重新读取配置。

    返回 (artifact 段, 错误描述)。失败时返回空段与可读错误，
    由调用方降级为 config_invalid，而不是让请求 500。
    """
    path = Path(settings.BASE_DIR) / CONFIG_FILENAME
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}, f'配置文件不存在: {path}'
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f'配置文件读取失败: {exc}'

    section = raw.get('artifact')
    if not isinstance(section, dict):
        return {}, '配置缺少 artifact 段'
    return section, None


def parse_quota_bytes(value: Any) -> int | None:
    """quota_bytes 必须是正整数，非法返回 None。

    注意排除 bool：JSON 的 true/false 在 Python 里也是 int 的子类。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def resolve_local_folder(raw: Any) -> Path | None:
    """展开 $VAR 与 ~，相对路径锚定 BASE_DIR（不是 cwd）。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    expanded = os.path.expandvars(raw.strip())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    return Path(os.path.normpath(str(path)))


def scan_directory(folder: Path) -> tuple[int, int]:
    """返回 (普通文件数, 总字节数)。跳过符号链接与子目录，不递归，不排除 dotfile。"""
    file_count = 0
    total = 0
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            file_count += 1
    return file_count, total


def get_overview() -> dict[str, Any]:
    """汇总目录路径、可用性、配额与已用量。"""
    section, config_error = load_artifact_config()
    raw_folder = section.get('local_folder')
    quota_bytes = parse_quota_bytes(section.get('quota_bytes'))
    folder = resolve_local_folder(raw_folder)

    exists = folder is not None and folder.is_dir()
    readable = exists and os.access(folder, os.R_OK)
    writable = exists and os.access(folder, os.W_OK)

    file_count = 0
    used_bytes = 0
    if readable:
        file_count, used_bytes = scan_directory(folder)

    error = config_error
    if error is None and folder is None:
        error = '配置缺少 artifact.local_folder'
    elif error is None and quota_bytes is None:
        error = '配置缺少或非法的 artifact.quota_bytes'

    if config_error or folder is None or quota_bytes is None:
        blocked_reason = REASON_CONFIG_INVALID
    elif not exists:
        blocked_reason = REASON_FOLDER_MISSING
    elif not readable:
        blocked_reason = REASON_NOT_READABLE
    elif not writable:
        blocked_reason = REASON_NOT_WRITABLE
    elif used_bytes >= quota_bytes:
        blocked_reason = REASON_QUOTA_EXCEEDED
    else:
        blocked_reason = None

    return {
        'local_folder': str(folder) if folder is not None else None,
        'local_folder_configured': raw_folder if isinstance(raw_folder, str) else None,
        'exists': exists,
        'readable': readable,
        'writable': writable,
        'quota_bytes': quota_bytes,
        'used_bytes': used_bytes,
        'remaining_bytes': quota_bytes - used_bytes if quota_bytes is not None else None,
        'file_count': file_count,
        'recording_enabled': blocked_reason is None,
        'blocked_reason': blocked_reason,
        'error': error,
    }
