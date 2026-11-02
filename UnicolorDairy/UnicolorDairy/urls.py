from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

from Home import security

urlpatterns = [
    path("login/", security.ProtectedLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password/",
        security.PasswordChangeView.as_view(success_url="/password/done/"),
        name="password_change",
    ),
    path("password/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    # Everyone logs in through the protected page, including for the Django admin.
    path("django-admin/login/", RedirectView.as_view(pattern_name="login", query_string=True)),
    path("django-admin/", admin.site.urls),
    path("", include("Home.urls")),
    path(
    "forgot-password/",
    auth_views.PasswordResetView.as_view(
        template_name="registration/password_reset_form.html",
        email_template_name="registration/password_reset_email.txt",
        html_email_template_name="registration/password_reset_email.html",
        subject_template_name="registration/password_reset_subject.txt",
    ),
    name="password_reset",
),

    path(
        "forgot-password/sent/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html"
        ),
        name="password_reset_done",
    ),

    path(
        "reset-password/<uidb64>/<token>/",
        security.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html"
        ),
        name="password_reset_confirm",
    ),

    path(
        "reset-password/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
]
