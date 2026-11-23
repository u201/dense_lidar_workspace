"""录制守护进程（单实例、阻塞式录制）。

职责：
- 独占 dumpcap / tcpdump，同一时刻只允许一个录制；
- 录制前完成配置、目录/配额、标定、雷达预检，失败不产生无元数据的 pcap；
- 先写 status=recording 的 sidecar，进程退出后补全成功/失败结果；
- 提供仅绑定回环地址的 HTTP API：GET /status、POST /record。

运行方式（在 device_control_server 目录下）：
    ..\\venv\\Scripts\\python.exe -m daemon.recorder
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 允许从任意目录启动：daemon 的上一级即 Django 项目根。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402

django.setup()

from artifact import service as artifact_service  # noqa: E402
from lidar import service as lidar_service  # noqa: E402
from recorder import calibration  # noqa: E402
from recorder import service as recorder_service  # noqa: E402

logger = logging.getLogger('recorder')

MAX_DURATION_SECONDS = 600
MAX_OUTPUT_BYTES = 16 * 1024
MAX_REQUEST_BYTES = 256 * 1024

DUMPCAP_FALLBACK = Path(r'C:\Program Files\Wireshark\dumpcap.exe')
PCAP_SUFFIX = '.pcap'
SIDECAR_SUFFIX = '.record.json'

SUCCESS_RETURNCODES = {'win32': (0,), 'linux': (0, 124)}


class RecorderRequestError(Exception):
    """请求级错误：直接映射为 HTTP 状态码与 daemon error code。"""

    def __init__(self, http_status: int, code: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------------------
# 纯函数：时间、预检、命令构造
# ---------------------------------------------------------------------------

def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_action(action: Any) -> dict[str, Any]:
    """校验 action 上下文，返回规范化后的快照；非法则 invalid_action_context。"""
    def fail(message: str) -> None:
        raise RecorderRequestError(400, 'invalid_action_context', message)

    if not isinstance(action, dict):
        fail('缺少 action 上下文')

    kind = action.get('kind')
    if kind not in ('scheduled', 'immediate'):
        fail(f'action.kind 非法: {kind!r}')

    for key in ('run_id', 'action_id', 'title', 'started_at'):
        value = action.get(key)
        if not isinstance(value, str) or not value.strip():
            fail(f'action.{key} 必须是非空字符串')

    step_index = action.get('step_index')
    step_count = action.get('step_count')
    if not _is_int(step_index) or step_index < 0:
        fail(f'action.step_index 非法: {step_index!r}')
    if not _is_int(step_count) or step_count <= 0:
        fail(f'action.step_count 非法: {step_count!r}')
    if step_index >= step_count:
        fail(f'action.step_index 超出范围: {step_index} >= {step_count}')

    steps = action.get('steps')
    if not isinstance(steps, list) or not steps:
        fail('action.steps 必须是非空数组')
    if not all(isinstance(step, dict) for step in steps):
        fail('action.steps 的元素必须是对象')

    instance_id = action.get('instance_id')
    if instance_id is not None and not isinstance(instance_id, str):
        fail('action.instance_id 必须是字符串或 null')
    scheduled_at = action.get('scheduled_at')
    if scheduled_at is not None and not isinstance(scheduled_at, str):
        fail('action.scheduled_at 必须是字符串或 null')

    return {
        'kind': kind,
        'instance_id': instance_id,
        'run_id': action['run_id'],
        'action_id': action['action_id'],
        'title': action['title'],
        'scheduled_at': scheduled_at,
        'started_at': action['started_at'],
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
    }


def resolve_capture_tool(cfg: dict[str, Any]) -> str:
    """定位 dumpcap / tcpdump；找不到返回 capture_tool_missing。"""
    if sys.platform == 'win32':
        configured = cfg.get('dumpcap_path')
        if configured:
            path = Path(configured)
            if path.is_file():
                return str(path)
        found = shutil.which('dumpcap')
        if found:
            return found
        if DUMPCAP_FALLBACK.is_file():
            return str(DUMPCAP_FALLBACK)
        raise RecorderRequestError(
            422, 'capture_tool_missing',
            '未找到 dumpcap，请安装 Wireshark/Npcap 或配置 recorder.dumpcap_path',
            {'configured': configured})

    found = shutil.which('tcpdump')
    if not found:
        raise RecorderRequestError(
            422, 'capture_tool_missing', '未找到 tcpdump，请先安装并授予抓包权限')
    return found


def resolve_output_folder() -> Path:
    """复用 artifact 配置解析目录并判定配额。"""
    section, error = artifact_service.load_artifact_config()
    if error is not None:
        raise RecorderRequestError(422, 'artifact_unavailable', error)
    folder = artifact_service.resolve_local_folder(
        artifact_service.read_local_folder(section))
    if folder is None:
        raise RecorderRequestError(
            422, 'artifact_unavailable', 'artifact 未配置当前平台的输出目录')

    overview = artifact_service.get_card()['overview']
    if not overview.get('recording_enabled'):
        reason = overview.get('blocked_reason') or 'unknown'
        raise RecorderRequestError(
            422, 'artifact_unavailable',
            f'输出目录不可用（{reason}）',
            {'blocked_reason': reason,
             'folder': overview.get('local_folder')})
    return folder


def query_lidar() -> dict[str, Any]:
    """按固定顺序查询待机模式、SN、转速；待机直接拒绝录制。"""
    try:
        cfg = lidar_service.load_config()
    except lidar_service.LidarConfigError as exc:
        raise RecorderRequestError(422, 'config_invalid', str(exc))

    try:
        mode = lidar_service.get_standby_mode(cfg)
        if mode != lidar_service.MODE_IN_OPERATION:
            raise RecorderRequestError(
                422, 'lidar_standby',
                '雷达处于待机模式，无有效数据；请先执行 lidar.standby(mode=0)',
                {'mode': mode})
        info = lidar_service.get_device_info(cfg)
        spin = lidar_service.get_spin_rate(cfg)
    except lidar_service.LidarUnreachableError as exc:
        raise RecorderRequestError(422, 'lidar_unreachable', str(exc))
    except lidar_service.LidarResponseError as exc:
        raise RecorderRequestError(422, 'lidar_response_invalid', str(exc))

    return {'sn': info['SN'], 'spin_rpm': spin['rpm']}


def build_capture_command(cfg: dict[str, Any], capture_tool: str,
                          duration: int, pcap_path: Path) -> list[str]:
    """按平台构造抓包命令；参数一律用列表，不拼 shell 字符串。"""
    bpf = f"udp port {cfg['udp_port']}"
    if sys.platform == 'win32':
        return [
            capture_tool,
            '-i', cfg['interface'],
            '-f', bpf,
            '-a', f'duration:{duration}',
            '-w', str(pcap_path),
        ]
    return [
        'timeout', '-s', 'INT', str(duration),
        capture_tool,
        '-i', cfg['interface'],
        '-n', '-U',
        '-w', str(pcap_path),
        bpf,
    ]


def decode_output(data: bytes) -> str:
    """dumpcap 输出可能含中文网卡名；UTF-8 优先，失败退回 GBK。"""
    for encoding in ('utf-8', 'gbk'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def make_record_id(folder: Path) -> str:
    """UTC 微秒时间戳；同名时追加 -1/-2，不覆盖。"""
    base = f'record-{_now():%Y%m%dT%H%M%S%fZ}'
    candidate = base
    suffix = 0
    while ((folder / f'{candidate}{PCAP_SUFFIX}').exists()
           or (folder / f'{candidate}{SIDECAR_SUFFIX}').exists()):
        suffix += 1
        candidate = f'{base}-{suffix}'
    return candidate


def write_sidecar(path: Path, record: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再 os.replace。"""
    tmp_path = path.with_name(path.name + '.tmp')
    tmp_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    os.replace(tmp_path, path)


