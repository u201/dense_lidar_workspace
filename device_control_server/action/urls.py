"""action URL 配置。"""

from django.urls import path

from . import views

app_name = 'action'

urlpatterns = [
    path('', views.index, name='index'),
    path('frag/card', views.card, name='card'),
    path('instances', views.instances, name='instances'),
    path('instances/live', views.instances_live, name='instances_live'),
    path('instances/<str:instance_id>/result',
         views.instance_result, name='instance_result'),
    path('instances/<str:instance_id>/cancel', views.cancel, name='cancel'),
    path('instances/<str:instance_id>/abort', views.abort, name='abort'),
    path('<str:action_id>/run', views.run, name='run'),
    path('<str:action_id>/schedule', views.schedule, name='schedule'),
]
