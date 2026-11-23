"""Recorder 测试：全部使用 fake 进程、临时目录与 mock HTTP，不真正抓包。"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, SimpleTestCase
from django.urls import reverse

from daemon import recorder as recorder_daemon
from recorder import calibration, commands, service

ACTION_CONTEXT = {
    'kind': 'immediate',
    'instance_id': None,
    'run_id': 'record_600rpm-run',
    'action_id': 'record_600rpm',
    'title': '600 rpm 录制 10 秒',
    'scheduled_at': None,
    'started_at': '2026-11-22T10:15:00Z',
    'step_index': 1,
    'step_count': 2,
    'steps': [
        {'command': 'lidar.spin', 'args': {'rpm': 600}, 'delay': 5},
        {'command': 'recorder.record', 'args': {'duration': 10}, 'delay': 0},
    ],
}

RECORDER_CONFIG = {
    'interface': 'WLAN 2',
    'udp_port': 2368,
    'port': 6,
    'timeout': 1.5,
    'dumpcap_path': None,
}

RECORDER_SECTION = {
    service.platform_interface_key(): 'WLAN 2',
    'udp_port': 2368,
    'port': 6,
    'request_timeout_seconds': 1.5,
}

SIDECAR = {
    'schema_version': 1,
    'record_id': 'record-20261122T101500123456Z',
    'pcap_file': 'record-20261122T101500123456Z.pcap',
    'requested_duration_seconds': 10,
    'started_at': '2026-11-22T10:15:00Z',
    'finished_at': '2026-11-22T10:15:10.5Z',
    'status': 'succeeded',
    'error': None,
}


def write_calibration(folder: Path, **overrides) -> Path:
    values = {
        'schema_version': 1,
        'alpha1_degrees': 1.5,
        'alpha2_degrees': -1.5,
        'arm_meter': 0.12,
        'calibrated_at': '2026-11-20T02:00:00Z',
    }
    values.update(overrides)
    path = folder / 'calibration.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    return path


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------

class CalibrationTests(SimpleTestCase):
    def test_valid_file_returns_values_mtime_sha256(self):
        with tempfile.TemporaryDirectory() as raw:
            path = write_calibration(Path(raw), note='额外键')
            snapshot = calibration.load_calibration(path)

        self.assertEqual(snapshot['values']['alpha1_degrees'], 1.5)
        self.assertEqual(snapshot['values']['note'], '额外键')
        self.assertEqual(len(snapshot['sha256']), 64)
        self.assertTrue(snapshot['mtime'].endswith('Z'))

    def test_missing_file(self):
        with self.assertRaises(calibration.CalibrationError):
            calibration.load_calibration(Path(tempfile.gettempdir()) / 'nope.json')

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'calibration.json'
            path.write_text('{not json', encoding='utf-8')
            with self.assertRaises(calibration.CalibrationError):
                calibration.load_calibration(path)

    def test_missing_required_key(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'calibration.json'
            path.write_text(json.dumps({
                'schema_version': 1,
                'alpha2_degrees': 0,
                'arm_meter': 0.1,
            }), encoding='utf-8')
            with self.assertRaises(calibration.CalibrationError):
                calibration.load_calibration(path)

    def test_alpha_out_of_range(self):
        with tempfile.TemporaryDirectory() as raw:
            path = write_calibration(Path(raw), alpha1_degrees=181)
            with self.assertRaises(calibration.CalibrationError):
                calibration.load_calibration(path)

    def test_arm_zero_or_negative(self):
        with tempfile.TemporaryDirectory() as raw:
            path = write_calibration(Path(raw), arm_meter=0)
            with self.assertRaises(calibration.CalibrationError):
                calibration.load_calibration(path)

    def test_bool_is_not_number(self):
        with tempfile.TemporaryDirectory() as raw:
            path = write_calibration(Path(raw), arm_meter=True)
            with self.assertRaises(calibration.CalibrationError):
                calibration.load_calibration(path)

    def test_unknown_keys_reported_but_allowed(self):
        with tempfile.TemporaryDirectory() as raw:
            path = write_calibration(Path(raw), extra_field=1)
            snapshot = calibration.load_calibration(path)

        self.assertEqual(calibration.unknown_keys(snapshot['values']),
                         ['extra_field'])


# ---------------------------------------------------------------------------
# service 配置与 HTTP
# ---------------------------------------------------------------------------

class RecorderConfigTests(SimpleTestCase):
    def _load(self, section):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'device_config.json'
            path.write_text(json.dumps({'recorder': section}), encoding='utf-8')
            return service.load_config(path)

    def test_valid_config(self):
        cfg = self._load(dict(RECORDER_SECTION))
        self.assertEqual(cfg['interface'], 'WLAN 2')
        self.assertEqual(cfg['port'], 6)

    def test_missing_interface_rejected(self):
        section = dict(RECORDER_SECTION)
        section[service.platform_interface_key()] = '  '
        with self.assertRaises(service.RecorderConfigError):
            self._load(section)

    def test_invalid_ports_rejected(self):
        for key, value in (('udp_port', True), ('udp_port', 0),
                           ('port', 70000)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(service.RecorderConfigError):
                    self._load(dict(RECORDER_SECTION, **{key: value}))


class ServiceTests(SimpleTestCase):
    def test_recording_pcap_when_recording(self):
        with patch.object(service, 'load_config', return_value=RECORDER_CONFIG), \
                patch.object(service, 'status',
                             return_value={'status': 'recording',
                                           'pcap_file': 'a.pcap'}):
            self.assertEqual(service.recording_pcap(), 'a.pcap')

    def test_recording_pcap_when_idle(self):
        with patch.object(service, 'load_config', return_value=RECORDER_CONFIG), \
                patch.object(service, 'status',
                             return_value={'status': 'idle',
                                           'pcap_file': None}):
            self.assertIsNone(service.recording_pcap())

    def test_recording_pcap_when_unreachable(self):
        with patch.object(service, 'load_config', return_value=RECORDER_CONFIG), \
                patch.object(service, 'status',
                             side_effect=service.RecorderUnreachableError('x')):
            self.assertIsNone(service.recording_pcap())

    def test_record_raises_daemon_error(self):
        with patch.object(service, 'load_config', return_value=RECORDER_CONFIG), \
                patch.object(service, '_request_json',
                             side_effect=service.RecorderError(
                                 'recorder_busy', 'busy')):
            with self.assertRaises(service.RecorderError) as ctx:
                service.record(10, ACTION_CONTEXT)
        self.assertEqual(ctx.exception.code, 'recorder_busy')


# ---------------------------------------------------------------------------
# daemon 预检
# ---------------------------------------------------------------------------

class DaemonPreflightTests(SimpleTestCase):
    def test_invalid_duration(self):
        for duration in (0, 601, True, '10'):
            with self.subTest(duration=duration):
                with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                    recorder_daemon.preflight(duration, ACTION_CONTEXT)
                self.assertEqual(ctx.exception.code, 'invalid_duration')

    def test_invalid_action_context(self):
        for action in (None, {}, dict(ACTION_CONTEXT, step_index=5)):
            with self.subTest(action=action):
                with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                    recorder_daemon.preflight(10, action)
                self.assertEqual(ctx.exception.code, 'invalid_action_context')

    def test_config_invalid(self):
        with patch.object(recorder_daemon.recorder_service, 'load_config',
                          side_effect=service.RecorderConfigError('bad')):
            with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                recorder_daemon.preflight(10, ACTION_CONTEXT)
        self.assertEqual(ctx.exception.code, 'config_invalid')

    def test_artifact_unavailable(self):
        with patch.object(recorder_daemon.recorder_service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(recorder_daemon, 'resolve_capture_tool',
                             return_value='dumpcap'), \
                patch.object(recorder_daemon, 'resolve_output_folder',
                             side_effect=recorder_daemon.RecorderRequestError(
                                 422, 'artifact_unavailable', 'full')):
            with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                recorder_daemon.preflight(10, ACTION_CONTEXT)
        self.assertEqual(ctx.exception.code, 'artifact_unavailable')

    def test_calibration_invalid(self):
        with patch.object(recorder_daemon.recorder_service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(recorder_daemon, 'resolve_capture_tool',
                             return_value='dumpcap'), \
                patch.object(recorder_daemon, 'resolve_output_folder',
                             return_value=Path(tempfile.gettempdir())), \
                patch.object(recorder_daemon.calibration, 'load_calibration',
                             side_effect=calibration.CalibrationError('bad')):
            with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                recorder_daemon.preflight(10, ACTION_CONTEXT)
        self.assertEqual(ctx.exception.code, 'calibration_invalid')

    def test_lidar_standby_prevents_capture(self):
        with patch.object(recorder_daemon.recorder_service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(recorder_daemon, 'resolve_capture_tool',
                             return_value='dumpcap'), \
                patch.object(recorder_daemon, 'resolve_output_folder',
                             return_value=Path(tempfile.gettempdir())), \
                patch.object(recorder_daemon.calibration, 'load_calibration',
                             return_value={'values': {}, 'mtime': '', 'sha256': ''}), \
                patch.object(recorder_daemon, 'query_lidar',
                             side_effect=recorder_daemon.RecorderRequestError(
                                 422, 'lidar_standby', 'standby')), \
                patch.object(recorder_daemon.subprocess, 'Popen') as popen:
            with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                recorder_daemon.preflight(10, ACTION_CONTEXT)
        self.assertEqual(ctx.exception.code, 'lidar_standby')
        popen.assert_not_called()

    def test_valid_preflight_returns_context(self):
        with patch.object(recorder_daemon.recorder_service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(recorder_daemon, 'resolve_capture_tool',
                             return_value='dumpcap'), \
                patch.object(recorder_daemon, 'resolve_output_folder',
                             return_value=Path(tempfile.gettempdir())), \
                patch.object(recorder_daemon.calibration, 'load_calibration',
                             return_value={'values': {'schema_version': 1},
                                           'mtime': 'm', 'sha256': 's'}), \
                patch.object(recorder_daemon, 'query_lidar',
                             return_value={'sn': 'SN-1', 'spin_rpm': 600}):
            ctx = recorder_daemon.preflight(10, ACTION_CONTEXT)

        self.assertEqual(ctx['lidar']['sn'], 'SN-1')
        self.assertEqual(ctx['action']['step_index'], 1)
        self.assertEqual(len(ctx['action']['steps']), 2)


# ---------------------------------------------------------------------------
# daemon 抓包与 sidecar
# ---------------------------------------------------------------------------

class FakeProcess:
    def __init__(self, output: bytes = b'', returncode: int = 0) -> None:
        self.stdout = io.BytesIO(output)
        self.returncode = returncode
        self.pid = 1234

    def wait(self) -> int:
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        pass


class DaemonCaptureTests(SimpleTestCase):
    def _preflight_ctx(self, folder: Path) -> dict:
        return {
            'duration': 1,
            'config': dict(RECORDER_CONFIG),
            'capture_tool': 'dumpcap.exe',
            'folder': folder,
            'calibration': {'values': {'schema_version': 1},
                            'mtime': '2026-11-20T02:00:00Z',
                            'sha256': 'a' * 64},
            'lidar': {'sn': 'SN-1', 'spin_rpm': 600},
            'action': recorder_daemon.validate_action(ACTION_CONTEXT),
        }

    def test_success_writes_sidecar_and_action(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            proc = FakeProcess(b'Packets received/dropped: 1050/26', 0)

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b'x' * 64)
                return proc

            recorder = recorder_daemon.Recorder()
            with patch.object(recorder_daemon, 'preflight',
                              return_value=self._preflight_ctx(folder)), \
                    patch.object(recorder_daemon.subprocess, 'Popen',
                                 side_effect=fake_popen):
                record = recorder.record(1, ACTION_CONTEXT)

            self.assertEqual(record['status'], 'succeeded')
            self.assertEqual(record['pcap_size_bytes'], 64)
            self.assertEqual(record['process']['returncode'], 0)
            self.assertIn('Packets received/dropped', record['process']['output'])
            self.assertEqual(record['action']['step_index'], 1)
            self.assertEqual(record['action']['step_count'], 2)
            self.assertFalse(record['process']['output_truncated'])
            self.assertIsNone(record['error'])

            sidecar = folder / f"{record['record_id']}.record.json"
            self.assertTrue(sidecar.exists())
            self.assertEqual(json.loads(sidecar.read_text(encoding='utf-8')),
                             record)

    def test_nonzero_exit_marks_failed_and_keeps_output(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            proc = FakeProcess(b'permission denied', 1)

            def fake_popen(command, **kwargs):
                Path(command[-1]).write_bytes(b'')
                return proc

            recorder = recorder_daemon.Recorder()
            with patch.object(recorder_daemon, 'preflight',
                              return_value=self._preflight_ctx(folder)), \
                    patch.object(recorder_daemon.subprocess, 'Popen',
                                 side_effect=fake_popen):
                record = recorder.record(1, ACTION_CONTEXT)

            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['error']['code'], 'capture_failed')
            self.assertEqual(record['process']['output'], 'permission denied')

    def test_second_record_is_busy(self):
        recorder = recorder_daemon.Recorder()
        recorder._record_lock.acquire()
        try:
            with self.assertRaises(recorder_daemon.RecorderRequestError) as ctx:
                recorder.record(1, ACTION_CONTEXT)
        finally:
            recorder._record_lock.release()
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.code, 'recorder_busy')

    def test_snapshot_idle_and_recording(self):
        recorder = recorder_daemon.Recorder()
        self.assertEqual(recorder.snapshot()['status'], 'idle')

        recorder.current = {
            'record_id': 'record-1',
            'pcap_path': Path('record-1.pcap'),
            'started_at': recorder_daemon._now(),
            'started_at_iso': '2026-11-22T10:15:00Z',
            'duration': 10,
        }
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot['status'], 'recording')
        self.assertEqual(snapshot['pcap_file'], 'record-1.pcap')
        self.assertGreaterEqual(snapshot['elapsed_seconds'], 0)

    def test_recovery_marks_recording_interrupted(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            pcap = folder / 'record-1.pcap'
            pcap.write_bytes(b'data')
            sidecar = folder / 'record-1.record.json'
            sidecar.write_text(json.dumps({'record_id': 'record-1',
                                           'status': 'recording'}),
                               encoding='utf-8')

            recorder = recorder_daemon.Recorder()
            with patch.object(recorder_daemon.artifact_service,
                              'load_artifact_config',
                              return_value=({}, None)), \
                    patch.object(recorder_daemon.artifact_service,
                                 'read_local_folder', return_value=str(folder)), \
                    patch.object(recorder_daemon.artifact_service,
                                 'resolve_local_folder', return_value=folder):
                recorder._recover_stale_sidecars()

            data = json.loads(sidecar.read_text(encoding='utf-8'))
            self.assertEqual(data['status'], 'interrupted')
            self.assertEqual(data['error']['code'], 'daemon_restarted')
            self.assertEqual(pcap.read_bytes(), b'data')

    def test_capture_command_windows(self):
        command = recorder_daemon.build_capture_command(
            RECORDER_CONFIG, 'dumpcap.exe', 10, Path('a.pcap'))
        with patch.object(recorder_daemon.sys, 'platform', 'win32'):
            command = recorder_daemon.build_capture_command(
                RECORDER_CONFIG, 'dumpcap.exe', 10, Path('a.pcap'))
        self.assertEqual(command[0], 'dumpcap.exe')
        self.assertIn('duration:10', command)
        self.assertEqual(command[-1], 'a.pcap')

    def test_capture_command_linux(self):
        with patch.object(recorder_daemon.sys, 'platform', 'linux'):
            command = recorder_daemon.build_capture_command(
                RECORDER_CONFIG, 'tcpdump', 10, Path('a.pcap'))
        self.assertEqual(command[:4], ['timeout', '-s', 'INT', '10'])
        self.assertIn('tcpdump', command)
        self.assertEqual(command[-1], 'udp port 2368')

    def test_decode_output_gbk(self):
        self.assertEqual(recorder_daemon.decode_output('网卡'.encode('gbk')), '网卡')

    def test_make_record_id_avoids_collision(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            first = recorder_daemon.make_record_id(folder)
            (folder / f'{first}.pcap').write_bytes(b'')
            second = recorder_daemon.make_record_id(folder)
            self.assertNotEqual(first, second)


# ---------------------------------------------------------------------------
# action 上下文
# ---------------------------------------------------------------------------

class ActionContextTests(SimpleTestCase):
    def test_execute_step_passes_action_snapshot(self):
        from action import service as action_service

        captured = {}

        def handler(args, ctx):
            captured.update(ctx)
            return {'ok': True}

        registry = {'fake.cmd': {'params': [], 'handler': handler}}
        steps = [{'command': 'fake.cmd', 'args': {'duration': 10},
                  'delay': 0}]
        execution = {
            'kind': 'scheduled',
            'instance_id': 'inst-1',
            'title': '预约录制',
            'scheduled_at': '2026-11-22T10:15:00Z',
            'steps': steps,
        }
        run = action_service.new_run('record_600rpm')

        with patch.object(action_service, 'get_registry',
                          return_value=registry):
            result = action_service.execute_step(
                steps[0], run, 'record_600rpm', execution)

        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(captured['step_index'], 0)
        self.assertEqual(captured['step_count'], 1)
        self.assertEqual(captured['action']['kind'], 'scheduled')
        self.assertEqual(captured['action']['instance_id'], 'inst-1')
        self.assertEqual(captured['action']['step_index'], 0)
        self.assertEqual(captured['action']['steps'][0]['args'],
                         {'duration': 10})

    def test_missing_execution_keeps_legacy_context(self):
        from action import service as action_service

        captured = {}

        def handler(args, ctx):
            captured.update(ctx)
            return {}

        registry = {'fake.cmd': {'params': [], 'handler': handler}}
        step = {'command': 'fake.cmd', 'args': {}, 'delay': 0}
        run = action_service.new_run('record_600rpm')

        with patch.object(action_service, 'get_registry',
                          return_value=registry):
            action_service.execute_step(step, run, 'record_600rpm')

        self.assertEqual(captured['action_id'], 'record_600rpm')
        self.assertNotIn('action', captured)


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

class RecorderCommandTests(SimpleTestCase):
    def _ctx(self):
        return {
            'run_id': 'run-1',
            'action_id': 'record_600rpm',
            'step_index': 1,
            'step_count': 2,
            'action': ACTION_CONTEXT,
        }

    def test_success_returns_record_fields(self):
        with patch.object(commands.service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(commands.service, 'record',
                             return_value=SIDECAR) as record:
            result = commands.record({'duration': 10}, self._ctx())

        self.assertEqual(result['record_id'], SIDECAR['record_id'])
        self.assertEqual(result['pcap_file'], SIDECAR['pcap_file'])
        self.assertEqual(result['elapsed_seconds'], 10.5)
        self.assertEqual(result['sidecar'],
                         f"{SIDECAR['record_id']}.record.json")
        self.assertEqual(record.call_args.kwargs['timeout'], 40)

    def test_missing_action_context_rejected(self):
        with patch.object(commands.service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(commands.service, 'record') as record:
            with self.assertRaises(commands.CommandError) as ctx:
                commands.record({'duration': 10},
                                {'run_id': 'run', 'action_id': 'x'})
        self.assertEqual(ctx.exception.code, 'invalid_args')
        record.assert_not_called()

    def test_daemon_codes_are_mapped(self):
        mapping = {
            'recorder_busy': 'recorder_busy',
            'calibration_invalid': 'calibration_invalid',
            'artifact_unavailable': 'artifact_unavailable',
            'config_invalid': 'config_error',
            'lidar_standby': 'device_standby',
            'lidar_unreachable': 'device_unreachable',
            'capture_tool_missing': 'capture_unavailable',
            'capture_failed': 'capture_failed',
        }
        for daemon_code, command_code in mapping.items():
            with self.subTest(daemon_code=daemon_code):
                with patch.object(commands.service, 'load_config',
                                  return_value=RECORDER_CONFIG), \
                        patch.object(commands.service, 'record',
                                     side_effect=service.RecorderError(
                                         daemon_code, 'x')):
                    with self.assertRaises(commands.CommandError) as ctx:
                        commands.record({'duration': 10}, self._ctx())
                self.assertEqual(ctx.exception.code, command_code)

    def test_unreachable_maps_to_recorder_unreachable(self):
        with patch.object(commands.service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(commands.service, 'record',
                             side_effect=service.RecorderUnreachableError('x')):
            with self.assertRaises(commands.CommandError) as ctx:
                commands.record({'duration': 10}, self._ctx())
        self.assertEqual(ctx.exception.code, 'recorder_unreachable')

    def test_failed_sidecar_raises(self):
        failed = dict(SIDECAR, status='failed',
                      error={'code': 'capture_failed', 'message': 'boom'})
        with patch.object(commands.service, 'load_config',
                          return_value=RECORDER_CONFIG), \
                patch.object(commands.service, 'record', return_value=failed):
            with self.assertRaises(commands.CommandError) as ctx:
                commands.record({'duration': 10}, self._ctx())
        self.assertEqual(ctx.exception.code, 'capture_failed')


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

class RecorderViewTests(SimpleTestCase):
    def _overview(self):
        return {
            'overview': {
                'recording_enabled': True,
                'used_bytes': 1024,
                'quota_bytes': 2048,
                'blocked_reason': None,
                'local_folder': 'C:\\captures',
            }
        }

    def _patches(self, status_result=None, status_error=None):
        stack = [
            patch.object(recorder_daemon, 'logger'),
            patch('recorder.views.artifact_service.get_card',
                  return_value=self._overview()),
            patch('recorder.views.calibration.load_calibration',
                  return_value={'values': {
                      'alpha1_degrees': 1.5,
                      'alpha2_degrees': -1.5,
                      'arm_meter': 0.12,
                      'calibrated_at': '2026-11-20T02:00:00Z',
                  }, 'mtime': 'm', 'sha256': 's'}),
            patch('recorder.views.service.load_config',
                  return_value=RECORDER_CONFIG),
        ]
        if status_error is not None:
            stack.append(patch('recorder.views.service.status',
                               side_effect=status_error))
        else:
            stack.append(patch('recorder.views.service.status',
                               return_value=status_result or {
                                   'status': 'idle'}))
        return stack

    def test_index_idle(self):
        stack = self._patches()
        for item in stack:
            item.start()
        try:
            response = Client().get(reverse('recorder:index'))
        finally:
            for item in reversed(stack):
                item.stop()

        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8')
        self.assertIn('Recorder', body)
        self.assertIn('WLAN 2', body)
        self.assertIn('当前无录制', body)
        self.assertNotIn('开始录制', body)
        self.assertNotIn('删除', body)

    def test_index_offline(self):
        stack = self._patches(status_error=service.RecorderUnreachableError('down'))
        for item in stack:
            item.start()
        try:
            response = Client().get(reverse('recorder:index'))
        finally:
            for item in reversed(stack):
                item.stop()

        body = response.content.decode('utf-8')
        self.assertIn('守护进程离线', body)
        self.assertIn('alpha1', body)

    def test_index_recording(self):
        stack = self._patches(status_result={
            'status': 'recording',
            'record_id': 'record-1',
            'pcap_file': 'record-1.pcap',
            'elapsed_seconds': 3.2,
        })
        for item in stack:
            item.start()
        try:
            response = Client().get(reverse('recorder:index'))
        finally:
            for item in reversed(stack):
                item.stop()

        body = response.content.decode('utf-8')
        self.assertIn('record-1', body)
        self.assertIn('record-1.pcap', body)
        self.assertIn('3.2', body)

    def test_config_error_keeps_artifact_and_calibration(self):
        stack = [
            patch('recorder.views.artifact_service.get_card',
                  return_value=self._overview()),
            patch('recorder.views.calibration.load_calibration',
                  return_value={'values': {'alpha1_degrees': 1.5},
                                'mtime': 'm', 'sha256': 's'}),
            patch('recorder.views.service.load_config',
                  side_effect=service.RecorderConfigError('bad config')),
        ]
        for item in stack:
            item.start()
        try:
            response = Client().get(reverse('recorder:index'))
        finally:
            for item in reversed(stack):
                item.stop()

        body = response.content.decode('utf-8')
        self.assertIn('配置错误', body)
        self.assertIn('输出目录可用', body)
        self.assertIn('alpha1', body)

    def test_card_fragment(self):
        stack = self._patches()
        for item in stack:
            item.start()
        try:
            response = Client().get(reverse('recorder:card'))
        finally:
            for item in reversed(stack):
                item.stop()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<section', response.content.decode('utf-8'))


# ---------------------------------------------------------------------------
# artifact 删除保护
# ---------------------------------------------------------------------------

class ArtifactDeleteProtectionTests(SimpleTestCase):
    def test_recording_file_cannot_be_deleted(self):
        from artifact import service as artifact_service

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'record-1.pcap'
            path.write_bytes(b'data')
            with patch.object(artifact_service, 'resolve_file', return_value=path), \
                    patch('recorder.service.recording_pcap',
                          return_value='record-1.pcap'):
                error = artifact_service.delete_file('record-1.pcap')

            self.assertIn('正在录制', error)
            self.assertTrue(path.exists())

    def test_daemon_offline_allows_delete(self):
        from artifact import service as artifact_service

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'record-1.pcap'
            path.write_bytes(b'data')
            with patch.object(artifact_service, 'resolve_file', return_value=path), \
                    patch('recorder.service.recording_pcap', return_value=None):
                error = artifact_service.delete_file('record-1.pcap')

            self.assertIsNone(error)
            self.assertFalse(path.exists())
