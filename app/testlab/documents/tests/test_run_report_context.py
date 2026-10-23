"""Test Run Report context (M2C): what the report shows and where it comes from. No Chromium needed."""

import json

import pytest
from django.urls import reverse

from testlab.documents import pdf, run_report_context
from testlab.documents.run_report_context import build_run_report_context, report_assets, run_report_file_name
from testlab.results import services
from testlab.results.tests.helpers import log, make_run

from .html_text import html_lines
from .report_data import dcs_running_run, dual_run, single_plug_run

pytestmark = pytest.mark.django_db


@pytest.fixture
def run(user):
    return dual_run(user)


@pytest.fixture
def context(run):
    return build_run_report_context(run)


def rows_by_parameter(context, heading):
    group = next(g for g in context["conditions"]["groups"] if g["heading"] == heading)
    return {row["parameter"]: row for row in group["rows"]}


def test_context_is_plain_data_ready_for_a_snapshot(context):
    assert json.loads(json.dumps(context)) == context


def test_identification_from_project_tcn_and_logged_period(context):
    ident = context["identification"]
    assert (ident["project_no"], ident["customer"], ident["run_no"], ident["tcn_no"]) == ("2026RSD03TL", "ADNOC", "001", "TCN001")
    assert ident["tcn_status"] == "Draft" and ident["test_unit"] == "B" and ident["arrangement"] == "Dual Pressurized"
    assert [s["seal_type_size"] for s in ident["seals"]] == ["UHTW 4.750", "UHTW 4.750"]
    # t = 0 is the DCS recording start (the test export), the end the last evidence
    assert ident["start_source"] == "start of the LabVIEW recording" and ident["logged_hours"] == "36.00"
    assert ident["running_hours"] == "36.00" and ident["unknown_hours"] == ""  # speed at every reading
    assert context["is_draft"] is True


def test_title_objective_and_prepared_by(context, user):
    report = context["report"]
    assert report["title"] == "Wavy-face qualification on water" and report["title_source"] == "Test Program TP-2026-03 rev B"
    assert report["objective"] == [["Demonstrate the leakage and wear performance of the wavy seal faces."]]
    assert report["objective_addition"] == [["Run 001: first 24 h on demineralised water."]]
    assert report["prepared_by"] == "Maria Geerts" and report["finalized"] is None
    assert context["sections"]["objective"] == 1


def test_description_comes_from_the_tcn(context):
    assert context["description"]["project"] == "ADNOC BAB"
    assert [line["text"] for line in context["description"]["run_lines"]] == [
        "Run on water at site conditions.", "Leakage collected per seal."
    ]


def test_configuration_seals_from_tcn_and_components_as_fitted(context):
    configuration = context["configuration"]
    assert configuration["reference_drawing"] == "UHTW 4.750 cartridge"
    assert [s["heading"] for s in configuration["seals"]] == ["Inboard Seal", "Outboard Seal"]
    ib_stator = next(c for c in configuration["components"] if c["label"] == "IB Stator")
    assert (ib_stator["part_no"], ib_stator["material"], ib_stator["condition"]) == ("C100603033", "C100603033 (SIC30)", "New")
    ob_rotor = next(c for c in configuration["components"] if c["label"] == "OB Rotor")
    assert ob_rotor["part_no"] == "—"  # missing value of an existing row