def _read_output(proc: subprocess.Popen) -> tuple[str, bool]:
    """读取合并后的 stdout/stderr，上限 16 KB，超出部分丢弃并标记截断。"""
    buffer = bytearray()
    truncated = False
    stream = proc.stdout
    if stream is None:
        return '', False
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        remaining = MAX_OUTPUT_BYTES - len(buffer)
        if remaining > 0:
            buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        else:
            truncated = True
    return decode_output(bytes(buffer)), truncated


def preflight(duration: Any, action: Any) -> dict[str, Any]:
    """按 spec 5.3 的顺序预检；失败抛出，不写文件、不 spawn。"""
    if not _is_int(duration) or not 1 <= duration <= MAX_DURATION_SECONDS:
        raise RecorderRequestError(
            400, 'invalid_duration',
            f'duration 必须是 1-{MAX_DURATION_SECONDS} 的整数')

    validated_action = validate_action(action)

    try:
        cfg = recorder_service.load_config()
    except recorder_service.RecorderError as exc:
        raise RecorderRequestError(422, 'config_invalid', exc.message)

    capture_tool = resolve_capture_tool(cfg)
    folder = resolve_output_folder()

    try:
        calibration_snapshot = calibration.load_calibration()
    except calibration.CalibrationError as exc:
        raise RecorderRequestError(422, 'calibration_invalid', str(exc))

    extra_keys = calibration.unknown_keys(calibration_snapshot['values'])
    if extra_keys:
        logger.warning('calibration.json 含未知键: %s', ', '.join(extra_keys))

    lidar_snapshot = query_lidar()

    return {
        'duration': duration,
        'config': cfg,
        'capture_tool': capture_tool,
        'folder': folder,
        'calibration': calibration_snapshot,
        'lidar': lidar_snapshot,
        'action': validated_action,
    }


