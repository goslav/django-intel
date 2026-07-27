from django.urls import path

from . import views

urlpatterns = [
    path("", views.source_search_list, name="source_search_list"),
    path("new/", views.source_search_create, name="source_search_create"),
    path("<int:search_id>/", views.source_search_detail, name="source_search_detail"),
    path("<int:search_id>/edit/", views.source_search_edit, name="source_search_edit"),
    path("runs/<int:run_id>/", views.source_search_run_detail, name="source_search_run_detail"),
]
