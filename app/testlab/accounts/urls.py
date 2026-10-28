from django.contrib.auth.views import LoginView, PasswordChangeDoneView, PasswordChangeView
from django.urls import path

from .forms import LoginForm
from .views import RegisterView

urlpatterns = [
    # Before django.contrib.auth.urls, so this login view (email address) is used.
    path("login/", LoginView.as_view(authentication_form=LoginForm), name="login"),
    path("register/", RegisterView.as_view(), name="register"),
    # Also before django.contrib.auth.urls: the admin's templates of the same name would win otherwise.
    path(
        "password_change/",
        PasswordChangeView.as_view(template_name="accounts/password_change.html"),
        name="password_change",
    ),
    path(
        "password_change/done/",
        PasswordChangeDoneView.as_view(template_name="accounts/password_change_done.html"),
        name="password_change_done",
    ),
]
