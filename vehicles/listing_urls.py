from django.urls import path

from . import views


urlpatterns = [
    path("classification-review/", views.classification_review, name="classification_review"),
]
