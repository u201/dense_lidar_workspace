"""Recorder 视图：只读卡片，展示守护进程状态、配置、配额与标定。

web 不触发录制、不删除文件；录制只能由 action 调用 recorder.record。
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from artifact import service as artifact_service

from . import calibration, service


def _card_context() -> dict:
    """汇总本地配置、artifact 配额、标定与 daemon 状态。

    artifact 与 calibration 各自独立降级，不因一项失败抹掉其他项。
    """
    context = {
        'status': 'ok',
        'status_label': '正常',
        'detail': None,
        'config': {'interface': None, 'udp_port': None, 'folder': None},
        'artifact': {
            'ready': False,
            'used_bytes': 0,
            'quota_bytes': 0,
            'blocked_reason': None,
        },
        'calibration': {'ok': False, 'values': None, 'error': None},
        'daemon': {
            'reachable': False,
            'recording': False,
            'record_id': None,
            'pcap_file': None,
            'elapsed_seconds': None,
        },
    }

    overview = artifact_service.get_card()['overview']
    context['artifact'] = {
        'ready': bool(overview.get('recording_enabled')),
        'used_bytes': overview.get('used_bytes', 0),
        'quota_bytes': overview.get('quota_bytes') or 0,
        'blocked_reason': overview.get('blocked_reason'),
    }
    context['config']['folder'] = (overview.get('local_folder')
                                   or overview.get('local_folder_configured'))

    try:
        snapshot = calibration.load_calibration()
    except calibration.CalibrationError as exc:
        context['calibration'] = {
            'ok': False, 'values': None, 'error': str(exc)}
    else:
        context['calibration'] = {
            'ok': True, 'values': snapshot['values'], 'error': None}

    try:
        cfg = service.load_config()
    except service.RecorderConfigError as exc:
        context['status'] = 'config_error'
        context['status_label'] = '配置错误'
        context['detail'] = exc.message
        return context

    context['config']['interface'] = cfg['interface']
    context['config']['udp_port'] = cfg['udp_port']

    try:
        snapshot = service.status(cfg=cfg)
    except service.RecorderError as exc:
        context['status'] = 'offline'
        context['status_label'] = '守护进程离线'
        context['detail'] = exc.message
        return context

    context['daemon'] = {
        'reachable': True,
        'recording': snapshot.get('status') == 'recording',
        'record_id': snapshot.get('record_id'),
        'pcap_file': snapshot.get('pcap_file'),
        'elapsed_seconds': snapshot.get('elapsed_seconds'),
    }
    return context


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /recorder/ —— 独立页，首屏同步渲染只读卡片。"""
    return render(request, 'recorder/index.html', _card_context())


@require_http_methods(['GET'])
def card(request: HttpRequest) -> HttpResponse:
    """GET /recorder/frag/card —— 手动刷新按钮对应的片段。"""
    return render(request, 'recorder/_card_body.html', _card_context())
