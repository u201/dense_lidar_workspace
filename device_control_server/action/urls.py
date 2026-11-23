"""action URL 配置。"""

from django.urls import path

from . import views

app_name = 'action'

urlpatterns = [
    path('', views.index, name='index'),
    path('instances', views.instances, name='instances'),
    path('instances/<str:instance_id>/cancel', views.cancel, name='cancel'),
    path('<str:action_id>/run', views.run, name='run'),
    path('<str:action_id>/schedule', views.schedule, name='schedule'),
]