def test_conditions_put_instructed_beside_actual(context):
    common = rows_by_parameter(context, "Test run")
    # shaft-running readings only: the 0 rpm reading at the start is in the logs, not in the range
    assert common["Speed"]["instructed"] == "2980" and common["Speed"]["actual"] == "2981 – 2985"
    assert common["Speed"]["count"] == 6 and common["Speed"]["source"] == "Manual log"
    assert common["Test unit"]["instructed"] == "B" and common["Test unit"]["actual"] == "—"
    duration = common["Duration, shaft running"]
    assert duration["instructed"] == "72" and duration["actual"] == "36.00" and duration["source"] == "Manual log"
    assert common["Elapsed clock time"]["actual"] == "36.00"
    assert common["Elapsed clock time"]["source"] == "DCS recording + manual log"
    assert context["conditions"]["static"] is False and context["conditions"]["provisional"] is False
    assert "Torque" not in common  # neither instructed nor logged
    barrier = rows_by_parameter(context, "Barrier")
    assert barrier["Medium"]["instructed"] == "Demi water" and barrier["Medium"]["source"] == ""
    assert barrier["Pressure"]["instructed"] == "70" and barrier["Pressure"]["actual"] == "69.5 – 71.5"  # 70.05: stopped
    assert barrier["Inlet temp."]["instructed"] == "60" and barrier["Inlet temp."]["actual"] == "—"  # never logged
    inboard = rows_by_parameter(context, "Inboard Seal")
    assert inboard["Leakage, average dynamic"]["actual"] == "6.0"
    assert inboard["Leakage, average dynamic"]["source"] == "Manual log"


def test_conditions_table_has_no_requirement_or_assessment(context):
    """TCN values are instructions: acceptance is in the Leakage, Face wear and Acceptance sections."""
    for group in context["conditions"]["groups"]:
        for row in group["rows"]:
            assert set(row) == {"parameter", "unit", "instructed", "actual", "count", "source", "provisional"}
    inboard = next(seal for seal in context["leakage"] if seal["seal"] == "Inboard Seal")
    assert inboard["criteria"][0]["requirement"] == "≤ 15 ml/h"  # still assessed in the Leakage Summary


def test_leakage_is_dynamic_volume_over_its_running_hours_never_a_mean_of_rates(context):
    inboard, outboard = context["leakage"]
    # 10 + 20 + 30 + 40 + 50 ml over 1 + 3 + 5 + 7 + 9 running h = 6.0 ml/h; the mean of the interval rates would be 6.8
    assert (inboard["total"], inboard["dynamic_volume"], inboard["dynamic_hours"], inboard["average"]) == (
        "155", "150", "25.00", "6.0")
    first, last = inboard["excluded"]  # the first reading's 5 ml (no period) and the blank at 36 h
    assert first["volume"] == "5" and "collection period unknown" in first["reason"]
    assert last["volume"] == "—" and "not measured" in last["reason"]
    assert inboard["uncovered"] == "11.00"  # 36 running h, 25 covered by dynamic collections
    assert inboard["criteria"][0]["assessment"] == "pass"
    assert (outboard["total"], outboard["average"]) == ("60", "2.4")
    assert outboard["criteria"][0]["assessment"] == "pass"  # Program P1 applies to each seal
    assert context["leakage_chart"].startswith("<svg") and "Cumulative collected volume" in context["leakage_chart"]
    assert "<polyline" not in context["leakage_chart"] and "<line" in context["leakage_chart"]  # markers, no curve


def test_saved_plots_in_their_order_with_their_source(context, run):
    assert [p["caption"] for p in context["plots"]] == ["Barrier inlet temperature", "Motor speed"]
    first = context["plots"][0]
    assert first["number"] == 1 and first["dataset"] == run.dcs_datasets.get().original_name
    assert first["window"] == "whole recording" and first["signals"] == "(T04) LBI [T [C]] (left)"
    assert {a["path"] for a in context["assets"]} >= {p["src"] for p in context["plots"]}


