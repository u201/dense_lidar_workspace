"""Command 注册表：显式收集各 app 的 COMMANDS。

设备 app 不反向 import action 的执行逻辑；registry 只提供查找与统一错误类型。
模块列表显式维护，避免 Django app 加载阶段产生 import 副作用。
"""

from functools import lru_cache
from importlib import import_module
from typing import Any

COMMAND_MODULES = (
    'lidar.commands',
    'recorder.commands',
)


class CommandError(Exception):
    """Command 执行失败，code 供页面与后续 worker 分类处理。"""

    def __init__(self, code: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@lru_cache(maxsize=1)
def get_registry() -> dict[str, dict[str, Any]]:
    """返回 command 名到定义的映射，重复名字直接视为配置错误。

    命令可用可选字段 ``abort`` 声明中断命令名；这里在加载时校验目标
    存在，声明写错时启动即失败，避免执行中才发现无法中止。
    """
    registry: dict[str, dict[str, Any]] = {}
    for module_name in COMMAND_MODULES:
        module = import_module(module_name)
        for name, definition in module.COMMANDS.items():
            if name in registry:
                raise RuntimeError(f'command 重复注册: {name}')
            registry[name] = definition

    for name, definition in registry.items():
        abort_name = definition.get('abort')
        if abort_name is None:
            continue
        if not isinstance(abort_name, str) or abort_name not in registry:
            raise RuntimeError(
                f'command {name} 声明的 abort 命令不存在: {abort_name!r}')
    return registry


def get_abort_definition(command_name: str) -> dict[str, Any] | None:
    """按命令名取其中断命令的定义；未声明或声明非法时返回 None。"""
    definition = get_registry().get(command_name)
    if not isinstance(definition, dict):
        return None
    abort_name = definition.get('abort')
    if not isinstance(abort_name, str):
        return None
    return get_registry().get(abort_name)
