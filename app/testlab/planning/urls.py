from django.urls import path

from . import views

app_name = "planning"

urlpatterns = [
    path("", views.PlanView.as_view(), name="plan"),
    path("bookings/new/", views.BookingEditView.as_view(), name="booking_create"),
    path("bookings/<int:pk>/", views.BookingEditView.as_view(), name="booking_edit"),
    path("bookings/<int:pk>/delete/", views.BookingDeleteView.as_view(), name="booking_delete"),
    path("blocks/new/", views.UnitBlockEditView.as_view(), name="block_create"),
    path("blocks/<int:pk>/", views.UnitBlockEditView.as_view(), name="block_edit"),
    path("blocks/<int:pk>/delete/", views.UnitBlockDeleteView.as_view(), name="block_delete"),
]
