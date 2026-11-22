"""recorder URL 配置。"""

from django.urls import path

from . import views

app_name = 'recorder'

urlpatterns = [
    path('', views.index, name='index'),
    path('frag/card', views.card, name='card'),
]
