"""Lidar 视图：htmx 整页/片段，显示设备在线状态、SN、待机模式与转速。"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from lidar.service import (
    MODE_IN_OPERATION, MODE_STANDBY,
    LidarConfigError, LidarUnreachableError, LidarResponseError,
    load_config, get_device_info, get_standby_mode, get_spin_rate,
)

MODE_DISPLAY = {
    MODE_IN_OPERATION: '运行 In Operation',
    MODE_STANDBY: '待机 Standby',
}

_FIELD_ERRORS = (LidarUnreachableError, LidarResponseError)


def _context(status: str, status_label: str,
             device: str | None = None, **extra) -> dict:
    context = {
        'status': status,
        'status_label': status_label,
        'device': device,
        'detail': None,
        'device_info': None,
        'mode': None,
        'mode_label': None,
        'spin': None,
    }
    context.update(extra)
    return context


def _status_context() -> dict:
    """探测设备并把结果映射成模板可直接用的状态。

    每次请求都实时查询，不缓存：页面上的“在线”必须是本次探测的结果。
    只有探活（device_info）失败才短路；standby / 转速失败只降级自身。
    """
    try:
        cfg = load_config()
    except LidarConfigError as exc:
        return _context('config_error', '配置错误', detail=str(exc))

    device = f'{cfg["host"]}:{cfg["port"]}'
    try:
        info = get_device_info(cfg)
    except LidarUnreachableError as exc:
        return _context('offline', '离线', device, detail=str(exc))
    except LidarResponseError as exc:
        return _context('abnormal', '设备异常', device, detail=str(exc))

    context = _context('ok', '在线', device, device_info=info)
    details = []

    try:
        mode = get_standby_mode(cfg)
    except _FIELD_ERRORS as exc:
        details.append(str(exc))
    else:
        context['mode'] = mode
        context['mode_label'] = MODE_DISPLAY[mode]

    try:
        context['spin'] = get_spin_rate(cfg)
    except _FIELD_ERRORS as exc:
        details.append(str(exc))

    if details:
        context['detail'] = '；'.join(details)
    return context


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /lidar/ —— 独立页，首屏同步探测后渲染完整内容。"""
    return render(request, 'lidar/index.html', _status_context())


@require_http_methods(['GET'])
def card(request: HttpRequest) -> HttpResponse:
    """GET /lidar/frag/card —— 手动刷新按钮对应的片段。"""
    return render(request, 'lidar/_card_body.html', _status_context())
