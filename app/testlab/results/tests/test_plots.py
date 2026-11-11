"""DCS plots added to the Result, the Result summary and the project page link (M2B)."""

import hashlib
import io
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.urls import reverse
from PIL import Image

from testlab.execution import datasets
from testlab.execution.tests.test_labview import NAME, export
from testlab.results import plots, services
from testlab.results.models import ResultPlot

from .helpers import make_run

pytestmark = pytest.mark.django_db

# Columns of the test export: 0 Time [Hrs], 1 (T04) LBI [T [C]], 2 Process Syst Press [BarG], 3 Motor Speed [RPM]


def png(size=(240, 120), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, fmt)
    return out.getvalue()


def definition(**changes):
    data = {
        "signals": [{"column": 1, "axis": "y"}, {"column": 3, "axis": "y2"}],
        "time_range": [0.002, 0.02],
        "y_range": [39.5, 42.5],
        "y2_range": None,
        "caption": "  LBI temperature and speed  ",
    }
    data.update(changes)
    return json.dumps(data)


@pytest.fixture
def run(user):
    return make_run(user, "dual_pressurized")


@pytest.fixture
def result(run, user):
    return services.create_result(run, user)


@pytest.fixture
def dataset(run, user):
    return datasets.import_dataset(run, SimpleUploadedFile(NAME, export()), user)


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


# --- definition and storage ------------------------------------------------------------


def test_plot_keeps_its_definition_and_report_image(result, dataset, user):
    image = png()
    plot = plots.add_plot(result, dataset, definition(), image, user)
    assert plot.dataset == dataset and plot.caption == "LBI temperature and speed"
    assert plot.signals == [
        {"column": 1, "name": "(T04) LBI", "unit": "T [C]", "axis": "y"},
        {"column": 3, "name": "Motor Speed", "unit": "RPM", "axis": "y2"},
    ]
    assert (plot.time_start_h, plot.time_end_h) == (0.002, 0.02)
    assert (plot.y_min, plot.y_max, plot.y2_min, plot.y2_max) == (39.5, 42.5, None, None)
    with plot.image.open("rb") as stored:
        assert stored.read() == image
    assert plot.image_sha256 == hashlib.sha256(image).hexdigest() and (plot.image_width, plot.image_height) == (240, 120)
    assert plot.image.name.startswith("projects/2026RSD03TL/runs/001/result/plots/")
    assert plot.rendering == {"samples_in_window": 13, "points_per_signal": 13, "decimated": False}
    assert plot.definition()["time_range"] == [0.002, 0.02] and plot.definition()["y2_range"] is None


def test_names_and_units_come_from_the_dataset_not_the_browser(result, dataset, user):
    sent = definition(signals=[{"column": 2, "axis": "y", "name": "Forged", "unit": "psi"}])
    plot = plots.add_plot(result, dataset, sent, png(), user)
    assert plot.signals == [{"column": 2, "name": "(X22) Process Syst Press", "unit": "BarG", "axis": "y"}]


def test_whole_recording_and_automatic_axes_are_stored_as_empty(result, dataset, user):
    plot = plots.add_plot(result, dataset, definition(time_range=None, y_range=None), png(), user)
    assert (plot.time_start_h, plot.time_end_h, plot.y_min, plot.y_max) == (None, None, None, None)
    assert plots.describe(plot)["window"] == "whole recording" and plots.describe(plot)["ranges"] == "automatic"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"signals": []}, "at least one signal"),
        ({"signals": [{"column": 0, "axis": "y"}]}, "not a signal"),  # the time column
        ({"signals": [{"column": 9, "axis": "y"}]}, "not a signal"),
        ({"signals": [{"column": 1, "axis": "y"}, {"column": 1, "axis": "y2"}]}, "not a signal"),
        ({"signals": [{"column": 1, "axis": "y3"}]}, "left or right axis"),
        ({"time_range": [0.02, 0.002]}, "lower to a higher"),
        ({"y_range": ["a", 3]}, "pair of numbers"),
        ({"caption": "x" * 301}, "too long"),
    ],
)
def test_invalid_definitions_are_refused_and_nothing_is_stored(result, dataset, user, file_storage, changes, message):
    with pytest.raises(plots.PlotError, match=message):
        plots.add_plot(result, dataset, definition(**changes), png(), user)
    assert not ResultPlot.objects.exists()
    assert not (file_storage / "projects/2026RSD03TL/runs/001/result/plots").exists() or not any(
        (file_storage / "projects/2026RSD03TL/runs/001/result/plots").iterdir()
    )


