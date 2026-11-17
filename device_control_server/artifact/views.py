"""Artifact 视图：htmx 整页/片段 + 文件下载/删除。"""

from datetime import datetime, timezone

from django.http import FileResponse, HttpRequest, Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from . import service


def _card_context() -> dict:
    """取卡片数据，并把 epoch 秒转成 datetime 供模板 date 过滤器使用。

    service 只输出原始单位（bytes、epoch 秒），展示层转换集中在这里。
    """
    card = service.get_card()
    for entry in card['files']:
        entry['mtime'] = datetime.fromtimestamp(entry['mtime_epoch'], tz=timezone.utc)
    return card


@require_http_methods(['GET'])
def index(request: HttpRequest) -> HttpResponse:
    """GET /artifact/ —— 独立页，首屏服务端渲染完整内容（不靠 load 再请求一次）。"""
    return render(request, 'artifact/index.html', _card_context())


@require_http_methods(['GET'])
def card(request: HttpRequest) -> HttpResponse:
    """GET /artifact/frag/card —— 手动刷新按钮对应的片段。"""
    return render(request, 'artifact/_card_body.html', _card_context())


@require_http_methods(['GET'])
def download(request: HttpRequest, filename: str) -> HttpResponse:
    """GET /artifact/files/<name>/download —— 流式下载，大文件不进内存。"""
    path = service.resolve_file(filename)
    if path is None:
        raise Http404
    response = FileResponse(
        open(path, 'rb'),
        as_attachment=True,
        filename=path.name,
        content_type='application/octet-stream',
    )
    # 默认 4KB 块对 GB 级文件迭代次数太多，调大到 256KB
    response.block_size = 256 * 1024
    return response


@require_http_methods(['POST'])
def delete(request: HttpRequest, filename: str) -> HttpResponse:
    """POST /artifact/files/<name>/delete —— 删除后重渲染本卡 body，卡内闭环。"""
    error = service.delete_file(filename)
    context = _card_context()
    if error is not None:
        context['delete_error'] = error
    return render(request, 'artifact/_card_body.html', context)
