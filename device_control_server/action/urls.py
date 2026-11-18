"""action URL 配置。"""

from django.urls import path

from . import views

app_name = 'action'

urlpatterns = [
    path('', views.index, name='index'),
    path('<str:action_id>/run', views.run, name='run'),
]