def test_measurement_summary_from_the_reported_measurements_only_applicable_rows(context):
    rows = {m["component"]: {r["quantity"]: r for r in m["rows"]} for m in context["measurements"]}
    # rotors: not applicable, no rows; OB Sleeve: optional, not measured, no rows
    assert set(rows) == {"IB Stator", "OB Stator", "IB Backing ring"}
    ib = rows["IB Stator"]
    assert list(ib) == ["Face flatness (circular trace)", "Face taper (Mahr)", "Face taper (ROUNDPAK)", "Face length",
                        "Roughness Ra"]  # fmt: skip
    flatness = ib["Face flatness (circular trace)"]
    assert (flatness["pre"]["value"], flatness["post"]["value"], flatness["source"]) == ("0.296", "0.296", "ROUNDPAK")
    assert flatness["delta"] == "0.000" and flatness["pre"]["extra"] == "1.021 HLB"  # instrument FLTt, not LFD P–V
    taper = ib["Face taper (Mahr)"]
    assert taper["pre"]["value"] == "−0.0186" and taper["pre"]["extra"] == "−0.064 HLB · Flat"
    assert taper["source"] == "Mahr" and "provisional" not in taper["post"]["extra"]
    radial = ib["Face taper (ROUNDPAK)"]  # both tapers shown: same sign convention and flat limit
    assert radial["source"] == "ROUNDPAK" and radial["post"]["value"].startswith("+")
    assert radial["post"]["extra"].endswith("HLB · Concave")
    assert ib["Roughness Ra"]["pre"]["value"] == "0.034"  # mean of 0.035 and 0.033
    length = ib["Face length"]
    assert (length["delta"], length["wear"], length["unit"], length["source"]) == ("−0.00012", "0.00012", "in", "Manual")
    ob = rows["OB Stator"]
    assert list(ob) == ["Face taper (Mahr)", "Roughness Ra"]  # length and ROUNDPAK not applicable
    assert ob["Face taper (Mahr)"]["post"] == {"value": "—", "extra": "missing", "status": "missing"}
    assert ob["Face taper (Mahr)"]["delta"] == "—"


def test_measurement_details_pre_to_post_with_the_selected_figures(context):
    titles = [block["title"] for block in context["measurement_details"]]
    assert titles == ["IB Stator — Mahr profile / roughness", "IB Stator — ROUNDPAK flatness",
                      "OB Stator — Mahr profile / roughness"]  # fmt: skip
    mahr_block, roundpak_block, ob_block = context["measurement_details"]
    profiles, gauges = mahr_block["figures"]  # default figures of a new Mahr measurement
    assert profiles["code"] == "mahr_profiles" and len(profiles["grid"]) == 2  # left, right
    assert set(profiles["grid"][0]) == {"pre", "post"} and profiles["grid"][0]["pre"].startswith("<svg")
    assert gauges["code"] == "taper_gauge" and "Flat" in gauges["grid"][0]["post"]
    mahr_rows = {r["label"]: r for r in mahr_block["rows"]}
    assert mahr_rows["Taper classification"]["pre"] == "Flat" and mahr_rows["Flat limit"]["post"] == "± 0.25 HLB"
    assert mahr_rows["First points skipped"]["pre"] == "5" and "Scan geometry confirmed" not in mahr_rows
    rows = {r["label"]: r for r in roundpak_block["rows"]}
    assert rows["Instrument flatness FLTt"]["pre"] == "0.296"
    assert rows["Circular LFD P–V (calculated)"]["pre"] == "0.2955"  # deleted points excluded
    assert rows["Circular LFD P–V (calculated)"]["post"] == "0.2958"
    assert rows["Instrument check result"]["post"] == "Pass"
    assert rows["Taper classification"]["post"] == "Concave" and rows["Radial taper, signed (calculated)"]["post"][0] == "+"
    assert rows["Strongest waviness components (UPR)"]["post"].startswith("3 UPR 0.083 µm, 2 UPR 0.073 µm")
    # The record holds every figure of the reported measurements; the profile selection picks from them.
    assert [f["code"] for f in roundpak_block["figures"]] == [
        "roundpak_circular", "roundpak_polar", "roundpak_radial", "roundpak_harmonics", "roundpak_radial_taper",
        "taper_gauge"]  # fmt: skip
    from testlab.documents.report_profiles import present
    from testlab.results.models import ReportSelection

    stored = ReportSelection.objects.get(profile="engineering").data
    engineering = present(context, "engineering", stored)
    shown = next(b for b in engineering["measurement_details"] if b["slot"] == roundpak_block["slot"])
    assert [f["code"] for f in shown["figures"]] == ["roundpak_circular", "roundpak_polar", "taper_gauge"]
    assert present(context, "customer")["measurement_details"] == []  # Customer: summary values only
    assert roundpak_block["sources"]["post"]["files"][0]["name"] == "ROUNDPAK-SIC30-post.csv"
    assert set(ob_block["figures"][0]["grid"][0]) == {"pre"}  # Post not measured


