from django.urls import path

from . import views


app_name = "market_profiles"

urlpatterns = [
    path("", views.market_profile_list, name="list"),
    path("new/", views.market_profile_create, name="create"),
    path("<int:profile_id>/", views.market_profile_detail, name="detail"),
    path("<int:profile_id>/edit/", views.market_profile_edit, name="edit"),
]