# ---------------------------------------------------------------------------
# 守护进程
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._record_lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

    # -- 生命周期 ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        current = self.current
        proc = current.get('process') if current else None
        if proc is not None and proc.poll() is None:
            logger.info('terminating capture process %s', proc.pid)
            proc.terminate()

    def run_forever(self) -> None:
        cfg = recorder_service.load_config()
        self._recover_stale_sidecars()
        self._start_http('127.0.0.1', cfg['port'])
        logger.info('recorder started on http://127.0.0.1:%s', cfg['port'])
        self._stop.wait()
        self._wait_capture_finished()
        self._stop_http()
        logger.info('recorder stopped')

    def _wait_capture_finished(self, timeout: float = 10.0) -> None:
        """收到停止信号后，等待录制线程把失败结果写入 sidecar。"""
        deadline = time.monotonic() + timeout
        while self.current is not None and time.monotonic() < deadline:
            time.sleep(0.1)

    def _start_http(self, host: str, port: int) -> None:
        handler = type('RecorderHandler', (_RecorderHandler,), {'recorder': self})
        self._http_server = ThreadingHTTPServer((host, port), handler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name='recorder-http', daemon=True)
        self._http_thread.start()

    def _stop_http(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
            self._http_thread = None

    def _recover_stale_sidecars(self) -> None:
        """启动时把遗留的 recording sidecar 标记为 interrupted。"""
        try:
            section, error = artifact_service.load_artifact_config()
            if error is not None:
                logger.warning('skip sidecar recovery: %s', error)
                return
            folder = artifact_service.resolve_local_folder(
                artifact_service.read_local_folder(section))
        except Exception:  # 恢复失败不能阻止守护进程启动
            logger.exception('skip sidecar recovery')
            return

        if folder is None or not folder.is_dir():
            logger.warning('skip sidecar recovery: output folder unavailable')
            return

        finished_at = iso_utc(_now())
        for path in sorted(folder.glob(f'*{SIDECAR_SUFFIX}')):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get('status') != 'recording':
                continue
            record['status'] = 'interrupted'
            record['finished_at'] = finished_at
            record['error'] = {
                'code': 'daemon_restarted',
                'message': '录制守护进程重启，录制被中断',
                'detail': {},
            }
            write_sidecar(path, record)
            logger.warning('marked interrupted: %s', path.name)

    # -- 状态与录制 -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        current = self.current
        if current is None:
            return {
                'status': 'idle',
                'record_id': None,
                'pcap_file': None,
                'started_at': None,
                'requested_duration_seconds': None,
                'elapsed_seconds': 0,
            }
        elapsed = (_now() - current['started_at']).total_seconds()
        return {
            'status': 'recording',
            'record_id': current['record_id'],
            'pcap_file': current['pcap_path'].name,
            'started_at': current['started_at_iso'],
            'requested_duration_seconds': current['duration'],
            'elapsed_seconds': round(elapsed, 3),
        }

    def abort(self, record_id: Any = None) -> dict[str, Any]:
        """中止当前录制；空闲时幂等 no-op，record_id 不匹配直接拒绝。"""
        current = self.current
        if current is None:
            return {'ok': True, 'aborted': False}
        if record_id is not None:
            if not isinstance(record_id, str):
                raise RecorderRequestError(
                    400, 'invalid_request', 'record_id 必须是字符串')
            if record_id != current['record_id']:
                raise RecorderRequestError(
                    409, 'record_mismatch',
                    f'当前录制为 {current["record_id"]}，与请求不匹配')

        current['abort_requested'] = True
        proc = current.get('process')
        if proc is not None and proc.poll() is None:
            logger.info('aborting capture process %s', proc.pid)
            proc.terminate()
        return {'ok': True, 'aborted': True}

    def record(self, duration: Any, action: Any) -> dict[str, Any]:
        if not self._record_lock.acquire(blocking=False):
            raise RecorderRequestError(409, 'recorder_busy', '已有录制在进行')
        try:
            return self._record_locked(duration, action)
        finally:
            self._record_lock.release()

    def _record_locked(self, duration: Any, action: Any) -> dict[str, Any]:
        ctx = preflight(duration, action)
        folder: Path = ctx['folder']
        record_id = make_record_id(folder)
        pcap_path = folder / f'{record_id}{PCAP_SUFFIX}'
        sidecar_path = folder / f'{record_id}{SIDECAR_SUFFIX}'
        command = build_capture_command(
            ctx['config'], ctx['capture_tool'], ctx['duration'], pcap_path)

        started_at = _now()
        record: dict[str, Any] = {
            'schema_version': 1,
            'record_id': record_id,
            'status': 'recording',
            'pcap_file': pcap_path.name,
            'requested_duration_seconds': ctx['duration'],
            'started_at': iso_utc(started_at),
            'finished_at': None,
            'interface': ctx['config']['interface'],
            'udp_port': ctx['config']['udp_port'],
            'bpf': f"udp port {ctx['config']['udp_port']}",
            'pcap_size_bytes': None,
            'lidar': ctx['lidar'],
            'calibration': ctx['calibration'],
            'action': ctx['action'],
            'process': {
                'command': command,
                'returncode': None,
                'output': None,
                'output_truncated': False,
            },
            'error': None,
        }
        write_sidecar(sidecar_path, record)

        self.current = current = {
            'record_id': record_id,
            'pcap_path': pcap_path,
            'sidecar_path': sidecar_path,
            'process': None,
            'started_at': started_at,
            'started_at_iso': record['started_at'],
            'duration': ctx['duration'],
            'abort_requested': False,
        }

        try:
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                code = 'capture_failed'
                if isinstance(exc, PermissionError):
                    code = 'capture_permission_denied'
                elif isinstance(exc, FileNotFoundError):
                    code = 'capture_tool_missing'
                record['status'] = 'failed'
                record['finished_at'] = iso_utc(_now())
                record['pcap_size_bytes'] = 0
                record['error'] = {
                    'code': code,
                    'message': f'启动抓包工具失败: {exc}',
                    'detail': {},
                }
                write_sidecar(sidecar_path, record)
                if code != 'capture_failed':
                    raise RecorderRequestError(422, code, record['error']['message'])
                return record

            current['process'] = proc
            # 预检期间到达的中止请求：进程一启动就立即终止。
            if current['abort_requested'] and proc.poll() is None:
                proc.terminate()
            output, truncated = _read_output(proc)
            returncode = proc.wait()

            record['finished_at'] = iso_utc(_now())
            record['pcap_size_bytes'] = (pcap_path.stat().st_size
                                         if pcap_path.exists() else 0)
            record['process']['returncode'] = returncode
            record['process']['output'] = output
            record['process']['output_truncated'] = truncated

            success_codes = SUCCESS_RETURNCODES.get(sys.platform, (0, 124))
            # 中止优先判定：避免被「返回码非 0 / pcap 为空」覆盖成失败。
            if current['abort_requested']:
                record['status'] = 'aborted'
                record['error'] = {
                    'code': 'recorder_aborted',
                    'message': '录制被中止',
                    'detail': {},
                }
            elif self._stop.is_set() and returncode != 0:
                record['status'] = 'failed'
                record['error'] = {
                    'code': 'daemon_stopped',
                    'message': '录制守护进程收到停止信号，录制被中断',
                    'detail': {},
                }
            elif returncode not in success_codes:
                record['status'] = 'failed'
                record['error'] = {
                    'code': 'capture_failed',
                    'message': f'抓包工具退出码 {returncode}',
                    'detail': {},
                }
            elif record['pcap_size_bytes'] == 0:
                record['status'] = 'failed'
                record['error'] = {
                    'code': 'capture_failed',
                    'message': '未生成 pcap 文件，请检查网卡名与抓包权限',
                    'detail': {},
                }
            else:
                record['status'] = 'succeeded'

            write_sidecar(sidecar_path, record)
            return record
        finally:
            self.current = None


class _RecorderHandler(BaseHTTPRequestHandler):
    """GET /status 与 POST /record；实例由 _start_http 注入。"""

    recorder: Recorder

    def do_GET(self) -> None:
        if self.path != '/status':
            self.send_error(404)
            return
        self._send_json(200, self.recorder.snapshot())

    def do_POST(self) -> None:
        if self.path == '/record':
            self._handle_record()
        elif self.path == '/abort':
            self._handle_abort()
        else:
            self.send_error(404)

    def _handle_record(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {
                'ok': False,
                'error': {'code': 'invalid_request', 'message': str(exc)},
            })
            return

        try:
            record = self.recorder.record(payload.get('duration'),
                                          payload.get('action'))
        except RecorderRequestError as exc:
            self._send_json(exc.http_status, {
                'ok': False,
                'error': {
                    'code': exc.code,
                    'message': exc.message,
                    'detail': exc.detail,
                },
            })
            return
        except Exception as exc:  # 单次请求异常不能拖垮守护进程
            logger.exception('record failed')
            self._send_json(500, {
                'ok': False,
                'error': {'code': 'internal_error', 'message': str(exc)},
            })
            return

        self._send_json(200, {'ok': True, 'record': record})

    def _handle_abort(self) -> None:
        try:
            payload = self._read_json(allow_empty=True)
        except ValueError as exc:
            self._send_json(400, {
                'ok': False,
                'error': {'code': 'invalid_request', 'message': str(exc)},
            })
            return

        try:
            result = self.recorder.abort(payload.get('record_id'))
        except RecorderRequestError as exc:
            self._send_json(exc.http_status, {
                'ok': False,
                'error': {
                    'code': exc.code,
                    'message': exc.message,
                    'detail': exc.detail,
                },
            })
            return
        except Exception as exc:  # 单次请求异常不能拖垮守护进程
            logger.exception('abort failed')
            self._send_json(500, {
                'ok': False,
                'error': {'code': 'internal_error', 'message': str(exc)},
            })
            return

        self._send_json(200, result)

    def _read_json(self, allow_empty: bool = False) -> dict[str, Any]:
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ValueError('Content-Length 非法')
        if length <= 0:
            if allow_empty:
                return {}
            raise ValueError('请求体为空')
        if length > MAX_REQUEST_BYTES:
            raise ValueError(f'请求体超过 {MAX_REQUEST_BYTES} 字节')
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'请求体不是合法 JSON: {exc}')
        if not isinstance(payload, dict):
            raise ValueError('请求体顶层必须是对象')
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug('http: ' + format, *args)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    recorder = Recorder()

    def handle_signal(signum: int, _frame: Any) -> None:
        logger.info('signal %s received, stopping', signum)
        recorder.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        recorder.run_forever()
    except recorder_service.RecorderError as exc:
        logger.error('recorder cannot start: %s', exc.message)
        return 1
    except OSError as exc:
        logger.error('recorder cannot start: %s', exc)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
