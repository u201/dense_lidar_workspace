"""Artifact 服务。

职责：读取 device_config.json 的 artifact 段、解析目录路径、
一次扫描目录同时产出用量汇总与文件条目，供 htmx 卡片视图使用。

状态判定分两层：
- 基础状态（配置/存在/可读/可写）：O(1)，见 _resolve_state()
- 配额状态（used_bytes/quota_exceeded）：目录已用量没有 O(1) 来源，
  必须扫描，见 get_card()
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


def _resolve_state() -> dict[str, Any]:
    """基础状态判定：配置、路径、可读写。不扫目录。

    用量相关字段（used_bytes/file_count/remaining_bytes）先给默认值，
    quota_exceeded 与 recording_enabled 由 get_card() 在扫描后补齐。
    返回里带 resolve 后的 Path（键 folder），供扫描与将来 Recorder 检查使用。
    """
    section, config_error = load_artifact_config()
    raw_folder = section.get('local_folder')
    quota_bytes = parse_quota_bytes(section.get('quota_bytes'))
    folder = resolve_local_folder(raw_folder)

    exists = folder is not None and folder.is_dir()
    readable = exists and os.access(folder, os.R_OK)
    writable = exists and os.access(folder, os.W_OK)

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
    else:
        blocked_reason = None  # 配额是否超，待扫描后判定

    return {
        'folder': folder,
        'local_folder': str(folder) if folder is not None else None,
        'local_folder_configured': raw_folder if isinstance(raw_folder, str) else None,
        'exists': exists,
        'readable': readable,
        'writable': writable,
        'quota_bytes': quota_bytes,
        'used_bytes': 0,
        'remaining_bytes': quota_bytes,
        'file_count': 0,
        'blocked_reason': blocked_reason,
        'recording_enabled': False,
        'error': error,
    }


def _scan_entries(folder: Path) -> list[dict[str, Any]]:
    """一次 scandir，产出 [{filename, size_bytes, mtime_epoch}]。

    跳过符号链接与子目录，不递归，不排除 dotfile；
    单个条目 stat 失败时跳过，不中断遍历。
    """
    entries: list[dict[str, Any]] = []
    with os.scandir(folder) as it:
        for entry in it:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                'filename': entry.name,
                'size_bytes': stat.st_size,
                'mtime_epoch': int(stat.st_mtime),
            })
    return entries


def get_card() -> dict[str, Any]:
    """htmx 卡片唯一入口：一次扫描同时产出 overview 与 files。

    不变量：used_bytes == Σ files.size_bytes，file_count == len(files)，
    两者来自同一次扫描，录制中也不会互相矛盾。
    """
    state = _resolve_state()
    entries: list[dict[str, Any]] = []

    # 目录不可写时仍展示列表（只读查看 + 下载），仅删除会失败
    if state['readable']:
        try:
            entries = _scan_entries(state['folder'])
        except OSError as exc:
            state['blocked_reason'] = REASON_NOT_READABLE
            state['error'] = f"目录不可读: {state['local_folder']} ({exc})"
            entries = []
        state['used_bytes'] = sum(e['size_bytes'] for e in entries)
        state['file_count'] = len(entries)

    if state['quota_bytes'] is not None:
        state['remaining_bytes'] = state['quota_bytes'] - state['used_bytes']
        if state['blocked_reason'] is None and state['used_bytes'] >= state['quota_bytes']:
            state['blocked_reason'] = REASON_QUOTA_EXCEEDED
    state['recording_enabled'] = state['blocked_reason'] is None

    # mtime 降序，同 mtime 按文件名降序，保证稳定（为将来 offset 分页兜底）
    entries.sort(key=lambda e: (e['mtime_epoch'], e['filename']), reverse=True)
    return {'overview': state, 'files': entries}


def resolve_file(filename: str) -> Path | None:
    """白名单校验文件名并解析为目录内文件路径。

    拒绝一切含路径成分的名字（..、/、\\、绝对路径），
    且目标必须是目录内的普通文件（不是符号链接），否则返回 None。
    """
    if not filename or Path(filename).name != filename:
        return None
    section, config_error = load_artifact_config()
    if config_error is not None:
        return None
    folder = resolve_local_folder(section.get('local_folder'))
    if folder is None:
        return None
    path = folder / filename
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def delete_file(filename: str) -> str | None:
    """删除目录内文件。成功返回 None，失败返回可读错误消息。

    TODO(Recorder 上线后)：当前录制目标文件禁止删除。
    """
    path = resolve_file(filename)
    if path is None:
        return '文件不存在或文件名非法'
    try:
        path.unlink()
    except OSError as exc:
        return f'删除失败: {exc}'
    return None
