import hashlib
import io

import pytest
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from testlab.projects import drawings
from testlab.projects.models import ProjectDrawing

from .test_models import make_project

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


def picture(image_format, size=(60, 40), mode="RGB"):
    out = io.BytesIO()
    Image.new(mode, size, "white").save(out, image_format)
    return out.getvalue()


def upload(name, content):
    return SimpleUploadedFile(name, content)


@pytest.fixture
def project(user):
    return make_project(user)


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


# ---- storage and checks ----------------------------------------------------------------------


@pytest.mark.parametrize("name, content, file_format", [
    ("tester.pdf", PDF, "PDF"),
    ("tester.tif", picture("TIFF", mode="1"), "TIFF"),
    ("tester.jpg", picture("JPEG"), "JPEG"),
    ("tester.png", picture("PNG", mode="RGBA"), "PNG"),
])  # fmt: skip
def test_tester_drawing_formats_stored_unchanged(project, user, file_storage, name, content, file_format):
    drawing = drawings.add_drawing(project, "tester", upload(name, content), user, " F001842744 ", " 2 ")
    assert (drawing.drawing_no, drawing.revision, drawing.file_format) == ("F001842744", "2", file_format)
    assert drawing.file.name.startswith("projects/2099RSD01TL/drawings/")
    assert (file_storage / drawing.file.name).read_bytes() == content
    assert drawing.sha256 == hashlib.sha256(content).hexdigest() and drawing.size == len(content)
    if file_format == "PDF":
        assert drawing.preview_file == ""
    else:
        with Image.open(file_storage / drawing.preview_file) as preview:
            assert preview.format == "JPEG"


def test_format_is_read_from_the_content_not_the_name(project, user):
    drawing = drawings.add_drawing(project, "tester", upload("scan.pdf", picture("PNG")), user, "F1")
    assert drawing.file_format == "PNG"
    with pytest.raises(drawings.DrawingUploadError, match="not a PDF, TIFF, JPEG or PNG file"):
        drawings.add_drawing(project, "tester", upload("tester.png", b"not a drawing"), user, "F1")


def test_pid_is_pdf_only(project, user):
    drawing = drawings.add_drawing(project, "pid", upload("pid.pdf", PDF), user, "P-100")
    assert drawing.kind == "pid" and drawing.file_format == "PDF"
    with pytest.raises(drawings.DrawingUploadError, match="A system P&ID must be a PDF"):
        drawings.add_drawing(project, "pid", upload("pid.png", picture("PNG")), user, "P-100")


def test_other_picture_formats_refused(project, user):
    with pytest.raises(drawings.DrawingUploadError, match="not a PDF, TIFF, JPEG or PNG"):
        drawings.add_drawing(project, "tester", upload("tester.bmp", picture("BMP")), user, "F1")
    assert not ProjectDrawing.objects.exists()


def test_size_limit(project, user, settings):
    settings.TESTLAB_DRAWING_MAX_MB = 0
    with pytest.raises(drawings.DrawingUploadError, match="larger than 0 MB"):
        drawings.add_drawing(project, "tester", upload("tester.pdf", PDF), user, "F1")


def test_new_upload_becomes_current_and_earlier_kept(project, user, file_storage):
    first = drawings.add_drawing(project, "tester", upload("a.pdf", PDF), user, "F1", "1")
    second = drawings.add_drawing(project, "tester", upload("b.pdf", PDF + b"%rev2"), user, "F1", "2")
    pid = drawings.add_drawing(project, "pid", upload("p.pdf", PDF), user, "P1")
    tester, system = drawings.drawing_sets(project)
    assert tester["current"] == second and tester["earlier"] == [first]
    assert system["current"] == pid and system["earlier"] == []
    assert (file_storage / first.file.name).read_bytes() == PDF


def test_only_the_current_drawing_can_be_deleted(project, user, file_storage, django_capture_on_commit_callbacks):
    first = drawings.add_drawing(project, "tester", upload("a.png", picture("PNG")), user, "F1", "1")
    second = drawings.add_drawing(project, "tester", upload("b.png", picture("PNG")), user, "F1", "2")
    with pytest.raises(drawings.DrawingUploadError, match="Only the current drawing"):
        drawings.delete_drawing(first)
    names = [second.file.name, second.preview_file]
    with django_capture_on_commit_callbacks(execute=True):
        drawings.delete_drawing(second)
    assert not any((file_storage / name).exists() for name in names)
    assert drawings.current(project, "tester") == first


