"""Action 服务：读取/校验 actions.json，并把执行请求落盘为实例。

立即执行与预约执行只差 scheduled_at：两者都把解析后的 steps 快照写成
实例文件，由 daemon/actor.py 每 tick 推进一步；web 请求只创建实例并
读取状态，不执行 command。

页面上的时间输入按固定 UTC+8 解释，实例文件里的时间一律存 UTC。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
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

# 预约实例配置来自 device_config.json 的 action 段（与 artifact/lidar 同源）。
# 每次调用重新读取，改配置后无需重启 web/actor 即可热生效。
CONFIG_PATH = Path(settings.BASE_DIR) / 'device_config.json'
DEFAULT_INSTANCE_DIR = 'action_instances'
DEFAULT_EXPIRE_SECONDS = 600

# 单步 delay 上限：防止一个等待把 actor 的调度拖得过长。
MAX_DELAY_SECONDS = 600

# 查询 actor /current 的超时：状态接口是附加能力，慢响应不能拖住页面。
CURRENT_TIMEOUT_SECONDS = 0.5

# 实例状态 → 页面文案 / 样式类；未知状态原样显示。
STATUS_LABELS = {
    'pending': '等待执行',
    'running': '执行中',
    'cancelling': '中止中',
    'succeeded': '成功',
    'failed': '失败',
    'expired': '已过期',
    'superseded': '被覆盖',
    'cancelled': '已取消',
    'aborted': '已中止',
}
STATUS_CLASSES = {
    'pending': 'offline',
    'running': 'running',
    'cancelling': 'running',
    'succeeded': 'ok',
    'failed': 'abnormal',
    'expired': 'abnormal',
    'superseded': 'abnormal',
    'cancelled': 'offline',
    'aborted': 'abnormal',
}
TERMINAL_STATUSES = frozenset(
    {'succeeded', 'failed', 'expired', 'superseded', 'cancelled', 'aborted'})

# 执行状态区保留的已完成实例条数；进行中的实例全部展示。
INSTANCE_HISTORY_LIMIT = 20


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
            'cleanup': [],
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

    # cleanup 可选：缺失或空数组都合法，元素结构与 steps 一致。
    cleanup: list[dict[str, Any]] = []
    raw_cleanup = raw_action.get('cleanup')
    if raw_cleanup is not None:
        if not isinstance(raw_cleanup, list):
            errors.append('cleanup 必须是数组')
        else:
            for cleanup_index, raw_step in enumerate(raw_cleanup):
                step, step_errors = _normalize_step(
                    raw_step, cleanup_index, registry)
                cleanup.append(step)
                errors.extend(f'cleanup {error}' for error in step_errors)

    return {
        'action_id': action_id,
        'title': title.strip(),
        'description': description,
        'steps': steps,
        'cleanup': cleanup,
        'errors': errors,
    }


def _normalize_step(raw_step: Any, index: int,
                    registry: dict[str, dict[str, Any]]
                    ) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(raw_step, dict):
        return (
            {'command': '', 'args': {}, 'delay': 0.0, 'fields': []},
            [f'step {index} 必须是对象'],
        )

    command = raw_step.get('command')
    if not isinstance(command, str) or command not in registry:
        return (
            {'command': command if isinstance(command, str) else '',
             'args': {}, 'delay': 0.0, 'fields': []},
            [f'step {index} 引用了未知 command: {command!r}'],
        )

    definition = registry[command]
    raw_args = raw_step.get('args', {})
    if not isinstance(raw_args, dict):
        return (
            {'command': command, 'args': {}, 'delay': 0.0, 'fields': []},
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

    delay, delay_error = _validate_delay_value(raw_step.get('delay', 0))
    if delay_error:
        errors.append(f'step {index} delay: {delay_error}')

    step = {
        'command': command,
        'args': args,
        'delay': delay,
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


def _validate_delay_value(value: Any) -> tuple[float, str | None]:
    """校验 delay：非 bool 的数字，范围 0 ~ MAX_DELAY_SECONDS。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0, 'delay 必须是数字'
    if value < 0:
        return 0.0, 'delay 不能为负'
    if value > MAX_DELAY_SECONDS:
        return 0.0, f'delay 不能超过 {MAX_DELAY_SECONDS} 秒'
    return float(value), None


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
# 执行（actor 逐 tick 调用 execute_step）
# ---------------------------------------------------------------------------

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
        steps.append({'command': step['command'], 'args': args,
                      'delay': step.get('delay', 0)})
    return steps, None


