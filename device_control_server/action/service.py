"""Action 服务：读取/校验 actions.json，并同步或预约执行 action。

同步执行与预约执行共用 execute_steps，区别只在 steps 的来源：
- 同步执行：actions.json 模板 + 表单覆盖值，即时解析后执行；
- 预约执行：解析后的 steps 快照落盘为实例文件，daemon/actor.py 只读快照。

页面上的时间输入按固定 UTC+8 解释，实例文件里的时间一律存 UTC。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from django.conf import settings

from .registry import CommandError, get_registry

ACTIONS_PATH = Path(settings.BASE_DIR) / 'actions.json'
ACTION_ID_RE = re.compile(r'^[a-z][a-z0-9_]*$')
INSTANCE_ID_RE = re.compile(r'^[a-z][a-z0-9_-]*$')

# datetime-local 输入框按固定 UTC+8 解释，与页面标签一致。
INPUT_TZ = timezone(timedelta(hours=8))
EXPIRE_AFTER_SECONDS = int(getattr(
    settings, 'ACTION_INSTANCE_EXPIRE_SECONDS', 600))


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def iso_utc(value: datetime) -> str:
    """统一落盘格式：UTC ISO 8601，以 Z 结尾。"""
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _parse_scheduled_at(raw: str) -> datetime:
    """把 datetime-local 的墙钟字符串按 UTC+8 转为 UTC。"""
    text = raw.strip()
    if not text:
        raise ValueError('请填写预约时间（UTC+8）')

    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
        try:
            wall_clock = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f'无法识别预约时间: {text!r}')

    scheduled_at = wall_clock.replace(tzinfo=INPUT_TZ).astimezone(timezone.utc)
    if scheduled_at <= datetime.now(timezone.utc):
        raise ValueError('预约时间必须晚于当前时间')
    return scheduled_at


def _format_local(text: str) -> str:
    return parse_utc(text).astimezone(INPUT_TZ).strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------------
# actions.json
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 执行（同步与 actor 共用）
# ---------------------------------------------------------------------------

def execute_action(action_id: str,
                   overrides: dict[int, dict[str, str]] | None = None
                   ) -> dict[str, Any]:
    """同步执行 action，任一步失败即停止并保留错误。"""
    actions, file_error = load_actions()
    if file_error:
        return _failure_run(action_id, 'actions_file_error', file_error)

    action = next((item for item in actions
                   if item['action_id'] == action_id), None)
    if action is None:
        return _failure_run(action_id, 'action_not_found',
                            f'action 不存在: {action_id}')
    if action['errors']:
        return _failure_run(action_id, 'invalid_action',
                            '；'.join(action['errors']))

    steps, arg_error = _resolve_steps(action, overrides or {})
    run = _new_run(action_id)
    succeeded = _run_steps(steps, run, action_id)

    if arg_error and succeeded:
        # 参数出错的步骤之前都执行成功，把该步骤补记为失败，便于页面定位。
        run['steps'].append({
            'index': arg_error['index'],
            'command': arg_error['command'],
            'args': arg_error['args'],
            'status': 'failed',
            'result': None,
            'error': {'code': 'invalid_args',
                      'message': arg_error['message']},
            'duration_ms': 0,
        })
        succeeded = False

    if succeeded and len(run['steps']) == len(action['steps']):
        run['status'] = 'succeeded'
    return _finish_run(run)


def execute_steps(steps: list[dict[str, Any]],
                  action_id: str) -> dict[str, Any]:
    """执行已解析的步骤快照；同步执行与 actor 共用同一份逻辑。"""
    run = _new_run(action_id)
    succeeded = _run_steps(steps, run, action_id)
    if succeeded and len(run['steps']) == len(steps):
        run['status'] = 'succeeded'
    return _finish_run(run)


def execute_instance(instance: dict[str, Any]) -> dict[str, Any]:
    return execute_steps(instance.get('steps', []), instance['action_id'])


def _resolve_steps(action: dict[str, Any],
                   overrides: dict[int, dict[str, str]]
                   ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把模板步骤解析成可执行的 steps 快照，返回首个参数错误。"""
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(action['steps']):
        args = dict(step['args'])
        errors = _apply_overrides(step['definition'], args,
                                  overrides.get(index, {}))
        if errors:
            return steps, {
                'index': index,
                'command': step['command'],
                'args': args,
                'message': '；'.join(errors),
            }
        steps.append({'command': step['command'], 'args': args})
    return steps, None


def _new_run(action_id: str) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    return {
        'run_id': f'{action_id}-{started_at:%Y%m%dT%H%M%S%fZ}',
        'action_id': action_id,
        'status': 'failed',
        'started_at': started_at.isoformat(),
        'finished_at': None,
        'steps': [],
        'error': None,
    }


def _run_steps(steps: list[dict[str, Any]], run: dict[str, Any],
               action_id: str) -> bool:
    registry = get_registry()
    for index, step in enumerate(steps):
        step_run = {
            'index': index,
            'command': step['command'],
            'args': step['args'],
            'status': 'succeeded',
            'result': None,
            'error': None,
            'duration_ms': 0,
        }
        started = time.monotonic()
        try:
            definition = registry.get(step['command'])
            if definition is None:
                raise CommandError('unknown_command',
                                   f'command 已不在注册表: {step["command"]}')
            step_run['result'] = definition['handler'](
                step['args'], {'run_id': run['run_id'], 'action_id': action_id})
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
            return False
    return True


