"""artifact URL 配置。"""

from django.urls import path

from . import views

app_name = 'artifact'

urlpatterns = [
    path('', views.index, name='index'),
    path('frag/card', views.card, name='card'),
    path('files/<str:filename>/download', views.download, name='download'),
    path('files/<str:filename>/delete', views.delete, name='delete'),
]
