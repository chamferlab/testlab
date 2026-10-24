from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="projects:list"), name="home"),
    path("projects/", include("testlab.projects.urls")),
    path("", include("testlab.tcn.urls")),
    path("", include("testlab.execution.urls")),
    path("", include("testlab.results.urls")),
    path("", include("testlab.reports.urls")),
    path("planning/", include("testlab.planning.urls")),
    path("accounts/", include("testlab.accounts.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    # Admin: user management and approval of registered accounts (superusers).
    path("admin/", admin.site.urls),
]