def new_run(action_id: str) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    return {
        'run_id': f'{action_id}-{started_at:%Y%m%dT%H%M%S%fZ}',
        'action_id': action_id,
        'status': 'failed',
        'started_at': started_at.isoformat(),
        'finished_at': None,
        'steps': [],
        'cleanup': [],
        'error': None,
    }


def execute_step(step: dict[str, Any], run: dict[str, Any],
                 action_id: str,
                 execution: dict[str, Any] | None = None,
                 step_index: int | None = None,
                 step_count: int | None = None
                 ) -> dict[str, Any]:
    """执行一步并返回结果；异常统一转成 error，不向外抛。

    step_index / step_count 由调用方按阶段显式传入：steps 阶段用 steps
    的索引与总数，cleanup 阶段用 cleanup 的索引与总数。不传时退化为
    旧行为（index 取 run['steps'] 已完成步数）。

    execution 为执行身份（kind / instance_id / title / scheduled_at /
    已解析 steps）；不传时退化为旧的 {'run_id', 'action_id'} 上下文。
    """
    if step_index is None:
        step_index = len(run['steps'])
    step_run = {
        'index': step_index,
        'command': step['command'],
        'args': step['args'],
        'status': 'succeeded',
        'result': None,
        'error': None,
        'duration_ms': 0,
    }
    started = time.monotonic()
    try:
        definition = get_registry().get(step['command'])
        if definition is None:
            raise CommandError('unknown_command',
                               f'command 已不在注册表: {step["command"]}')
        step_run['result'] = definition['handler'](
            step['args'],
            _build_step_context(step_index, run, action_id, execution,
                                step_count))
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
    return step_run


def _build_step_context(step_index: int, run: dict[str, Any],
                        action_id: str,
                        execution: dict[str, Any] | None,
                        step_count: int | None = None) -> dict[str, Any]:
    """构造传给 command handler 的上下文。

    action 快照里的 steps 是已解析值（含表单覆盖），step_index 在 handler
    执行前确定，不能用 handler 返回后的 run['steps'] 反推。
    """
    if execution is None:
        return {'run_id': run['run_id'], 'action_id': action_id}

    steps = execution.get('steps') or []
    if step_count is None:
        step_count = len(steps)
    return {
        'run_id': run['run_id'],
        'action_id': action_id,
        'step_index': step_index,
        'step_count': step_count,
        'action': {
            'kind': execution.get('kind', 'immediate'),
            'instance_id': execution.get('instance_id'),
            'run_id': run['run_id'],
            'action_id': action_id,
            'title': execution.get('title') or action_id,
            'scheduled_at': execution.get('scheduled_at'),
            'started_at': run['started_at'].replace('+00:00', 'Z'),
            'step_index': step_index,
            'step_count': step_count,
            'steps': [
                {
                    'command': step.get('command'),
                    'args': step.get('args', {}),
                    'delay': step.get('delay', 0),
                }
                for step in steps
            ],
        },
    }


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

class ActionConfigError(Exception):
    """action 段配置缺失或非法。"""


