from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password/",
        auth_views.PasswordChangeView.as_view(success_url="/password/done/"),
        name="password_change",
    ),
    path("password/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("django-admin/", admin.site.urls),
    path("", include("Home.urls")),
]