def test_drawing_in_an_issued_final_report_cannot_be_deleted(project, user):
    from django.utils import timezone

    from testlab.reports.models import FinalReport, FinalReportDocument

    drawing = drawings.add_drawing(project, "pid", upload("p.pdf", PDF), user, "P-100")
    report = FinalReport.objects.create(project=project, updated_by=user)
    FinalReportDocument.objects.create(report=report, revision=0, identity=f"{project.project_no}R-R0",
                                       file="r0.pdf", sha256="0" * 64, size=1, issued_at=timezone.now(),
                                       issued_by=user)  # fmt: skip
    with pytest.raises(drawings.DrawingUploadError, match=f"issued Final Report {project.project_no}R-R0"):
        drawings.delete_drawing(drawing)
    newer = drawings.add_drawing(project, "pid", upload("p2.pdf", PDF), user, "P-100", "B")
    assert drawings.delete_refusal(newer) is None  # issued before it was uploaded


def test_missing_preview_rebuilt(project, user, file_storage):
    drawing = drawings.add_drawing(project, "tester", upload("a.tif", picture("TIFF")), user, "F1")
    (file_storage / drawing.preview_file).unlink()
    name = drawings.preview_name(drawing)
    assert (file_storage / name).exists()
    drawing.refresh_from_db()
    assert drawing.preview_file == name


# ---- screens ---------------------------------------------------------------------------------


def test_upload_from_project_defaults(logged_in, project):
    url = reverse("projects:drawing_add", args=[project.pk, "tester"])
    response = logged_in.post(url, {"tester-drawing_no": "F001842744", "tester-revision": "2",
                                    "tester-file": upload("tester.pdf", PDF)})  # fmt: skip
    assert response.status_code == 302
    assert response["Location"] == reverse("projects:defaults", args=[project.pk]) + "#drawings"
    assert message_texts(response) == ["Tester drawing F001842744 rev 2 added."]
    page = logged_in.get(reverse("projects:defaults", args=[project.pk])).content.decode()
    assert "F001842744" in page and "Replace with a new drawing" in page
    assert "F001842744 rev 2" in logged_in.get(reverse("projects:detail", args=[project.pk])).content.decode()


def test_refused_upload_shows_the_reason(logged_in, project):
    url = reverse("projects:drawing_add", args=[project.pk, "pid"])
    response = logged_in.post(url, {"pid-drawing_no": "P-100", "pid-file": upload("pid.jpg", picture("JPEG"))})
    assert response.status_code == 200
    assert "A system P&amp;ID must be a PDF." in response.content.decode()
    assert not ProjectDrawing.objects.exists()


def test_unknown_kind_ignored(logged_in, project):
    url = reverse("projects:drawing_add", args=[project.pk, "seal"])
    response = logged_in.post(url, {"seal-drawing_no": "X", "seal-file": upload("x.pdf", PDF)})
    assert response.status_code == 302 and not ProjectDrawing.objects.exists()


def test_open_download_and_preview(logged_in, project, user):
    pdf = drawings.add_drawing(project, "pid", upload("pid.pdf", PDF), user, "P-100")
    tif = drawings.add_drawing(project, "tester", upload("tester.tif", picture("TIFF")), user, "F1")
    response = logged_in.get(reverse("projects:drawing_file", args=[pdf.pk]))
    assert response["Content-Type"] == "application/pdf" and b"".join(response.streaming_content) == PDF
    assert "inline" in response["Content-Disposition"]
    response = logged_in.get(reverse("projects:drawing_file", args=[tif.pk]) + "?download=1")
    disposition = response["Content-Disposition"]
    assert "attachment" in disposition and 'filename="tester.tif"' in disposition
    response = logged_in.get(reverse("projects:drawing_preview", args=[tif.pk]))
    assert response["Content-Type"] == "image/jpeg"


def test_delete_view(logged_in, project, user):
    first = drawings.add_drawing(project, "tester", upload("a.pdf", PDF), user, "F1", "1")
    drawings.add_drawing(project, "tester", upload("b.pdf", PDF), user, "F1", "2")
    response = logged_in.post(reverse("projects:drawing_delete", args=[first.pk]), follow=True)
    assert message_texts(response) == [drawings.delete_refusal(first)]
    current = drawings.current(project, "tester")
    response = logged_in.post(reverse("projects:drawing_delete", args=[current.pk]))
    assert message_texts(response) == ["Tester drawing F1 rev 2 deleted."]
    assert list(ProjectDrawing.objects.all()) == [first]


def test_login_required(client, project, user):
    drawing = drawings.add_drawing(project, "pid", upload("pid.pdf", PDF), user, "P-100")
    response = client.get(reverse("projects:drawing_file", args=[drawing.pk]))
    assert response.status_code == 302 and "login" in response["Location"]
