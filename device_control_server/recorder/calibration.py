"""calibration.json 读取与严格校验。

纯库：不依赖 Django，daemon、command、web 共用同一份校验逻辑。
文件与 device_config.json 同级；缺失或非法一律拒绝，不做兜底。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CALIBRATION_PATH = Path(__file__).resolve().parent.parent / 'calibration.json'

SCHEMA_VERSION = 1
REQUIRED_KEYS = ('schema_version', 'alpha1_degrees', 'alpha2_degrees', 'arm_meter')
KNOWN_KEYS = set(REQUIRED_KEYS) | {'calibrated_at'}


class CalibrationError(Exception):
    """标定文件缺失、格式非法或取值越界。"""


def _is_number(value: Any) -> bool:
    """排除 bool：JSON 的 true/false 在 Python 里也是 int 的子类。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _iso_utc(timestamp: float) -> str:
    return (datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .isoformat().replace('+00:00', 'Z'))


def _require_number(values: dict[str, Any], key: str, minimum: float,
                    maximum: float, *, exclusive_min: bool = False) -> None:
    if key not in values:
        raise CalibrationError(f'标定缺少必填键: {key}')
    value = values[key]
    if not _is_number(value):
        raise CalibrationError(f'calibration.{key} 必须是数字')
    if exclusive_min:
        if not minimum < value <= maximum:
            raise CalibrationError(
                f'calibration.{key} 必须满足 {minimum} < x <= {maximum}: {value!r}')
    elif not minimum <= value <= maximum:
        raise CalibrationError(
            f'calibration.{key} 必须满足 {minimum} <= x <= {maximum}: {value!r}')


def validate(values: dict[str, Any]) -> None:
    """按 spec 4.3 校验必填键；额外键保留，不在这里拒绝。"""
    version = values.get('schema_version')
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise CalibrationError('calibration.schema_version 必须为 1')

    _require_number(values, 'alpha1_degrees', -180, 180)
    _require_number(values, 'alpha2_degrees', -180, 180)
    _require_number(values, 'arm_meter', 0, 10, exclusive_min=True)

    if 'calibrated_at' in values and not isinstance(values['calibrated_at'], str):
        raise CalibrationError('calibration.calibrated_at 必须是字符串')


def unknown_keys(values: dict[str, Any]) -> list[str]:
    """返回 spec 之外的键，供 daemon 记 warning；不影响校验结果。"""
    return sorted(set(values) - KNOWN_KEYS)


def load_calibration(path: Path | None = None) -> dict[str, Any]:
    """读取并校验标定文件，返回 {values, mtime, sha256}。

    values 是文件原样的 dict（含额外键），mtime 为 UTC ISO 字符串。
    """
    target = Path(path) if path else CALIBRATION_PATH
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raise CalibrationError(f'标定文件不存在: {target}')
    except OSError as exc:
        raise CalibrationError(f'标定文件读取失败: {target}: {exc}')

    try:
        values = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalibrationError(f'标定文件不是合法 JSON: {exc}')

    if not isinstance(values, dict):
        raise CalibrationError('标定文件顶层必须是对象')

    validate(values)

    try:
        mtime = _iso_utc(target.stat().st_mtime)
    except OSError as exc:
        raise CalibrationError(f'标定文件状态读取失败: {target}: {exc}')

    return {
        'values': values,
        'mtime': mtime,
        'sha256': hashlib.sha256(raw).hexdigest(),
    }
