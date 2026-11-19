"""Lidar command 声明：参数 schema 与执行 handler 放在一起。

action 页面用 schema 渲染输入控件，执行器用同一份 schema 校验 args。
handler 不接触 Django request，只接收已校验参数与运行上下文。
"""

from typing import Any

from action.registry import CommandError
from lidar.service import (
    LidarArgumentError, LidarConfigError, LidarUnreachableError, LidarResponseError,
    load_config, set_standby_mode,
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
}
