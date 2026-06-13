from django.contrib import admin
from django.urls import path

from core.views import DashboardView, OptimizeRouteView

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("admin/", admin.site.urls),
    path("api/optimize/", OptimizeRouteView.as_view(), name="optimize-route"),
]