def load_action_config() -> dict[str, Any]:
    """读取 device_config.json 的 action 段。

    action_instance_dir 为相对路径时，按 device_config.json 所在目录解析
    （即 BASE_DIR）；绝对路径直接使用。port 为 actor 状态接口端口，
    缺失或非法时抛 ActionConfigError；其余字段缺失或非法时回退默认值，
    避免单点配置错误导致整个预约功能不可用。
    """
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        raw = {}

    section = raw.get('action') if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        section = {}

    raw_dir = section.get('action_instance_dir', DEFAULT_INSTANCE_DIR)
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raw_dir = DEFAULT_INSTANCE_DIR
    instance_dir = Path(raw_dir)
    if not instance_dir.is_absolute():
        instance_dir = CONFIG_PATH.parent / instance_dir

    expire = section.get('action_instance_expire_seconds',
                         DEFAULT_EXPIRE_SECONDS)
    if isinstance(expire, bool) or not isinstance(expire, int) or expire <= 0:
        expire = DEFAULT_EXPIRE_SECONDS

    http_port = section.get('port')
    if (isinstance(http_port, bool) or not isinstance(http_port, int)
            or not 1 <= http_port <= 65535):
        raise ActionConfigError('action.port 必须是 1-65535 的整数')

    return {
        'instance_dir': instance_dir,
        'expire_seconds': int(expire),
        'http_port': http_port,
    }


def get_expire_after_seconds() -> int:
    return load_action_config()['expire_seconds']


def _instance_dir() -> Path:
    directory = load_action_config()['instance_dir']
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


