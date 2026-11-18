"""Action 服务：读取/校验 actions.json，并同步执行 action。

本阶段执行器由 Django 请求直接调用，仅面向瞬时 command。
后续 action worker 复用同一注册表、同一定义文件与本执行器，不另写一套。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from django.conf import settings

from .registry import CommandError, get_registry

ACTIONS_PATH = Path(settings.BASE_DIR) / 'actions.json'
ACTION_ID_RE = re.compile(r'^[a-z][a-z0-9_]*$')


def load_actions() -> tuple[list[dict[str, Any]], str | None]:
    """每次请求重新读取定义；单个 action 非法不影响其他 action。"""
    try:
        raw = json.loads(ACTIONS_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return [], f'定义文件不存在: {ACTIONS_PATH}'
    except (OSError, json.JSONDecodeError) as exc:
        return [], f'定义文件读取失败: {exc}'

    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        return [], '定义文件需要 schema_version = 1'
    raw_actions = raw.get('actions')
    if not isinstance(raw_actions, list):
        return [], '定义文件缺少 actions 数组'

    registry = get_registry()
    actions = []
    seen_ids: set[str] = set()
    for index, raw_action in enumerate(raw_actions):
        actions.append(_normalize_action(raw_action, index, registry, seen_ids))
    return actions, None


def _normalize_action(raw_action: Any, index: int,
                      registry: dict[str, dict[str, Any]],
                      seen_ids: set[str]) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(raw_action, dict):
        return {
            'action_id': f'invalid_{index}',
            'title': f'Action #{index + 1}',
            'description': '',
            'steps': [],
            'errors': ['action 必须是对象'],
        }

    raw_id = raw_action.get('action_id')
    action_id = raw_id if isinstance(raw_id, str) else f'invalid_{index}'
    if not isinstance(raw_id, str) or not ACTION_ID_RE.fullmatch(raw_id):
        errors.append('action_id 必须匹配 [a-z][a-z0-9_]*')
    elif raw_id in seen_ids:
        errors.append(f'action_id 重复: {raw_id}')
    else:
        seen_ids.add(raw_id)

    title = raw_action.get('title')
    if not isinstance(title, str) or not title.strip():
        title = action_id
    description = raw_action.get('description')
    if not isinstance(description, str):
        description = ''

    raw_steps = raw_action.get('steps')
    steps: list[dict[str, Any]] = []
    if not isinstance(raw_steps, list) or not raw_steps:
        errors.append('steps 必须是非空数组')
    else:
        for step_index, raw_step in enumerate(raw_steps):
            step, step_errors = _normalize_step(raw_step, step_index, registry)
            steps.append(step)
            errors.extend(step_errors)

    return {
        'action_id': action_id,
        'title': title.strip(),
        'description': description,
        'steps': steps,
        'errors': errors,
    }


def _normalize_step(raw_step: Any, index: int,
                    registry: dict[str, dict[str, Any]]
                    ) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(raw_step, dict):
        return {'command': '', 'args': {}, 'fields': []}, [f'step {index} 必须是对象']

    command = raw_step.get('command')
    if not isinstance(command, str) or command not in registry:
        return (
            {'command': command if isinstance(command, str) else '',
             'args': {}, 'fields': []},
            [f'step {index} 引用了未知 command: {command!r}'],
        )

    definition = registry[command]
    raw_args = raw_step.get('args', {})
    if not isinstance(raw_args, dict):
        return (
            {'command': command, 'args': {}, 'fields': []},
            [f'step {index} 的 args 必须是对象'],
        )

    params = definition.get('params', [])
    params_by_name = {param['name']: param for param in params}
    unknown = sorted(set(raw_args) - set(params_by_name))
    if unknown:
        errors.append(f'step {index} 含未知参数: {", ".join(unknown)}')

    args: dict[str, Any] = {}
    for param in params:
        name = param['name']
        if name in raw_args:
            value = raw_args[name]
        elif 'default' in param:
            value = param['default']
        elif param.get('required'):
            errors.append(f'step {index} 缺少必填参数: {name}')
            continue
        else:
            continue

        normalized, error = _validate_value(param, value)
        if error:
            errors.append(f'step {index} 参数 {name}: {error}')
        else:
            args[name] = normalized

    step = {
        'command': command,
        'args': args,
        'definition': definition,
        'fields': _build_fields(params, args),
    }
    return step, errors


def _validate_value(param: dict[str, Any], value: Any) -> tuple[Any, str | None]:
    """校验 JSON 中已是目标类型的值。"""
    param_type = param.get('type')
    if param_type == 'integer':
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif param_type == 'number':
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif param_type == 'string':
        valid = isinstance(value, str)
    elif param_type == 'boolean':
        valid = isinstance(value, bool)
    else:
        return None, f'不支持的类型: {param_type!r}'

    if not valid:
        return None, f'需要 {param_type}，实际为 {value!r}'
    if 'choices' in param and value not in param['choices']:
        return None, f'只能取 {param["choices"]}'
    if 'minimum' in param and value < param['minimum']:
        return None, f'不能小于 {param["minimum"]}'
    if 'maximum' in param and value > param['maximum']:
        return None, f'不能大于 {param["maximum"]}'
    return value, None


def _build_fields(params: list[dict[str, Any]],
                  args: dict[str, Any]) -> list[dict[str, Any]]:
    """生成模板直接可用的控件模型，默认值来自 action preset。"""
    fields = []
    for param in params:
        value = args.get(param['name'])
        choices = []
        if 'choices' in param:
            labels = param.get('choice_labels', {})
            for choice in param['choices']:
                choices.append({
                    'value': choice,
                    'label': labels.get(str(choice), str(choice)),
                    'selected': choice == value,
                })
        fields.append({
            'name': param['name'],
            'label': param.get('label', param['name']),
            'type': param['type'],
            'help': param.get('help', ''),
            'value': value,
            'choices': choices,
        })
    return fields


def parse_overrides(post: Mapping[str, str]) -> dict[int, dict[str, str]]:
    """把 step_0_mode 形式的表单字段按 step 分组。"""
    overrides: dict[int, dict[str, str]] = {}
    for key, value in post.items():
        if not key.startswith('step_'):
            continue
        parts = key.split('_', 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        overrides.setdefault(int(parts[1]), {})[parts[2]] = value
    return overrides


def execute_action(action_id: str,
                   overrides: dict[int, dict[str, str]] | None = None
                   ) -> dict[str, Any]:
    """同步执行 action，任一步失败即停止并保留错误。"""
    started_at = datetime.now(timezone.utc)
    run_id = f'{action_id}-{started_at:%Y%m%dT%H%M%S%fZ}'
    run = {
        'run_id': run_id,
        'action_id': action_id,
        'status': 'failed',
        'started_at': started_at.isoformat(),
        'finished_at': None,
        'steps': [],
        'error': None,
    }

    actions, file_error = load_actions()
    if file_error:
        run['error'] = {'code': 'actions_file_error', 'message': file_error}
        return _finish_run(run)

    action = next((item for item in actions
                   if item['action_id'] == action_id), None)
    if action is None:
        run['error'] = {'code': 'action_not_found',
                        'message': f'action 不存在: {action_id}'}
        return _finish_run(run)
    if action['errors']:
        run['error'] = {'code': 'invalid_action',
                        'message': '；'.join(action['errors'])}
        return _finish_run(run)

    overrides = overrides or {}
    for index, step in enumerate(action['steps']):
        args = dict(step['args'])
        arg_errors = _apply_overrides(step['definition'], args,
                                      overrides.get(index, {}))
        if arg_errors:
            run['steps'].append({
                'index': index,
                'command': step['command'],
                'args': args,
                'status': 'failed',
                'result': None,
                'error': {'code': 'invalid_args',
                          'message': '；'.join(arg_errors)},
                'duration_ms': 0,
            })
            break

        step_run = {
            'index': index,
            'command': step['command'],
            'args': args,
            'status': 'succeeded',
            'result': None,
            'error': None,
            'duration_ms': 0,
        }
        started = time.monotonic()
        try:
            step_run['result'] = step['definition']['handler'](
                args, {'run_id': run_id, 'action_id': action_id})
        except CommandError as exc:
            step_run['status'] = 'failed'
            step_run['error'] = {
                'code': exc.code,
                'message': exc.message,
                'detail': exc.detail,
            }
        except Exception as exc:  # 防止单个 command 把请求打成 500
            step_run['status'] = 'failed'
            step_run['error'] = {
                'code': 'internal_error',
                'message': f'{type(exc).__name__}: {exc}',
            }
        step_run['duration_ms'] = round((time.monotonic() - started) * 1000)
        run['steps'].append(step_run)
        if step_run['status'] == 'failed':
            break

    if len(run['steps']) == len(action['steps']) and all(
            step['status'] == 'succeeded' for step in run['steps']):
        run['status'] = 'succeeded'
    return _finish_run(run)


def _apply_overrides(definition: dict[str, Any], args: dict[str, Any],
                     raw_overrides: dict[str, str]) -> list[str]:
    """表单值是字符串，先按 schema 转换，再走同一套取值校验。"""
    errors = []
    params_by_name = {param['name']: param
                      for param in definition.get('params', [])}
    for name, raw_value in raw_overrides.items():
        param = params_by_name.get(name)
        if param is None:
            errors.append(f'未知参数: {name}')
            continue
        value, error = _coerce_form_value(param, raw_value)
        if error is None:
            value, error = _validate_value(param, value)
        if error:
            errors.append(f'参数 {name}: {error}')
        else:
            args[name] = value
    return errors


def _coerce_form_value(param: dict[str, Any], raw: str) -> tuple[Any, str | None]:
    try:
        if param['type'] == 'integer':
            return int(raw), None
        if param['type'] == 'number':
            return float(raw), None
        if param['type'] == 'boolean':
            lowered = raw.strip().lower()
            if lowered in ('1', 'true', 'on'):
                return True, None
            if lowered in ('0', 'false', 'off'):
                return False, None
            return None, f'无法转换为 boolean: {raw!r}'
        return raw, None
    except ValueError:
        return None, f'无法转换为 {param["type"]}: {raw!r}'


def _finish_run(run: dict[str, Any]) -> dict[str, Any]:
    run['finished_at'] = datetime.now(timezone.utc).isoformat()
    return run
