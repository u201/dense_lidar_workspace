"""Turntable command 声明：参数 schema 与执行 handler 放在一起。

action 页面用 schema 渲染输入控件，执行器用同一份 schema 校验 args。
handler 不接触 Django request，只接收已校验参数与运行上下文。
"""

from typing import Any

from action.registry import CommandError


def standby(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        'mode': args['mode'],
    }


def seconds_per_revolution(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        'seconds_per_revolution': args['seconds_per_revolution'],
    }


COMMANDS = {
    'turntable.standby': {
        'label': '设置转台待机模式',
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
    'turntable.seconds_per_revolution': {
        'label': '设置每转时间',
        'description': '设置每转时间，支持 180 / 240 / 360 秒',
        'params': [
            {
                'name': 'seconds_per_revolution',
                'label': '每转时间',
                'type': 'integer',
                'required': True,
                'choices': [180, 240, 360],
                'choice_labels': {'180': '180 秒', '240': '240 秒', '360': '360 秒'},
                'default': 180,
            },
        ],
        'handler': seconds_per_revolution,
    },
}