def test_optional_components_with_their_evidence_and_no_empty_rows(context):
    rows = {m["component"]: m for m in context["measurements"]}
    ring = rows["IB Backing ring"]
    assert ring["optional"] is True
    (thickness,) = ring["rows"]
    assert (thickness["quantity"], thickness["pre"]["value"], thickness["post"]["value"], thickness["unit"]) == (
        "Dimension: Thickness", "3.502", "3.498", "mm")
    assert thickness["delta"] == "−0.004" and thickness["wear_quantity"] is False  # no wear meaning
    labels = [c["label"] for c in context["configuration"]["components"]]
    assert "IB Backing ring" in labels and "OB Sleeve" in labels  # fitted parts are listed
    assert [c["label"] for c in context["component_observations"]] == ["IB Backing ring"]
    single = context["pictures"]["single_stage"][0]["rows"]
    assert "IB Backing ring" in [item["label"] for row in single for item in row]


def test_wear_only_for_face_length(context):
    assert [(w["component"], w["quantity"], w["wear"]) for w in context["wear"]] == [("IB Stator", "Face length", "0.00012")]
    wear = context["wear"][0]
    assert wear["delta"] == "−0.00012" and "Pre − Post" in wear["direction"]
    assert wear["hours"] == "36.00" and wear["rate"] == "3.33E-6"  # over the shaft-running hours
    assert wear["criteria"][0]["assessment"] == "fail"


def test_cross_section_and_paired_pictures(context):
    pictures = context["pictures"]
    assert [p["caption"] for p in pictures["cross_sections"]] == ["Cross section of the new parts"]
    (pair_group,) = pictures["comparisons"]
    assert pair_group["label"] == "IB Rotor"
    (pair,) = pair_group["pairs"]
    assert (pair["pre"]["caption"], pair["post"]["caption"]) == ("IB rotor before", "IB rotor after")
    (single,) = pictures["single_stage"]  # post-test only, no empty placeholder; components in display order
    assert single["stage"] == "Post-test"
    assert [item["label"] for item in single["rows"][0]] == ["IB Backing ring", "OB Stator"]
    assert single["rows"][0][1]["picture"]["orientation"] == "portrait"


def test_assets_are_the_stored_pictures_and_plots(context, run):
    files = report_assets(context)
    assert set(files) == {a["path"] for a in context["assets"]} and len(files) == 7
    result = run.result
    plot = result.plots.order_by("order").first()
    with plot.image.open("rb") as stored:
        assert files[f"assets/plot-{plot.pk}.png"] == stored.read()
    picture_asset = next(a for a in context["assets"] if a["kind"] == "picture")
    assert picture_asset["sha256"] == result.images.get(pk=picture_asset["id"]).sha256  # traceable to the original


def test_events_are_entries_with_an_event_or_note(context):
    assert [(e["ref"], e["category"], e["note"]) for e in context["events"]] == [
        ("E1", "Start / restart", "Start recording"), ("E2", "Adjustment", "Cooling-water flow reduced"),
    ]
    assert context["events"][1]["elapsed"] == "36.00"


def test_acceptance_records_with_sources_engineer_and_calculated_assessments(context):
    rows = context["acceptance"]
    assert [(a["criterion"], a["basis"].split(" · ")[-1]) for a in rows] == [
        ("Leakage", "Inboard Seal"), ("Leakage", "Outboard Seal"), ("Test duration", "Shaft-running hours"),
        ("IB stator wear", "IB Stator"), ("Face condition", ""),
    ]  # fmt: skip
    ib, ob, duration = rows[0], rows[1], rows[2]
    assert (ib["actual"], ib["label"], ib["by"]) == ("6.0 ml/h", "Pass", "calculated")
    assert ib["requirement_source"] == "Test Program TP-2026-03 rev B P1 (TCN not issued yet)"  # a draft TCN
    assert (duration["actual"], duration["label"], duration["by"]) == ("36.00 h", "Fail", "calculated")
    assert rows[3]["requirement_source"] == "entered on the Test Result"
    rows = {a["criterion"]: a for a in rows}
    assert (rows["Face condition"]["actual"], rows["Face condition"]["by"], rows["Face condition"]["remark"]) == (
        "No damage seen", "engineer", "Visual inspection.")