def test_image_must_be_a_png(result, dataset, user):
    with pytest.raises(plots.PlotError, match="not a PNG"):
        plots.add_plot(result, dataset, definition(), png(fmt="JPEG"), user)
    with pytest.raises(plots.PlotError, match="missing"):
        plots.add_plot(result, dataset, definition(), b"", user)


def test_dataset_of_another_run_is_refused(result, user):
    other = make_run(user, project_no="2026RSD04TL")
    foreign = datasets.import_dataset(other, SimpleUploadedFile(NAME, export()), user)
    with pytest.raises(plots.PlotError, match="another run"):
        plots.add_plot(result, foreign, definition(), png(), user)


def test_update_replaces_definition_and_image(result, dataset, user, other_user, django_capture_on_commit_callbacks):
    plot = plots.add_plot(result, dataset, definition(), png(), user)
    old = plot.image.name
    new_image = png((300, 150))
    with pytest.raises(plots.PlotError):
        plots.update_plot(plot, definition(signals=[]), new_image, other_user)
    plot.refresh_from_db()
    assert plot.image.name == old
    with django_capture_on_commit_callbacks(execute=True):
        plots.update_plot(plot, definition(signals=[{"column": 2, "axis": "y"}], caption="Pressure"), new_image, other_user)
    plot.refresh_from_db()
    assert [s["column"] for s in plot.signals] == [2] and plot.caption == "Pressure"
    assert plot.image_width == 300 and plot.updated_by == other_user and plot.created_by == user
    assert not plot.image.storage.exists(old)


def test_plot_protects_its_dataset(result, dataset, user):
    plots.add_plot(result, dataset, definition(), png(), user)
    with pytest.raises(ProtectedError):
        dataset.delete()


def test_move_and_delete(result, dataset, user, django_capture_on_commit_callbacks):
    first = plots.add_plot(result, dataset, definition(caption="first"), png(), user)
    second = plots.add_plot(result, dataset, definition(caption="second"), png(), user)
    plots.move(second, -1)
    assert [p.caption for p in result.plots.all()] == ["second", "first"]
    name = first.image.name
    with django_capture_on_commit_callbacks(execute=True):
        plots.delete_plot(first)
    assert [p.caption for p in result.plots.all()] == ["second"]
    assert not first.image.storage.exists(name)


# --- views -----------------------------------------------------------------------------


def post_plot(client, url, data=None, image=None):
    return client.post(url, {
        "definition": data or definition(),
        "image": SimpleUploadedFile("plot.png", image if image is not None else png(), content_type="image/png"),
    })  # fmt: skip


def test_dcs_viewer_adds_plot_to_result(logged_in, run, result, dataset):
    page = logged_in.get(reverse("execution:dcs_detail", args=[dataset.pk])).content.decode()
    assert "Add plot to Result" in page and reverse("results:plot_add", args=[dataset.pk]) in page
    response = post_plot(logged_in, reverse("results:plot_add", args=[dataset.pk]))
    assert response.status_code == 200
    plot = result.plots.get()
    assert response.json()["result_url"].endswith(f"#plot-{plot.pk}")
    assert response.json()["plot"]["signals"][0]["name"] == "(T04) LBI"

    page = logged_in.get(reverse("results:result", args=[run.pk])).content.decode()
    assert 'id="plot-' in page and "LBI temperature and speed" in page
    assert "(T04) LBI [T [C]] (left), Motor Speed [RPM] (right)" in page
    assert "0.002 – 0.020 h" in page and "left 39.5 – 42.5" in page
    assert f"?plot={plot.pk}" in page
    image = logged_in.get(reverse("results:plot_image", args=[plot.pk]))
    assert image["Content-Type"] == "image/png"


def test_bad_definition_answers_with_the_reason(logged_in, result, dataset):
    response = post_plot(logged_in, reverse("results:plot_add", args=[dataset.pk]), definition(signals=[]))
    assert response.status_code == 400 and "at least one signal" in response.json()["error"]


