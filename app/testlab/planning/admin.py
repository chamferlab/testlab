"""Test units are managed in the admin; bookings and blocks on the lab plan."""

from django.contrib import admin

from .models import TestUnit


@admin.register(TestUnit)
class TestUnitAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "active", "sort_order"]
    list_editable = ["active", "sort_order"]
