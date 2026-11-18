"""Lidar 视图：htmx 整页/片段，显示设备在线状态与待机模式。"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from drivers import lidar as lidar_driver

MODE_DISPLAY = {
    lidar_driver.MODE_IN_OPERATION: '运行 In Operation',
    lidar_driver.MODE_STANDBY: '待机 Standby',
}


def _status_context() -> dict:
    """探测设备并把结果映射成模板可直接用的状态。

    每次请求都实时查询，不缓存：页面上的“在线”必须是本次探测的结果。
    """
    try:
        cfg = lidar_driver.load_config()
    except lidar_driver.LidarConfigError as exc:
        return {
            'status': 'config_error',
            'status_label': '配置错误',
            'device': None,
            'mode': None,
            'mode_label': None,
            'detail': str(exc),
        }

    device = f'{cfg["host"]}:{cfg["port"]}'
    try:
        mode = lidar_driver.get_standby_mode(cfg)
    except lidar_driver.LidarUnreachableError as exc:
        return {
            'status': 'offline',
            'status_label': '离线',
            'device': device,
            'mode': None,
            'mode_label': None,
            'detail': str(exc),
        }
    except lidar_driver.LidarResponseError as exc:
        return {
            'status': 'abnormal',
            'status_label': '设备异常',
            'device': device,
            'mode': None,
            'mode_label': None,
            'detail': str(exc),
        }

    return {
        'status': 'ok',
        'status_label': '在线',
        'device': device,
        'mode': mode,
        'mode_label': MODE_DISPLAY[mode],
        'detail': None,
    }


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /lidar/ —— 独立页，首屏同步探测后渲染完整内容。"""
    return render(request, 'lidar/index.html', _status_context())


@require_http_methods(['GET'])
def card(request: HttpRequest) -> HttpResponse:
    """GET /lidar/frag/card —— 手动刷新按钮对应的片段。"""
    return render(request, 'lidar/_card_body.html', _status_context())
