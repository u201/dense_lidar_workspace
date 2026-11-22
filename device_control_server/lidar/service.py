"""激光雷达驱动：通过设备 CGI 查询/设置待机模式。

纯库，不监听端口、不常驻、不后台线程。调用方（视图、command handler 等）
直接在本进程内调用，设备状态每次实时向设备查询，不做缓存。

配置来自 device_control_server/device_config.json 的 lidar 段：
    device_ip                 设备 IP
    port                      设备 CGI 端口（真机通常 80，测试环境可能不同）
    request_timeout_seconds   单次请求超时，默认 1.5
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'device_config.json'

MODE_IN_OPERATION = 0
MODE_STANDBY = 1
MODE_LABELS = {MODE_IN_OPERATION: 'in_operation', MODE_STANDBY: 'standby'}

# 转速：设备侧 SpinSpeed 是索引，页面按 rpm 展示
SPIN_INDEX_TO_RPM = {1: 300, 2: 600, 3: 1200}
RPM_TO_SPIN_INDEX = {rpm: index for index, rpm in SPIN_INDEX_TO_RPM.items()}
SPIN_RPMS = set(RPM_TO_SPIN_INDEX)


class LidarError(Exception):
    """驱动层异常基类，调用方按子类区分处理。"""


class LidarConfigError(LidarError):
    """配置缺失或字段非法。"""


class LidarArgumentError(LidarError):
    """调用参数非法，未向设备发出请求。"""


class LidarUnreachableError(LidarError):
    """网络不可达、超时，或设备返回非 200。"""


class LidarResponseError(LidarError):
    """设备响应非法，或 ErrorCode 非 0。"""

    def __init__(self, message: str, error_code: str | None = None,
                 body: Any = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.body = body


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """读取并校验 lidar 段，返回 {host, port, timeout}。"""
    path = Path(config_path) if config_path else CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise LidarConfigError(f'配置文件不存在: {path}')
    except (OSError, json.JSONDecodeError) as exc:
        raise LidarConfigError(f'配置文件读取失败: {path}: {exc}')

    section = raw.get('lidar')
    if not isinstance(section, dict):
        raise LidarConfigError('配置缺少 lidar 段')

    host = section.get('device_ip')
    if not isinstance(host, str) or not host.strip():
        raise LidarConfigError('lidar.device_ip 缺失或非法')

    port = section.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise LidarConfigError('lidar.port 必须是 1-65535 的整数')

    timeout = section.get('request_timeout_seconds', 1.5)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise LidarConfigError('lidar.request_timeout_seconds 必须是正数')

    return {'host': host.strip(), 'port': port, 'timeout': float(timeout)}


def _request(cfg: dict[str, Any] | None, query: str) -> dict[str, Any]:
    """向 pandar.cgi 发请求并返回解析后的 JSON。

    query 为完整 query string；action=set 时以空 body 发 POST，其余为 GET。
    """
    cfg = cfg if cfg is not None else load_config()
    url = f'http://{cfg["host"]}:{cfg["port"]}/pandar.cgi?{query}'
    data = b'' if query.startswith('action=set') else None
    request = urllib.request.Request(url, data=data)

    try:
        with urllib.request.urlopen(request, timeout=cfg['timeout']) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise LidarUnreachableError(f'设备返回 HTTP {exc.code}: {url}')
    except (urllib.error.URLError, OSError) as exc:
        raise LidarUnreachableError(
            f'无法连接设备 {cfg["host"]}:{cfg["port"]}: {exc}')

    if status != 200:
        raise LidarUnreachableError(f'设备返回 HTTP {status}: {url}')

    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise LidarResponseError(f'设备响应不是合法 JSON: {exc}',
                                 body=payload[:200])

    if not isinstance(parsed, dict):
        raise LidarResponseError(f'设备响应结构非法: {type(parsed).__name__}',
                                 body=parsed)
    return parsed


def _check_error_code(parsed: dict[str, Any], action_label: str) -> None:
    """ErrorCode 非 '0' 视为设备侧失败。"""
    head = parsed.get('Head')
    error_code = head.get('ErrorCode') if isinstance(head, dict) else None
    if str(error_code) != '0':
        raise LidarResponseError(f'{action_label}失败: ErrorCode={error_code}',
                                 error_code=None if error_code is None else str(error_code),
                                 body=parsed)


def get_device_info(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """查询设备信息，成功仅返回 {'SN': ...}。"""
    parsed = _request(cfg, 'action=get&object=device_info')
    _check_error_code(parsed, '查询设备信息')

    body = parsed.get('Body')
    if not isinstance(body, dict):
        raise LidarResponseError('设备信息响应缺少 Body',
                                 body=parsed)
    sn = body.get('SN')
    if not isinstance(sn, str) or not sn.strip():
        raise LidarResponseError(f'设备返回 SN 非法: {sn!r}',
                                 body=parsed)
    return {'SN': sn}


def get_standby_mode(cfg: dict[str, Any] | None = None) -> int:
    """查询待机模式，返回 0 或 1。"""
    parsed = _request(cfg, 'action=get&object=lidar_data&key=standbymode')
    _check_error_code(parsed, '查询待机模式')

    body = parsed.get('Body')
    raw_mode = body.get('standbymode') if isinstance(body, dict) else None
    # 真机返回字符串 '0'/'1'，不假定一定是数字；bool/浮点一律视为非法
    if isinstance(raw_mode, bool) or not isinstance(raw_mode, (int, str)):
        raise LidarResponseError(f'设备返回 standbymode 非法: {raw_mode!r}',
                                 body=parsed)
    try:
        mode = int(raw_mode)
    except ValueError:
        raise LidarResponseError(f'设备返回 standbymode 非法: {raw_mode!r}',
                                 body=parsed)
    if mode not in (MODE_IN_OPERATION, MODE_STANDBY):
        raise LidarResponseError(f'设备返回 standbymode 非法: {raw_mode!r}',
                                 body=parsed)
    return mode


def set_standby_mode(mode: int, cfg: dict[str, Any] | None = None) -> None:
    """设置待机模式。成功返回 None，失败抛 LidarError。"""
    if isinstance(mode, bool) or mode not in (MODE_IN_OPERATION, MODE_STANDBY):
        raise LidarArgumentError(f'standby mode 只能是 0 或 1: {mode!r}')

    query = f'action=set&object=lidar_data&key=standbymode&value={mode}'
    parsed = _request(cfg, query)
    _check_error_code(parsed, '设置待机模式')


def get_spin_rate(cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """查询转速，返回 {'index': SpinSpeed 索引, 'rpm': 显示值}。"""
    parsed = _request(cfg, 'action=get&object=lidar_config')
    _check_error_code(parsed, '查询转速')

    body = parsed.get('Body')
    raw = body.get('SpinSpeed') if isinstance(body, dict) else None
    # 真机返回字符串 '1'/'2'/'3'，也兼容整数；bool 一律视为非法
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise LidarResponseError(f'设备返回 SpinSpeed 非法: {raw!r}',
                                 body=parsed)
    try:
        index = int(raw)
    except ValueError:
        raise LidarResponseError(f'设备返回 SpinSpeed 非法: {raw!r}',
                                 body=parsed)
    rpm = SPIN_INDEX_TO_RPM.get(index)
    if rpm is None:
        raise LidarResponseError(f'设备返回 SpinSpeed 非法: {raw!r}',
                                 body=parsed)
    return {'index': index, 'rpm': rpm}


def set_spin_rate(rpm: int, cfg: dict[str, Any] | None = None) -> None:
    """按 rpm 设置转速。成功返回 None，失败抛 LidarError。"""
    if isinstance(rpm, bool) or rpm not in SPIN_RPMS:
        raise LidarArgumentError(f'转速只能是 300/600/1200: {rpm!r}')

    index = RPM_TO_SPIN_INDEX[rpm]
    query = f'action=set&object=lidar&key=spin_speed&value={index}'
    parsed = _request(cfg, query)
    _check_error_code(parsed, '设置转速')
