from django.urls import path

from . import views


app_name = "market"

urlpatterns = [
    path("", views.market_list, name="listing_list"),
    path("<int:listing_id>/", views.market_detail, name="listing_detail"),
]
