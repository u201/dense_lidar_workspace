"""Lidar service / 视图 / 模板 / 命令测试（不访问真实设备）。"""

import json
import urllib.error
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from action.registry import CommandError
from lidar import commands
from lidar.service import (
    LidarArgumentError, LidarResponseError, LidarUnreachableError,
    get_device_info, get_spin_rate, get_standby_mode,
    set_spin_rate, set_standby_mode,
)

CFG = {'host': '1.2.3.4', 'port': 80, 'timeout': 1.5}


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _payload(body: dict, error_code='0') -> bytes:
    return json.dumps({
        'Head': {'ErrorCode': error_code, 'Message': 'Success'},
        'Body': body,
    }).encode('utf-8')


@patch('lidar.service.urllib.request.urlopen')
class DeviceInfoTests(SimpleTestCase):
    def test_success_returns_sn_only(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({
            'SN': 'device-sn', 'ProdName': 'PandarXT-16', 'Mac': 'x',
        }))
        self.assertEqual(get_device_info(CFG), {'SN': 'device-sn'})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(
            request.full_url,
            'http://1.2.3.4:80/pandar.cgi?action=get&object=device_info')
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 1.5)

    def test_error_code_non_zero(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({'SN': 'x'}, '1'))
        with self.assertRaises(LidarResponseError) as ctx:
            get_device_info(CFG)
        self.assertEqual(ctx.exception.error_code, '1')

    def test_http_error_is_unreachable(self, urlopen):
        urlopen.return_value = FakeResponse(b'', 500)
        with self.assertRaises(LidarUnreachableError):
            get_device_info(CFG)

    def test_connection_error_is_unreachable(self, urlopen):
        urlopen.side_effect = urllib.error.URLError('refused')
        with self.assertRaises(LidarUnreachableError):
            get_device_info(CFG)

    def test_timeout_is_unreachable(self, urlopen):
        urlopen.side_effect = TimeoutError('timed out')
        with self.assertRaises(LidarUnreachableError):
            get_device_info(CFG)

    def test_non_json(self, urlopen):
        urlopen.return_value = FakeResponse(b'not json')
        with self.assertRaises(LidarResponseError):
            get_device_info(CFG)

    def test_top_level_not_dict(self, urlopen):
        urlopen.return_value = FakeResponse(b'[]')
        with self.assertRaises(LidarResponseError):
            get_device_info(CFG)

    def test_body_not_dict(self, urlopen):
        urlopen.return_value = FakeResponse(
            b'{"Head": {"ErrorCode": "0"}, "Body": []}')
        with self.assertRaises(LidarResponseError):
            get_device_info(CFG)

    def test_sn_missing_or_invalid(self, urlopen):
        for body in ({}, {'SN': ''}, {'SN': '   '}, {'SN': 1}):
            urlopen.return_value = FakeResponse(_payload(body))
            with self.assertRaises(LidarResponseError):
                get_device_info(CFG)


@patch('lidar.service.urllib.request.urlopen')
class StandbyTests(SimpleTestCase):
    def test_get_string_mode(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({'standbymode': '1'}))
        self.assertEqual(get_standby_mode(CFG), 1)

    def test_get_bool_is_invalid(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({'standbymode': True}))
        with self.assertRaises(LidarResponseError):
            get_standby_mode(CFG)

    def test_set_uses_post_and_value(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({}))
        set_standby_mode(1, CFG)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.data, b'')
        self.assertEqual(
            request.full_url,
            'http://1.2.3.4:80/pandar.cgi'
            '?action=set&object=lidar_data&key=standbymode&value=1')

    def test_set_invalid_mode(self, urlopen):
        with self.assertRaises(LidarArgumentError):
            set_standby_mode(2, CFG)
        urlopen.assert_not_called()


@patch('lidar.service.urllib.request.urlopen')
class SpinRateTests(SimpleTestCase):
    def test_get_string_index(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({'SpinSpeed': '2'}))
        self.assertEqual(get_spin_rate(CFG), {'index': 2, 'rpm': 600})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            'http://1.2.3.4:80/pandar.cgi?action=get&object=lidar_config')

    def test_get_int_index(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({'SpinSpeed': 3}))
        self.assertEqual(get_spin_rate(CFG), {'index': 3, 'rpm': 1200})

    def test_get_invalid_index(self, urlopen):
        for body in ({}, {'SpinSpeed': '4'}, {'SpinSpeed': 'abc'},
                     {'SpinSpeed': True}, {'SpinSpeed': None}):
            urlopen.return_value = FakeResponse(_payload(body))
            with self.assertRaises(LidarResponseError):
                get_spin_rate(CFG)

    def test_set_maps_rpm_to_index(self, urlopen):
        urlopen.return_value = FakeResponse(_payload({}))
        set_spin_rate(600, CFG)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(
            request.full_url,
            'http://1.2.3.4:80/pandar.cgi'
            '?action=set&object=lidar&key=spin_speed&value=2')

    def test_set_invalid_rpm(self, urlopen):
        for rpm in (2400, 0, True):
            with self.assertRaises(LidarArgumentError):
                set_spin_rate(rpm, CFG)
        urlopen.assert_not_called()


