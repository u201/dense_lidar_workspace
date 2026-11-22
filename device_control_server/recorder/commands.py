"""recorder.record command：校验上下文、转发给录制守护进程、映射错误。

handler 不接触 Django request，也不自己抓包或写 sidecar。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from action.registry import CommandError

from . import service

# daemon error code → CommandError code（spec 第 7 节）
CODE_MAP = {
    'recorder_busy': 'recorder_busy',
    'calibration_invalid': 'calibration_invalid',
    'artifact_unavailable': 'artifact_unavailable',
    'config_invalid': 'config_error',
    'lidar_standby': 'device_standby',
    'lidar_unreachable': 'device_unreachable',
    'lidar_response_invalid': 'device_unreachable',
    'capture_tool_missing': 'capture_unavailable',
    'capture_permission_denied': 'capture_unavailable',
    'capture_failed': 'capture_failed',
    'daemon_stopped': 'capture_failed',
}

# 客户端超时在录制时长之外留出的余量：预检 + 工具启动 + 收尾。
TIMEOUT_MARGIN_SECONDS = 30


def _parse_utc(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def _elapsed_seconds(sidecar: dict[str, Any]) -> float | None:
    started = _parse_utc(sidecar.get('started_at'))
    finished = _parse_utc(sidecar.get('finished_at'))
    if started is None or finished is None:
        return None
    return round((finished - started).total_seconds(), 3)


def record(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        cfg = service.load_config()
    except service.RecorderConfigError as exc:
        raise CommandError('config_error', exc.message)

    action = ctx.get('action')
    step_index = ctx.get('step_index')
    step_count = ctx.get('step_count')
    if (not isinstance(action, dict)
            or isinstance(step_index, bool) or not isinstance(step_index, int)
            or isinstance(step_count, bool) or not isinstance(step_count, int)
            or step_count <= 0 or not 0 <= step_index < step_count):
        raise CommandError(
            'invalid_args',
            '缺少 action 执行上下文（step_index / step_count / action 快照）')

    duration = args['duration']
    try:
        sidecar = service.record(
            duration, action, cfg=cfg,
            timeout=duration + TIMEOUT_MARGIN_SECONDS)
    except service.RecorderUnreachableError as exc:
        raise CommandError('recorder_unreachable', exc.message)
    except service.RecorderError as exc:
        raise CommandError(CODE_MAP.get(exc.code, 'recorder_error'),
                           exc.message, exc.detail)

    if sidecar.get('status') != 'succeeded':
        error = sidecar.get('error') or {}
        code = CODE_MAP.get(error.get('code', ''), 'capture_failed')
        raise CommandError(
            code,
            error.get('message') or '录制失败',
            {'record_id': sidecar.get('record_id')})

    return {
        'record_id': sidecar.get('record_id'),
        'pcap_file': sidecar.get('pcap_file'),
        'duration_seconds': sidecar.get('requested_duration_seconds'),
        'elapsed_seconds': _elapsed_seconds(sidecar),
        'sidecar': f"{sidecar.get('record_id')}.record.json",
    }


COMMANDS = {
    'recorder.record': {
        'label': '录制雷达数据',
        'description': '按指定时长录制 UDP 数据并生成 pcap + sidecar',
        'params': [
            {
                'name': 'duration',
                'label': '时长（秒）',
                'type': 'integer',
                'required': True,
                'minimum': 1,
                'maximum': 600,
                'default': 10,
                'help': '录制期间该步骤阻塞，结束后才执行后续步骤',
            },
        ],
        'handler': record,
    },
}
