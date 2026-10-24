"""Lab plan, and the forms for bookings and unit blocks."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from . import plan, weeks
from .forms import BookingForm, UnitBlockForm
from .models import Booking, UnitBlock
from .permissions import can_edit_planning


class PlanView(View):
    template_name = "planning/plan.html"

    def get(self, request):
        this_year, this_week = weeks.week_of(timezone.localdate())
        try:
            year = int(request.GET.get("year", this_year))
        except ValueError:
            year = this_year
        year = max(year, weeks.FIRST_YEAR)
        show_cancelled = request.GET.get("cancelled") == "1"
        n_weeks = weeks.weeks_in_year(year)
        context = {
            "year": year,
            "years": range(weeks.FIRST_YEAR, max(this_year, weeks.FIRST_YEAR, year) + 3),
            "week_numbers": range(1, n_weeks + 1),
            "n_weeks": n_weeks,
            "months": plan.months(year),
            "current_week": this_week if year == this_year else None,
            "units": plan.build(year, show_cancelled=show_cancelled),
            "show_cancelled": show_cancelled,
            "can_edit": can_edit_planning(request.user),
        }
        return render(request, self.template_name, context)


class PeriodEditView(View):
    """Create (no pk) or edit a booking or unit block."""

    model = None
    form_class = None
    template_name = "planning/period_form.html"

    def dispatch(self, request, *args, **kwargs):
        if not can_edit_planning(request.user):
            raise PermissionDenied("You may not change the lab planning.")
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, pk):
        return get_object_or_404(self.model, pk=pk) if pk else self.model()

    def get(self, request, pk=None):
        item = self.get_object(pk)
        initial = {}
        if not pk:
            # From the plan: a click on a unit's "+" brings its unit and year along.
            if unit := request.GET.get("unit"):
                initial["unit"] = unit
            year = request.GET.get("year", "")
            if year.isdigit() and int(year) >= weeks.FIRST_YEAR:
                this_week = weeks.week_of(timezone.localdate())
                start = this_week if this_week[0] == int(year) else (int(year), 1)
                initial["start_week"] = initial["end_week"] = weeks.label(*start)
        return self.render(request, item, self.form_class(instance=item, initial=initial))

    def post(self, request, pk=None):
        item = self.get_object(pk)
        form = self.form_class(request.POST, instance=item)
        if not form.is_valid():
            return self.render(request, item, form)
        item = form.save(commit=False)
        if item.pk is None:
            item.created_by = request.user
        item.updated_by = request.user
        item.save()
        messages.success(request, f"{self.describe(item)} saved.")
        return redirect(f"{reverse('planning:plan')}?year={weeks.week_of(item.start)[0]}")

    def render(self, request, item, form):
        context = {"item": item, "form": form, "kind": self.kind, "title": self.title(item),
                   "delete_url": reverse(f"planning:{self.kind}_delete", args=[item.pk]) if item.pk else None}  # fmt: skip
        return render(request, self.template_name, context)


class BookingEditView(PeriodEditView):
    model = Booking
    form_class = BookingForm
    kind = "booking"

    def title(self, item):
        return f"Booking {item.project_no}" if item.pk else "New booking"

    def describe(self, item):
        return f"Booking of {item.project_no} ({item.unit}, {item.weeks_text})"


class UnitBlockEditView(PeriodEditView):
    model = UnitBlock
    form_class = UnitBlockForm
    kind = "block"

    def title(self, item):
        return f"{item.get_kind_display()} of {item.unit}" if item.pk else "New unit block"

    def describe(self, item):
        return f"{item.get_kind_display()} of {item.unit} ({item.weeks_text})"


class PeriodDeleteView(View):
    model = None

    def post(self, request, pk):
        if not can_edit_planning(request.user):
            raise PermissionDenied("You may not change the lab planning.")
        item = get_object_or_404(self.model, pk=pk)
        year, text = weeks.week_of(item.start)[0], f"{item} ({item.weeks_text})"
        item.delete()
        messages.success(request, f"{text} deleted.")
        return redirect(f"{reverse('planning:plan')}?year={year}")


class BookingDeleteView(PeriodDeleteView):
    model = Booking


class UnitBlockDeleteView(PeriodDeleteView):
    model = UnitBlock