def test_narrative_as_written_and_empty_parts_omitted(context):
    narrative = context["narrative"]
    assert narrative["observations"] == [["Stable for the first 24 h."], ["Leakage steady."]]
    assert narrative["conclusion"] == [["The wavy faces performed acceptably."]] and narrative["recommendation"] == []
    assert context["sections"]["conclusion"] == 12  # after the seal face measurement details


def test_manual_log_table_holds_only_logged_measurements(context):
    (table,) = context["log"]["tables"]
    labels = [(c["label"], c["unit"]) for c in table["columns"]]
    assert labels == [("Speed", "rpm"), ("Pressure", "barg"), ("Face temp.", "°C"), ("Period", "h"),
                      ("Inboard", "ml"), ("Inboard rate", "ml/h"), ("Outboard", "ml"), ("Outboard rate", "ml/h")]  # fmt: skip
    assert len(table["rows"]) == 7 and table["rows"][0]["event"] == "E1"
    assert table["rows"][-1]["cells"] == ["2985", "", "", "11.00", "", "", "", ""]  # blank: not measured, no 0
    second = table["rows"][1]
    assert second["cells"] == ["2981", "69.5", "61", "1.00", "10", "10.0", "4", "4.0"]
    assert [row["elapsed"] for row in table["rows"]][:3] == ["0.00", "1.00", "4.00"]  # chronological


def test_wide_manual_log_is_split_into_tables_repeating_date_and_time(user):
    context = build_run_report_context(single_plug_run(user))
    tables = context["log"]["tables"]
    assert len(tables) == 2
    assert all(len(t["columns"]) <= run_report_context.LOG_COLUMNS_PER_TABLE for t in tables)
    assert sum(len(t["columns"]) for t in tables) == 12 + 1 + 4
    assert all(len(t["rows"]) == 4 and t["rows"][0]["date"] for t in tables)
    assert tables[1]["groups"][0]["heading"] != tables[0]["groups"][-1]["heading"]  # split between groups


def test_single_with_plug_seal(user):
    context = build_run_report_context(single_plug_run(user))
    headings = [g["heading"] for g in context["conditions"]["groups"]]
    assert headings == ["Test run", "Process fluid", "Flush", "Plug Seal", "Test Seal"]
    assert rows_by_parameter(context, "Process fluid")["Medium"]["instructed"] == "Crude oil"
    assert [s["seal"] for s in context["leakage"]] == ["Plug Seal", "Test Seal"]
    assert [c["label"] for c in context["configuration"]["components"]] == ["Test Stator", "Test Rotor", "Plug Rotor"]
    assert context["wear"][0]["component"] == "Test Rotor"
    assert context["pictures"]["single_stage"][0]["rows"][0][0]["label"] == "Plug Rotor"


def test_run_without_result_reports_tcn_and_log_only(user):
    run = make_run(user, "dual_pressurized")
    log(run, user, 0, speed=2980, inboard_leakage=0)
    context = build_run_report_context(run)
    assert context["has_result"] is False and context["log"]["tables"]
    assert context["plots"] == [] and context["acceptance"] == [] and context["assets"] == []
    assert set(context["sections"]) == {"description", "conditions", "leakage"}  # no seal data, no components


