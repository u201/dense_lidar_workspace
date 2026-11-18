"""Action 视图：htmx 整页与单卡片执行结果片段。"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from . import service


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /action/ —— 首屏直接渲染全部 action 卡片。"""
    actions, file_error = service.load_actions()
    return render(request, 'action/index.html', {
        'actions': actions,
        'file_error': file_error,
    })


@require_http_methods(['POST'])
def run(request: HttpRequest, action_id: str) -> HttpResponse:
    """POST /action/<id>/run —— 同步执行，仅替换卡片内结果区。"""
    result = service.execute_action(
        action_id, service.parse_overrides(request.POST))
    succeeded = result['status'] == 'succeeded'
    result['status_label'] = '执行成功' if succeeded else '执行失败'
    result['status_class'] = 'ok' if succeeded else 'abnormal'

    response = render(request, 'action/_run_result.html', {'run': result})
    if succeeded:
        # 同屏存在 lidar 卡片时实时刷新；没有该卡片时事件自然无效
        response['HX-Trigger'] = 'lidar-refresh'
    return response
