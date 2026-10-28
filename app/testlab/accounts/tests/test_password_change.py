"""Users change their own password from the topbar."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db

OLD = "pw-for-tests-only"
NEW = "Wet-seal-2026!"


def change(client, old=OLD, new1=NEW, new2=NEW):
    data = {"old_password": old, "new_password1": new1, "new_password2": new2}
    return client.post(reverse("password_change"), data)


def test_topbar_links_to_password_change(client, user):
    client.force_login(user)
    content = client.get(reverse("home"), follow=True).content.decode()
    assert f'href="{reverse("password_change")}"' in content


def test_form_uses_testlab_templates(client, user):
    client.force_login(user)
    response = client.get(reverse("password_change"))
    names = [t.name for t in response.templates]
    assert "accounts/password_change.html" in names and "base.html" in names
    assert not any(name.startswith("admin/") for name in names)


def test_change_keeps_user_logged_in_and_new_password_works(client, user):
    client.force_login(user)
    response = change(client)
    assert response.status_code == 302 and response["Location"] == reverse("password_change_done")
    done = client.get(reverse("password_change_done"))
    assert "Password changed" in done.content.decode()
    assert "accounts/password_change_done.html" in [t.name for t in done.templates]
    user.refresh_from_db()
    assert user.check_password(NEW)
    assert client.get(reverse("password_change")).status_code == 200  # session survives the change


def test_wrong_old_password_and_mismatch_are_rejected(client, user):
    client.force_login(user)
    assert "incorrectly" in change(client, old="wrong").content.decode()
    assert "didn" in change(client, new2="Different-2026!").content.decode()
    assert "too common" in change(client, new1="12345678", new2="12345678").content.decode()
    user.refresh_from_db()
    assert user.check_password(OLD)