def test_empty_result_omits_empty_sections(user):
    run = make_run(user, "dual_pressurized")
    services.create_result(run, user)
    context = build_run_report_context(run)
    assert context["log"]["tables"] == [] and context["leakage"] == [] and context["events"] == []
    for absent in ["leakage", "plots", "events", "measurement_details", "pictures", "acceptance", "conclusion"]:
        assert absent not in context["sections"]
    assert context["sections"]["configuration"] == 2  # the components of the new Result
    # Required by default but not measured yet: listed as missing (not applicable would have no row).
    cells = [row[stage] for group in context["measurements"] for row in group["rows"] for stage in ("pre", "post")]
    assert len(cells) == 4 * 5 * 2 and {c["extra"] for c in cells} == {"missing"}


def test_not_applicable_measurements_leave_no_rows(user):
    run = make_run(user, "dual_pressurized")
    result = services.create_result(run, user)
    result.dimensional_applicability = result.mahr_applicability = result.roundpak_applicability = "not_applicable"
    result.save()
    context = build_run_report_context(run)
    assert context["measurements"] == [] and "measurements" not in context["sections"]


def test_report_belongs_to_its_own_run(user, run):
    other = make_run(user, "dual_pressurized", project_no="2026RSD04TL")
    context = build_run_report_context(other)
    assert context["identification"]["project_no"] == "2026RSD04TL"
    assert context["plots"] == [] and context["log"]["tables"] == [] and context["assets"] == []


def test_file_name(run):
    assert run_report_file_name(run) == "2026RSD03TL-Run001-TestRunReport-DRAFT.pdf"


# --- preview view ----------------------------------------------------------------------------


def test_preview_needs_login(client, run):
    response = client.get(reverse("results:report_preview", args=[run.pk]))
    assert response.status_code == 302 and "/login/" in response["Location"]


def test_preview_of_unknown_run_is_404(client, user):
    client.force_login(user)
    assert client.get(reverse("results:report_preview", args=[999])).status_code == 404


def test_preview_renders_the_runs_report(client, user, run, monkeypatch):
    calls = []

    class FakeTemplate:
        def render_pdf(self, context, assets):
            calls.append((context, assets))
            return b"%PDF-1.7 fake"

    monkeypatch.setattr("testlab.results.publication.run_report_template", lambda: FakeTemplate())
    client.force_login(user)
    response = client.get(reverse("results:report_preview", args=[run.pk]))
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == 'inline; filename="2026RSD03TL-Run001-TestRunReport-Engineering-DRAFT.pdf"'
    assert response["Cache-Control"] == "no-store"
    (context, assets), = calls
    assert context["identification"]["run_no"] == run.display_no and context["is_draft"] and len(assets) == 7


def test_preview_failure_returns_to_the_result_with_the_reason(client, user, run, monkeypatch):
    class Broken:
        def render_pdf(self, context, assets):
            raise pdf.PdfConversionError("Chromium for Playwright is not installed.")

    monkeypatch.setattr("testlab.results.publication.run_report_template", lambda: Broken())
    client.force_login(user)
    response = client.get(reverse("results:report_preview", args=[run.pk]), follow=True)
    assert "not installed" in response.content.decode()
    assert response.redirect_chain[-1][0] == reverse("results:result", args=[run.pk])


def test_result_page_offers_preview_report(client, user, run):
    client.force_login(user)
    page = client.get(reverse("results:result", args=[run.pk])).content.decode()
    assert "Preview PDF" in page and reverse("results:report_profile_preview", args=[run.pk, "customer"]) in page
    assert reverse("results:report_contents", args=[run.pk, "engineering"]) in page


FOOTER = (
    "Actual: lowest – highest reading with the shaft running (n readings or samples), start-up and stop transients "
    "included; not a steady-state range. Readings with the shaft stopped are in the complete logs and plots. Source "
    "per row: a reviewed DCS signal of the parameter, else the manual log. TCN values are instructions, not "
    "acceptance limits."
)


def conditions_lines(context):
    from testlab.documents.render import run_report_template

    from testlab.documents.report_profiles import present

    lines = html_lines(run_report_template().render_html(present(context, "engineering")))
    start = lines.index(next(line for line in lines if line.endswith("Test Conditions & Results")))
    end = lines.index(next(line for line in lines[start:] if line.endswith("Leakage Summary")))
    return lines[start:end]