@patch('lidar.views.load_config', return_value=dict(CFG))
@patch('lidar.views.get_spin_rate')
@patch('lidar.views.get_standby_mode')
@patch('lidar.views.get_device_info')
class ViewTests(TestCase):
    def setUp(self):
        self.url = reverse('lidar:index')

    def test_all_fields_ok(self, device_info, standby, spin, load_config):
        device_info.return_value = {'SN': 'device-sn'}
        standby.return_value = 1
        spin.return_value = {'index': 2, 'rpm': 600}

        response = self.client.get(self.url)
        self.assertContains(response, '在线')
        self.assertContains(response, 'device-sn')
        self.assertContains(response, '待机')
        self.assertContains(response, '600 rpm')

    def test_probe_unreachable_short_circuits(
            self, device_info, standby, spin, load_config):
        device_info.side_effect = LidarUnreachableError('timeout')

        response = self.client.get(self.url)
        self.assertContains(response, '离线')
        standby.assert_not_called()
        spin.assert_not_called()

    def test_probe_response_error(self, device_info, standby, spin, load_config):
        device_info.side_effect = LidarResponseError('bad body')

        response = self.client.get(self.url)
        self.assertContains(response, '设备异常')
        standby.assert_not_called()
        spin.assert_not_called()

    def test_standby_failure_degrades_only_itself(
            self, device_info, standby, spin, load_config):
        device_info.return_value = {'SN': 'device-sn'}
        standby.side_effect = LidarResponseError('standby bad')
        spin.return_value = {'index': 2, 'rpm': 600}

        response = self.client.get(self.url)
        self.assertContains(response, '在线')
        self.assertContains(response, 'device-sn')
        self.assertContains(response, 'standby bad')
        self.assertContains(response, '600 rpm')
        self.assertNotContains(response, '待机模式')

    def test_spin_failure_degrades_only_itself(
            self, device_info, standby, spin, load_config):
        device_info.return_value = {'SN': 'device-sn'}
        standby.return_value = 0
        spin.side_effect = LidarUnreachableError('spin timeout')

        response = self.client.get(self.url)
        self.assertContains(response, '在线')
        self.assertContains(response, 'device-sn')
        self.assertContains(response, '运行 In Operation')
        self.assertContains(response, 'spin timeout')
        self.assertNotContains(response, '转速')

    def test_config_error_skips_all_requests(
            self, device_info, standby, spin, load_config):
        from lidar.service import LidarConfigError
        load_config.side_effect = LidarConfigError('missing lidar')

        response = self.client.get(self.url)
        self.assertContains(response, '配置错误')
        device_info.assert_not_called()
        standby.assert_not_called()
        spin.assert_not_called()

    def test_card_fragment_refreshes_content(
            self, device_info, standby, spin, load_config):
        device_info.return_value = {'SN': 'sn-1'}
        standby.return_value = 1
        spin.return_value = {'index': 1, 'rpm': 300}
        first = self.client.get(reverse('lidar:card'))
        self.assertContains(first, 'sn-1')

        device_info.return_value = {'SN': 'sn-2'}
        second = self.client.get(reverse('lidar:card'))
        self.assertContains(second, 'sn-2')


class SpinCommandTests(SimpleTestCase):
    def setUp(self):
        self.handler = commands.COMMANDS['lidar.spin']['handler']

    @patch('lidar.commands.set_spin_rate')
    @patch('lidar.commands.load_config', return_value=dict(CFG))
    def test_success(self, load_config, set_spin_rate):
        result = self.handler({'rpm': 600}, {})
        set_spin_rate.assert_called_once_with(600, CFG)
        self.assertEqual(result, {'rpm': 600, 'index': 2,
                                  'device': '1.2.3.4:80'})

    @patch('lidar.commands.set_spin_rate')
    @patch('lidar.commands.load_config', return_value=dict(CFG))
    def test_invalid_rpm(self, load_config, set_spin_rate):
        with self.assertRaises(CommandError) as ctx:
            self.handler({'rpm': 2400}, {})
        self.assertEqual(ctx.exception.code, 'invalid_args')
        set_spin_rate.assert_not_called()

    @patch('lidar.commands.set_spin_rate',
           side_effect=LidarUnreachableError('timeout'))
    @patch('lidar.commands.load_config', return_value=dict(CFG))
    def test_device_unreachable(self, load_config, set_spin_rate):
        with self.assertRaises(CommandError) as ctx:
            self.handler({'rpm': 300}, {})
        self.assertEqual(ctx.exception.code, 'device_unreachable')

    @patch('lidar.commands.set_spin_rate',
           side_effect=LidarResponseError('bad', error_code='3', body={}))
    @patch('lidar.commands.load_config', return_value=dict(CFG))
    def test_device_error(self, load_config, set_spin_rate):
        with self.assertRaises(CommandError) as ctx:
            self.handler({'rpm': 300}, {})
        self.assertEqual(ctx.exception.code, 'device_error')
        self.assertEqual(ctx.exception.detail['error_code'], '3')