def _failure_run(action_id: str, code: str, message: str) -> dict[str, Any]:
    run = _new_run(action_id)
    run['error'] = {'code': code, 'message': message}
    return _finish_run(run)


def _finish_run(run: dict[str, Any]) -> dict[str, Any]:
    run['finished_at'] = datetime.now(timezone.utc).isoformat()
    return run


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


# ---------------------------------------------------------------------------
# 预约实例
# ---------------------------------------------------------------------------

def _instance_dir() -> Path:
    directory = Path(settings.ACTION_INSTANCE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _instance_path(instance_id: str) -> Path:
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError(f'非法实例 ID: {instance_id!r}')
    return _instance_dir() / f'{instance_id}.json'


def save_instance(instance: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再 os.replace，避免读到半个文件。"""
    path = _instance_path(instance['instance_id'])
    tmp_path = path.with_suffix('.json.tmp')
    tmp_path.write_text(
        json.dumps(instance, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    os.replace(tmp_path, path)


def load_instance(instance_id: str) -> dict[str, Any] | None:
    try:
        path = _instance_path(instance_id)
    except ValueError:
        return None
    try:
        instance = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(instance, dict):
        return None
    # 文件内容与文件名不一致时不能返回：后续 save_instance 会写到另一个文件。
    if instance.get('instance_id') != instance_id:
        return None
    return instance


def iter_instances(status: str | None = None) -> Iterator[dict[str, Any]]:
    """按文件名顺序读取实例；坏文件跳过，不影响其他实例。"""
    for path in sorted(_instance_dir().glob('*.json')):
        try:
            instance = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(instance, dict):
            continue

        # instance_id 必须与文件名一致且合法：actor 保存状态时按 instance_id
        # 定位文件，不一致会写到另一个文件，原文件永远 pending，导致重复执行。
        instance_id = instance.get('instance_id')
        if (not isinstance(instance_id, str)
                or instance_id != path.stem
                or not INSTANCE_ID_RE.fullmatch(instance_id)):
            continue

        if status is not None and instance.get('status') != status:
            continue
        yield instance


def schedule_action(action_id: str, raw_scheduled_at: str,
                    overrides: dict[int, dict[str, str]] | None = None
                    ) -> tuple[dict[str, Any] | None, str | None]:
    """校验模板、参数和时间后，落盘一个 pending 实例。"""
    try:
        scheduled_at = _parse_scheduled_at(raw_scheduled_at)
    except ValueError as exc:
        return None, str(exc)

    actions, file_error = load_actions()
    if file_error:
        return None, file_error

    action = next((item for item in actions
                   if item['action_id'] == action_id), None)
    if action is None:
        return None, f'action 不存在: {action_id}'
    if action['errors']:
        return None, '；'.join(action['errors'])

    steps, arg_error = _resolve_steps(action, overrides or {})
    if arg_error:
        return None, (f'step {arg_error["index"]} 参数错误: '
                      f'{arg_error["message"]}')

    created_at = datetime.now(timezone.utc)
    instance = {
        # 只用数字时间戳：INSTANCE_ID_RE 仅允许小写，避免 ISO 的 T/Z 大写。
        'instance_id': f'{action_id}-{created_at:%Y%m%d%H%M%S%f}',
        'action_id': action_id,
        'title': action['title'],
        'scheduled_at': iso_utc(scheduled_at),
        'expire_after_seconds': EXPIRE_AFTER_SECONDS,
        'status': 'pending',
        'created_at': iso_utc(created_at),
        'started_at': None,
        'finished_at': None,
        'steps': steps,
        'run': None,
        'error': None,
    }
    save_instance(instance)

    result = dict(instance)
    result['scheduled_local'] = _format_local(instance['scheduled_at'])
    return result, None


def list_pending_instances() -> list[dict[str, Any]]:
    """页面展示用：已预约、未执行且未失效，按计划时间排序。"""
    now = datetime.now(timezone.utc)
    pending = []
    for instance in iter_instances('pending'):
        try:
            scheduled_at = parse_utc(instance['scheduled_at'])
            created_at = parse_utc(instance['created_at'])
        except (KeyError, TypeError, ValueError):
            continue
        expire_after = instance.get('expire_after_seconds', EXPIRE_AFTER_SECONDS)
        if now - scheduled_at > timedelta(seconds=expire_after):
            continue
        item = dict(instance)
        item['scheduled_local'] = _format_local(instance['scheduled_at'])
        item['created_local'] = _format_local(instance['created_at'])
        pending.append(item)
    pending.sort(key=lambda item: (parse_utc(item['scheduled_at']),
                                   parse_utc(item['created_at'])))
    return pending


def cancel_instance(instance_id: str) -> str | None:
    """取消尚未执行的预约；返回错误文案，成功返回 None。"""
    instance = load_instance(instance_id)
    if instance is None:
        return '实例不存在'
    if instance.get('status') != 'pending':
        return f'实例状态为 {instance.get("status")}，不能取消'

    instance['status'] = 'cancelled'
    instance['finished_at'] = iso_utc(datetime.now(timezone.utc))
    save_instance(instance)
    return None