def test_dcs_actuals_use_the_running_intervals_with_a_compact_source(user, file_storage):
    """Recording 01 Oct 09:00 → 04 Oct 16:00; shaft running 09:15 → 02 Oct 10:00 and 11:00 → 04 Oct 15:00."""
    context = build_run_report_context(dcs_running_run(user))
    lines = conditions_lines(context)
    assert lines[1] == "Parameter Unit TCN (Instructed) Actual Source"
    assert "Duration, shaft running h 72 76.74† DCS · Motor Speed" in lines
    assert "Elapsed clock time h — 79.00 DCS recording" in lines
    # start-up samples ≥ 50 rpm included (1500), the pre-start, post-stop and standstill samples not
    assert "Speed rpm 3000 1500 – 2997 (n=27626)† DCS · Motor Speed" in lines
    assert "Pressure barg 30 29.8 – 31.0 (n=27626)† DCS · X12 PT Barrier" in lines  # 0.412 stopped: out
    assert "Inlet temp. °C 55 21.0 – 56.4 (n=27626)† DCS · T04 LBI" in lines
    assert "Torque Nm — 47.7 – 50.2 (n=27626)† DCS · Motor Torque" in lines
    assert "Flow l/min 2 14.9 – 15.2 (n=5) Manual log" in lines
    assert FOOTER in lines and "Requirement" not in " ".join(lines) and "Assessment" not in " ".join(lines)
    ident = context["identification"]
    assert ident["recordings"][0]["start"] == "01-Oct-2026 09:00" and ident["recordings"][0]["hours"] == "79.00"
    assert (ident["running_hours"], ident["running_count"], ident["running_provisional"]) == ("76.74", 2, True)
    assert [(s["start"], s["end"]) for s in ident["running_spans"]] == [
        ("01-Oct-2026 09:15", "02-Oct-2026 10:00"), ("02-Oct-2026 11:00", "04-Oct-2026 15:00")]


def test_confirmed_intervals_are_not_marked_provisional(user, file_storage):
    context = build_run_report_context(dcs_running_run(user, confirm=True))
    lines = conditions_lines(context)
    assert "Speed rpm 3000 1500 – 2997 (n=27626) DCS · Motor Speed" in lines
    assert not any("†" in line or "rovisional" in line for line in lines)
    (determination,) = context["running"]["determinations"]
    assert determination["status"] == "confirmed" and determination["threshold_rpm"] == 50


def test_a_static_test_keeps_its_static_footer(user):
    run = make_run(user, "dual_pressurized")
    log(run, user, 0, speed=0, seal_support_pressure=70)
    log(run, user, 5, speed=0, seal_support_pressure=71)
    context = build_run_report_context(run)
    assert context["conditions"]["static"] is True
    lines = conditions_lines(context)
    assert lines[-1] == ("Static test: actual = lowest – highest reading with the shaft stopped (static summary; "
                         "n readings). Source per row: a reviewed DCS signal of the parameter, else the manual log. "
                         "TCN values are instructions, not acceptance limits.")  # fmt: skip
    assert FOOTER not in lines


def test_report_numbers_are_rounded_for_display_only():
    from testlab.results.formatting import report_number, report_range

    assert report_number(47.682, "Nm") == "47.7" and report_number(2987.4, "rpm") == "2987"
    assert report_range(29.832, 30.991, "barg") == "29.8 – 31.0" and report_range(21.033, 56.41, "°C") == "21.0 – 56.4"
    assert report_number(15.04, "l/min") == "15.0" and report_number(6.04, "ml/h") == "6.0"
    assert report_number(76.7472, "h") == "76.75" and report_number(-0.04, "barg") == "0.0"
    assert report_range(30.01, 29.98 + 0.06, "barg") == "30.0"  # both show the same: one value
    assert report_number(0.1234, "mm") == "0.1234"  # no rule for the unit: as recorded
