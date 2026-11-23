"""Action 视图：htmx 整页、执行结果片段与执行状态队列。"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from . import service


def _instances_context(error: str | None = None) -> dict:
    instances = service.list_instances()
    return {
        'instances': instances,
        'has_active': any(not item['terminal'] for item in instances),
        'error': error,
    }


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /action/ —— 首屏直接渲染全部 action 卡片与执行状态。"""
    actions, file_error = service.load_actions()
    context = _instances_context()
    context.update({'actions': actions, 'file_error': file_error})
    return render(request, 'action/index.html', context)


@require_http_methods(['GET'])
def instances(request: HttpRequest) -> HttpResponse:
    """GET /action/instances —— 执行状态片段（进行中 + 最近完成）。"""
    return render(request, 'action/_instances.html', _instances_context())


@require_http_methods(['GET'])
def instances_live(request: HttpRequest) -> HttpResponse:
    """GET /action/instances/live —— 轮询片段，不替换滚动容器。"""
    return render(request, 'action/_instances_live.html',
                  _instances_context())


def _result_context(instance: dict | None, error: str | None) -> dict:
    return {
        'instance': service.instance_view(instance) if instance else None,
        'error': error,
    }


@require_http_methods(['POST'])
def run(request: HttpRequest, action_id: str) -> HttpResponse:
    """POST /action/<id>/run —— 创建立即执行实例，结果区轮询到终态。"""
    instance, error = service.submit_action(
        action_id, service.parse_overrides(request.POST))

    response = render(request, 'action/_instance_result.html',
                      _result_context(instance, error))
    if instance is not None:
        # 执行状态卡片监听该事件，局部刷新
        response['HX-Trigger'] = 'instances-refresh'
    return response


@require_http_methods(['GET'])
def instance_result(request: HttpRequest, instance_id: str) -> HttpResponse:
    """GET /action/instances/<id>/result —— 单实例结果轮询。"""
    instance = service.load_instance(instance_id)
    error = None if instance is not None else '实例不存在'
    return render(request, 'action/_instance_result.html',
                  _result_context(instance, error))


@require_http_methods(['POST'])
def schedule(request: HttpRequest, action_id: str) -> HttpResponse:
    """POST /action/<id>/schedule —— 落盘预约实例，不立即执行。"""
    instance, error = service.schedule_action(
        action_id, request.POST.get('scheduled_at', ''),
        service.parse_overrides(request.POST))

    response = render(request, 'action/_schedule_result.html', {
        'instance': instance,
        'error': error,
    })
    if instance is not None:
        # 执行状态卡片监听该事件，局部刷新
        response['HX-Trigger'] = 'instances-refresh'
    return response


@require_http_methods(['POST'])
def cancel(request: HttpRequest, instance_id: str) -> HttpResponse:
    """POST /action/instances/<id>/cancel —— 取消尚未执行的预约。"""
    error = service.cancel_instance(instance_id)
    return render(request, 'action/_instances_live.html',
                  _instances_context(error))
