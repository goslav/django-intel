from django.urls import path

from . import views

app_name = "dealers"

urlpatterns = [
    path("", views.dealer_list, name="list"),
    path("activity/", views.dealer_activity, name="activity"),
    path("compare/", views.dealer_model_comparison, name="model_comparison"),
    path("compare/<int:dealer_id>/", views.dealer_model_comparison_detail, name="model_comparison_detail"),
    path("<int:dealer_id>/", views.dealer_detail, name="detail"),
    path("<int:dealer_id>/snapshots/", views.dealer_snapshot_history, name="snapshot_history"),
    path("<int:dealer_id>/snapshots/<int:snapshot_id>/", views.dealer_snapshot_detail, name="snapshot_detail"),
]
