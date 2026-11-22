"""Lidar command 声明：参数 schema 与执行 handler 放在一起。

action 页面用 schema 渲染输入控件，执行器用同一份 schema 校验 args。
handler 不接触 Django request，只接收已校验参数与运行上下文。
"""

from typing import Any

from action.registry import CommandError
from lidar.service import (
    LidarArgumentError, LidarConfigError, LidarUnreachableError, LidarResponseError,
    RPM_TO_SPIN_INDEX, SPIN_RPMS,
    load_config, set_spin_rate, set_standby_mode,
)


def standby(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """设置待机模式，成功后返回设备地址与实际设置值。"""
    try:
        cfg = load_config()
        set_standby_mode(args['mode'], cfg)
    except LidarArgumentError as exc:
        raise CommandError('invalid_args', str(exc))
    except LidarConfigError as exc:
        raise CommandError('config_error', str(exc))
    except LidarUnreachableError as exc:
        raise CommandError('device_unreachable', str(exc))
    except LidarResponseError as exc:
        raise CommandError(
            'device_error',
            str(exc),
            detail={'error_code': exc.error_code, 'body': exc.body},
        )

    return {
        'mode': args['mode'],
        'device': f'{cfg["host"]}:{cfg["port"]}',
    }


def spin(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """设置转速，成功后返回设备地址、rpm 与设备侧索引。"""
    try:
        cfg = load_config()
    except LidarConfigError as exc:
        raise CommandError('config_error', str(exc))

    rpm = args['rpm']
    if isinstance(rpm, bool) or rpm not in SPIN_RPMS:
        raise CommandError('invalid_args', f'转速只能是 300/600/1200: {rpm!r}')

    try:
        set_spin_rate(rpm, cfg)
    except LidarArgumentError as exc:
        raise CommandError('invalid_args', str(exc))
    except LidarUnreachableError as exc:
        raise CommandError('device_unreachable', str(exc))
    except LidarResponseError as exc:
        raise CommandError(
            'device_error',
            str(exc),
            detail={'error_code': exc.error_code, 'body': exc.body},
        )

    return {
        'rpm': rpm,
        'index': RPM_TO_SPIN_INDEX[rpm],
        'device': f'{cfg["host"]}:{cfg["port"]}',
    }


COMMANDS = {
    'lidar.standby': {
        'label': '设置雷达待机模式',
        'description': '0 = In Operation，1 = Standby',
        'params': [
            {
                'name': 'mode',
                'label': '待机模式',
                'type': 'integer',
                'required': True,
                'choices': [0, 1],
                'choice_labels': {'0': 'In Operation', '1': 'Standby'},
                'default': 1,
                'help': '0 开始工作，1 进入待机',
            },
        ],
        'handler': standby,
    },
    'lidar.spin': {
        'label': '设置雷达转速',
        'description': '设置转速，支持 300 / 600 / 1200 rpm',
        'params': [
            {
                'name': 'rpm',
                'label': '转速',
                'type': 'integer',
                'required': True,
                'choices': [300, 600, 1200],
                'choice_labels': {'300': '300 rpm', '600': '600 rpm', '1200': '1200 rpm'},
                'default': 600,
                'help': '仅 XT 系列支持',
            },
        ],
        'handler': spin,
    },
}
