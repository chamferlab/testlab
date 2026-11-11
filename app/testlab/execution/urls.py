from django.urls import path

from . import import_views, views

app_name = "execution"

urlpatterns = [
    path("runs/<int:run_pk>/log/", views.ManualLogView.as_view(), name="log"),
    path("log/<int:pk>/edit/", views.EntryEditView.as_view(), name="entry_edit"),
    path("log/<int:pk>/delete/", views.EntryDeleteView.as_view(), name="entry_delete"),
    path("runs/<int:run_pk>/log/import/", import_views.LogImportView.as_view(), name="log_import"),
    path("log/imports/<int:pk>/", import_views.LogImportDetailView.as_view(), name="log_import_detail"),
    path("log/imports/<int:pk>/mapping/", import_views.LogImportMappingView.as_view(), name="log_import_mapping"),
    path("log/imports/<int:pk>/preview/", import_views.LogImportPreviewView.as_view(), name="log_import_preview"),
    path("log/imports/<int:pk>/undo/", import_views.LogImportUndoView.as_view(), name="log_import_undo"),
    path("log/imports/<int:pk>/discard/", import_views.LogImportDiscardView.as_view(), name="log_import_discard"),
    path("log/imports/<int:pk>/file/", import_views.LogImportFileView.as_view(), name="log_import_file"),
    path("runs/<int:run_pk>/dcs/", views.DcsListView.as_view(), name="dcs"),
    path("dcs/<int:pk>/", views.DcsDetailView.as_view(), name="dcs_detail"),
    path("dcs/<int:pk>/running/", views.DcsRunningView.as_view(), name="dcs_running"),
    path("dcs/<int:pk>/data/", views.DcsDataView.as_view(), name="dcs_data"),
    path("dcs/<int:pk>/raw/", views.DcsRawView.as_view(), name="dcs_raw"),
    path("dcs/<int:pk>/delete/", views.DcsDeleteView.as_view(), name="dcs_delete"),
]