def select_due_winner(instances: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同一 tick 的到点实例中选创建时间最新者；同一时刻用 instance_id 兜底。"""
    if not instances:
        return None
    return max(instances,
               key=lambda item: (parse_utc(item['created_at']),
                                 item['instance_id']))


def mark_superseded(instance: dict[str, Any],
                    winner: dict[str, Any]) -> None:
    """标记被覆盖的预约；重读文件，只有仍为 pending 才写，避免覆盖取消。"""
    fresh = load_instance(instance['instance_id'])
    if fresh is None or fresh.get('status') != 'pending':
        return
    fresh['status'] = 'superseded'
    fresh['started_at'] = None
    fresh['run'] = None
    fresh['finished_at'] = iso_utc(datetime.now(timezone.utc))
    fresh['error'] = {
        'code': 'superseded',
        'message': (f'被创建时间更晚的到点预约覆盖'
                    f'（{winner["instance_id"]}），未执行'),
    }
    save_instance(fresh)


def _find_action(action_id: str
                 ) -> tuple[dict[str, Any] | None, str | None]:
    actions, file_error = load_actions()
    if file_error:
        return None, file_error
    action = next((item for item in actions
                   if item['action_id'] == action_id), None)
    if action is None:
        return None, f'action 不存在: {action_id}'
    if action['errors']:
        return None, '；'.join(action['errors'])
    return action, None


def _prepare_steps(action: dict[str, Any],
                   overrides: dict[int, dict[str, str]] | None
                   ) -> tuple[list[dict[str, Any]] | None, str | None]:
    steps, arg_error = _resolve_steps(action, overrides or {})
    if arg_error:
        return None, (f'step {arg_error["index"]} 参数错误: '
                      f'{arg_error["message"]}')
    return steps, None


def _prepare_cleanup(action: dict[str, Any]) -> list[dict[str, Any]]:
    """cleanup 只用定义值，不接受表单覆盖。"""
    cleanup, _ = _resolve_steps({'steps': action.get('cleanup') or []}, {})
    return cleanup


def _create_instance(action: dict[str, Any], steps: list[dict[str, Any]],
                     cleanup: list[dict[str, Any]],
                     scheduled_at: datetime, kind: str,
                     created_at: datetime | None = None) -> dict[str, Any]:
    created_at = created_at or datetime.now(timezone.utc)
    instance = {
        # 只用数字时间戳：INSTANCE_ID_RE 仅允许小写，避免 ISO 的 T/Z 大写。
        'instance_id': f'{action["action_id"]}-{created_at:%Y%m%d%H%M%S%f}',
        'action_id': action['action_id'],
        'title': action['title'],
        'kind': kind,
        'scheduled_at': iso_utc(scheduled_at),
        'expire_after_seconds': get_expire_after_seconds(),
        'status': 'pending',
        'created_at': iso_utc(created_at),
        'started_at': None,
        'finished_at': None,
        'steps': steps,
        'cleanup': cleanup,
        'run': None,
        'error': None,
    }
    save_instance(instance)
    return instance


def submit_action(action_id: str,
                  overrides: dict[int, dict[str, str]] | None = None
                  ) -> tuple[dict[str, Any] | None, str | None]:
    """立即执行：创建 scheduled_at=now 的实例，由 actor 下一 tick 启动。"""
    action, error = _find_action(action_id)
    if error:
        return None, error
    steps, error = _prepare_steps(action, overrides)
    if error:
        return None, error
    now = datetime.now(timezone.utc)
    return _create_instance(action, steps, _prepare_cleanup(action),
                            now, 'immediate', now), None


def schedule_action(action_id: str, raw_scheduled_at: str,
                    overrides: dict[int, dict[str, str]] | None = None
                    ) -> tuple[dict[str, Any] | None, str | None]:
    """校验模板、参数和时间后，落盘一个 pending 实例。"""
    try:
        scheduled_at = _parse_scheduled_at(raw_scheduled_at)
    except ValueError as exc:
        return None, str(exc)

    action, error = _find_action(action_id)
    if error:
        return None, error
    steps, error = _prepare_steps(action, overrides)
    if error:
        return None, error

    instance = _create_instance(action, steps, _prepare_cleanup(action),
                                scheduled_at, 'scheduled')
    result = dict(instance)
    result['scheduled_local'] = _format_local(instance['scheduled_at'])
    return result, None


# ---------------------------------------------------------------------------
# 执行状态视图（web 展示；Phase 1 不改 actor）
# ---------------------------------------------------------------------------

def current_snapshot() -> dict[str, Any] | None:
    """读取 actor 的 /current；未启动、超时或响应异常都返回 None。"""
    try:
        port = load_action_config()['http_port']
    except ActionConfigError:
        return None
    try:
        with urllib.request.urlopen(
                f'http://127.0.0.1:{port}/current',
                timeout=CURRENT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _live_matches(live: dict[str, Any] | None,
                  instance: dict[str, Any]) -> bool:
    return (isinstance(live, dict)
            and live.get('running') is True
            and live.get('instance_id') == instance.get('instance_id'))


def _local_or_none(text: Any) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return _format_local(text)
    except (TypeError, ValueError):
        return None


def _failure_text(instance: dict[str, Any],
                  run: dict[str, Any]) -> str | None:
    """实例错误优先，其次最后一个失败步骤，最后 run.error。"""
    error = instance.get('error')
    if isinstance(error, dict) and error.get('message'):
        return f'{error.get("code", "error")}: {error["message"]}'

    for step in reversed(run.get('steps') or []):
        step_error = step.get('error')
        if step.get('status') == 'failed' and isinstance(step_error, dict):
            return (f'步骤 {step.get("index")} {step.get("command")}: '
                    f'{step_error.get("code", "error")}: '
                    f'{step_error.get("message", "")}')

    run_error = run.get('error')
    if isinstance(run_error, dict) and run_error.get('message'):
        return f'{run_error.get("code", "error")}: {run_error["message"]}'
    return None


def instance_view(instance: dict[str, Any],
                  live: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
    """把实例文件与 actor /current 合并成模板可直接使用的视图。"""
    status = instance.get('status') or 'pending'
    terminal = status in TERMINAL_STATUSES
    steps = instance.get('steps') or []
    run = instance.get('run') or {}
    phase = None

    if not terminal and _live_matches(live, instance):
        run = live.get('run') or run
        phase = live.get('phase')
        total = live.get('total_steps')
        total = total if isinstance(total, int) else len(steps)
        done = live.get('next_index')
        done = done if isinstance(done, int) else 0
        wait_until = live.get('not_before')
    else:
        total = len(steps)
        done = len(run.get('steps') or [])
        wait_until = None

    done = max(0, min(done, total))
    # cleanup 阶段不再提示“下一步 steps 命令”，避免显示被跳过的步骤。
    pending_command = (steps[done].get('command')
                       if (status == 'running' and phase != 'cleanup'
                           and done < len(steps)) else None)

    now = datetime.now(timezone.utc)
    waiting = False
    if isinstance(wait_until, str):
        try:
            waiting = parse_utc(wait_until) > now
        except (TypeError, ValueError):
            waiting = False

    view = dict(instance)
    view.update({
        'status_label': STATUS_LABELS.get(status, status),
        'status_class': STATUS_CLASSES.get(status, 'offline'),
        'terminal': terminal,
        'run': run,
        'done_steps': done,
        'total_steps': total,
        'pending_command': pending_command,
        'waiting': waiting,
        'wait_until_local': _local_or_none(wait_until) if waiting else None,
        'scheduled_local': _local_or_none(instance.get('scheduled_at')),
        'created_local': _local_or_none(instance.get('created_at')),
        'started_local': _local_or_none(instance.get('started_at')),
        'finished_local': _local_or_none(instance.get('finished_at')),
        'failure_text': _failure_text(instance, run),
    })
    return view


def _sort_time(instance: dict[str, Any], field: str) -> datetime:
    try:
        return parse_utc(instance[field])
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def list_instances(limit: int = INSTANCE_HISTORY_LIMIT
                   ) -> list[dict[str, Any]]:
    """进行中实例（含实时进度）+ 最近完成实例，供执行状态区展示。"""
    live = current_snapshot()
    now = datetime.now(timezone.utc)
    active: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []

    for instance in iter_instances():
        if instance.get('status') == 'pending':
            try:
                scheduled_at = parse_utc(instance['scheduled_at'])
            except (KeyError, TypeError, ValueError):
                continue
            expire_after = instance.get('expire_after_seconds',
                                        get_expire_after_seconds())
            if now - scheduled_at > timedelta(seconds=expire_after):
                continue

        item = instance_view(instance, live)
        if item['terminal']:
            finished.append(item)
        else:
            active.append(item)

    active.sort(key=lambda item: (_sort_time(item, 'scheduled_at'),
                                  _sort_time(item, 'created_at')))
    finished.sort(key=lambda item: _sort_time(item, 'created_at'),
                  reverse=True)
    return active + finished[:limit]


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


def _post_abort(instance_id: str, port: int) -> tuple[bool, str | None]:
    """调用 actor POST /current/abort，返回 (是否成功, 错误文案)。"""
    body = json.dumps({'instance_id': instance_id}).encode('utf-8')
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/current/abort', data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(
                request, timeout=CURRENT_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        # 409 等错误响应仍带 JSON 文案，读取后原样交给页面。
        try:
            payload = json.loads(exc.read().decode('utf-8'))
        except (OSError, ValueError):
            payload = None
    except (OSError, ValueError) as exc:
        return False, f'无法连接 actor 中止执行: {exc}'

    if isinstance(payload, dict) and payload.get('ok') is True:
        return True, None
    error = payload.get('error') if isinstance(payload, dict) else None
    message = error.get('message') if isinstance(error, dict) else None
    return False, message or 'actor 拒绝了中止请求'


def request_abort(instance_id: str) -> str | None:
    """中止实例：pending 转为取消，running / cancelling 转发 actor。"""
    instance = load_instance(instance_id)
    if instance is None:
        return '实例不存在'

    status = instance.get('status')
    if status == 'pending':
        return cancel_instance(instance_id)
    if status not in ('running', 'cancelling'):
        return '实例已结束，无法中止'

    try:
        port = load_action_config()['http_port']
    except ActionConfigError:
        return 'action.port 配置缺失，无法中止'

    ok, message = _post_abort(instance_id, port)
    return None if ok else message
