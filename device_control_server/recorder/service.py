"""Recorder 配置读取与守护进程 HTTP 访问。

纯库：不依赖 Django。web 视图、command handler、daemon 启动都复用这里的
配置校验与请求逻辑，避免三处各写一套。

边界：本模块只做配置读取与 daemon HTTP 访问，不抓包、不写 sidecar；
抓包与落盘全部在 daemon/recorder.py。
"""

from __future__ import annotations

import ipaddress
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'device_config.json'

DEFAULT_TIMEOUT = 1.5


class RecorderError(Exception):
    """客户端错误基类；code 与 daemon 的错误码保持一致。"""

    def __init__(self, code: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class RecorderConfigError(RecorderError):
    def __init__(self, message: str) -> None:
        super().__init__('config_invalid', message)


class RecorderUnreachableError(RecorderError):
    def __init__(self, message: str) -> None:
        super().__init__('recorder_unreachable', message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def platform_interface_key() -> str:
    return 'win_interface' if sys.platform == 'win32' else 'linux_interface'


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """读取并校验 device_config.json 的 recorder 段。

    返回 {interface, udp_port, port, device_ip, timeout, dumpcap_path}。
    device_ip 必须是回环地址：daemon 只允许本机访问。
    """
    path = Path(config_path) if config_path else CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise RecorderConfigError(f'配置文件不存在: {path}')
    except (OSError, json.JSONDecodeError) as exc:
        raise RecorderConfigError(f'配置文件读取失败: {path}: {exc}')

    section = raw.get('recorder') if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise RecorderConfigError('配置缺少 recorder 段')

    interface_key = platform_interface_key()
    interface = section.get(interface_key)
    if not isinstance(interface, str) or not interface.strip():
        raise RecorderConfigError(f'recorder.{interface_key} 缺失或非法')

    udp_port = section.get('udp_port')
    if not _is_int(udp_port) or not 1 <= udp_port <= 65535:
        raise RecorderConfigError('recorder.udp_port 必须是 1-65535 的整数')

    port = section.get('port')
    if not _is_int(port) or not 1 <= port <= 65535:
        raise RecorderConfigError('recorder.port 必须是 1-65535 的整数')

    host = section.get('device_ip')
    if not isinstance(host, str) or not host.strip():
        raise RecorderConfigError('recorder.device_ip 缺失或非法')
    try:
        is_loopback = ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        raise RecorderConfigError(f'recorder.device_ip 不是合法地址: {host!r}')
    if not is_loopback:
        raise RecorderConfigError(
            f'recorder.device_ip 必须是回环地址: {host!r}')

    timeout = section.get('request_timeout_seconds', DEFAULT_TIMEOUT)
    if not _is_number(timeout) or timeout <= 0:
        raise RecorderConfigError(
            'recorder.request_timeout_seconds 必须是正数')

    dumpcap_path = section.get('dumpcap_path')
    if dumpcap_path is not None and not isinstance(dumpcap_path, str):
        raise RecorderConfigError('recorder.dumpcap_path 必须是字符串')

    return {
        'interface': interface.strip(),
        'udp_port': udp_port,
        'port': port,
        'device_ip': host.strip(),
        'timeout': float(timeout),
        'dumpcap_path': dumpcap_path.strip() if dumpcap_path else None,
    }


def _base_url(cfg: dict[str, Any]) -> str:
    return f'http://{cfg["device_ip"]}:{cfg["port"]}'


def _parse_body(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecorderError('invalid_response', f'守护进程响应不是合法 JSON: {exc}')


def _raise_daemon_error(payload: Any) -> None:
    error = payload.get('error') if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get('code'), str):
        raise RecorderError(error['code'], str(error.get('message', '')),
                            error.get('detail'))
    raise RecorderError('invalid_response', '守护进程响应缺少 error')


def _request_json(cfg: dict[str, Any], method: str, path: str,
                  payload: dict[str, Any] | None = None,
                  timeout: float | None = None) -> Any:
    url = _base_url(cfg) + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    request = urllib.request.Request(
        url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(
                request, timeout=timeout or cfg['timeout']) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        # daemon 的 4xx/422 也返回结构化 JSON，优先解析出真实错误码。
        try:
            body = exc.read()
        except OSError:
            body = b''
        try:
            _raise_daemon_error(_parse_body(body))
        except RecorderError as parsed:
            if parsed.code != 'invalid_response':
                raise parsed
        raise RecorderUnreachableError(f'守护进程返回 HTTP {exc.code}: {url}')
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RecorderUnreachableError(f'无法连接录制守护进程: {exc}')

    parsed = _parse_body(raw)
    if status != 200:
        _raise_daemon_error(parsed)
        raise RecorderUnreachableError(f'守护进程返回 HTTP {status}: {url}')
    return parsed


def status(cfg: dict[str, Any] | None = None,
           timeout: float | None = None) -> dict[str, Any]:
    """GET /status，返回 daemon 状态快照。"""
    cfg = cfg if cfg is not None else load_config()
    payload = _request_json(cfg, 'GET', '/status', timeout=timeout)
    if not isinstance(payload, dict):
        raise RecorderError('invalid_response', '守护进程状态响应结构非法')
    return payload


def record(duration: int, action: dict[str, Any], *,
           cfg: dict[str, Any] | None = None,
           timeout: float | None = None) -> dict[str, Any]:
    """POST /record，阻塞到录制结束，返回最终 sidecar。

    客户端超时由调用方按 duration + 30 计算，不在这里猜。
    """
    cfg = cfg if cfg is not None else load_config()
    payload = _request_json(
        cfg, 'POST', '/record',
        {'duration': duration, 'action': action},
        timeout=timeout)
    if not isinstance(payload, dict) or not payload.get('ok'):
        _raise_daemon_error(payload)
        raise RecorderError('invalid_response', '守护进程录制响应结构非法')
    final = payload.get('record')
    if not isinstance(final, dict):
        raise RecorderError('invalid_response', '守护进程录制响应缺少 record')
    return final


def recording_pcap(timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """返回正在录制的 pcap 文件名；空闲、不可达或异常一律返回 None。"""
    try:
        cfg = load_config()
        snapshot = status(cfg, timeout=timeout)
    except RecorderError:
        return None
    if snapshot.get('status') != 'recording':
        return None
    pcap_file = snapshot.get('pcap_file')
    return pcap_file if isinstance(pcap_file, str) and pcap_file else None
