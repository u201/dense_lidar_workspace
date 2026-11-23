from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.http import HttpRequest


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /turntable/ —— 独立页，首屏同步探测后渲染完整内容。"""
    return render(request, 'turntable/index.html', {})


@require_http_methods(['GET'])
def card(request: HttpRequest) -> HttpResponse:
    """GET /turntable/frag/card —— 手动刷新按钮对应的片段。"""
    return render(request, 'turntable/_card_body.html', {})
