from django import forms
from django.utils import timezone

from . import weeks
from .models import Booking, TestUnit, UnitBlock


class WeeksForm(forms.ModelForm):
    """Common to bookings and unit blocks: the unit and the first and last week, picked by ISO week."""

    start_week = forms.ChoiceField(label="First week")
    end_week = forms.ChoiceField(label="Last week")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        # Inactive units take no new bookings; a period already on one keeps it.
        units = TestUnit.objects.filter(active=True)
        if instance.unit_id:
            units = units | TestUnit.objects.filter(pk=instance.unit_id)
        self.fields["unit"].queryset = units.distinct()
        self.fields["unit"].empty_label = None
        self.fields["unit"].label_from_instance = lambda unit: f"{unit.code} · {unit.name}" if unit.name else unit.code
        this_year = weeks.week_of(timezone.localdate())[0]
        first, last = weeks.FIRST_YEAR, max(this_year, weeks.FIRST_YEAR) + 2
        if instance.pk:
            first = min(first, weeks.week_of(instance.start)[0])
            last = max(last, weeks.week_of(instance.end)[0])
            self.initial.setdefault("start_week", weeks.label(*weeks.week_of(instance.start)))
            self.initial.setdefault("end_week", weeks.label(*weeks.week_of(instance.end)))
        choices = weeks.week_choices(first, last)
        self.fields["start_week"].choices = choices
        self.fields["end_week"].choices = choices

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_week") and cleaned.get("end_week"):
            start = weeks.monday(*weeks.parse(cleaned["start_week"]))
            end = weeks.sunday(*weeks.parse(cleaned["end_week"]))
            if end < start:
                self.add_error("end_week", "The last week lies before the first week.")
            else:
                self.instance.start, self.instance.end = start, end
        return cleaned


class BookingForm(WeeksForm):
    class Meta:
        model = Booking
        fields = ["project_no", "description", "engineers", "unit", "status", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "project_no": "Free text: the project need not exist in TestLab yet; the plan links to it once it does.",
            "engineers": "As shown on the plan, e.g. “Martijn, Naresh”.",
            "status": "Tentative and Confirmed bookings both hold the unit; Cancelled ones free it.",
        }

    field_order = ["project_no", "description", "engineers", "unit", "start_week", "end_week", "status", "note"]


class UnitBlockForm(WeeksForm):
    class Meta:
        model = UnitBlock
        fields = ["unit", "kind", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    field_order = ["unit", "kind", "start_week", "end_week", "note"]