def test_dcs_viewer_without_result_asks_to_start_it(logged_in, run, dataset):
    page = logged_in.get(reverse("execution:dcs_detail", args=[dataset.pk])).content.decode()
    assert "start the Result" in page and "dcs-plot-to-result" not in page
    response = post_plot(logged_in, reverse("results:plot_add", args=[dataset.pk]))
    assert response.status_code == 400 and "no Result yet" in response.json()["error"]


def test_production_policy_refuses_plots(logged_in, result, dataset, settings):
    settings.TESTLAB_EXECUTION_REQUIRES_ISSUED_TCN = True
    response = post_plot(logged_in, reverse("results:plot_add", args=[dataset.pk]))
    assert response.status_code == 403 and not ResultPlot.objects.exists()


def test_opening_a_plot_restores_its_definition(logged_in, result, dataset, user):
    plot = plots.add_plot(result, dataset, definition(), png(), user)
    page = logged_in.get(reverse("execution:dcs_detail", args=[dataset.pk]) + f"?plot={plot.pk}").content.decode()
    assert "Update plot in Result" in page and "Add as new plot" in page
    config = json.loads(page.split('id="dcs-plot-to-result" type="application/json">')[1].split("</script>")[0])
    assert config["plot"]["time_range"] == [0.002, 0.02] and config["plot"]["signals"][1]["axis"] == "y2"
    assert config["updateUrl"] == reverse("results:plot_update", args=[plot.pk])

    response = post_plot(logged_in, config["updateUrl"], definition(caption="Redrawn"))
    assert response.status_code == 200
    plot.refresh_from_db()
    assert plot.caption == "Redrawn" and result.plots.count() == 1


def test_caption_move_and_remove_from_result_page(logged_in, run, result, dataset, user):
    plot = plots.add_plot(result, dataset, definition(), png(), user)
    logged_in.post(reverse("results:plot_caption", args=[plot.pk]), {"caption": " Speed steps "})
    plot.refresh_from_db()
    assert plot.caption == "Speed steps"
    assert "Remove plot from the Result?" in logged_in.get(reverse("results:plot_delete", args=[plot.pk])).content.decode()
    response = logged_in.post(reverse("results:plot_delete", args=[plot.pk]))
    assert response.status_code == 302 and not ResultPlot.objects.exists()
    dataset.refresh_from_db()  # the dataset stays


def test_result_summary_layout(logged_in, run, result, dataset):
    page = logged_in.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Run summary" in page and 'class="result-blocks"' in page
    assert dataset.original_name in page  # DCS datasets in the run summary
    assert "No face wear yet" in page and 'class="card-grid"' not in page.split('id="plots"')[0]


def test_project_page_links_to_the_run_result(logged_in, run):
    page = logged_in.get(reverse("projects:detail", args=[run.project.pk])).content.decode()
    assert f'href="{reverse("results:result", args=[run.pk])}"' in page and ">Result</a>" in page


# --- deleting an imported dataset ------------------------------------------------------


def test_dataset_imported_mid_test_can_be_deleted_with_its_files(logged_in, run, user):
    from testlab.execution import running_intervals
    from testlab.execution.models import DcsDataset

    dataset = datasets.import_dataset(run, SimpleUploadedFile(NAME, export()), user)
    running_intervals.ensure_current(dataset, user)
    storage, names = dataset.raw_file.storage, [dataset.raw_file.name, dataset.columns_file]
    url = reverse("execution:dcs_delete", args=[dataset.pk])
    assert "Delete dataset" in logged_in.get(url).content.decode()
    from django.test import TestCase

    with TestCase.captureOnCommitCallbacks(execute=True):
        response = logged_in.post(url)
    assert response.url == reverse("execution:dcs", args=[run.pk])
    assert not DcsDataset.objects.exists()
    assert not any(storage.exists(name) for name in names)


def test_dataset_with_result_plots_is_not_deleted(logged_in, result, dataset, user):
    plots.add_plot(result, dataset, definition(), png(), user)
    url = reverse("execution:dcs_delete", args=[dataset.pk])
    page = logged_in.get(url).content.decode()
    assert "1 Result plot is made from" in page and "Delete dataset" not in page
    logged_in.post(url)
    assert type(dataset).objects.filter(pk=dataset.pk).exists()
    plots.delete_plot(dataset.result_plots.get())
    logged_in.post(url)
    assert not type(dataset).objects.filter(pk=dataset.pk).exists()
