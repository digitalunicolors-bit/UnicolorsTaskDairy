from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse


class ForcePasswordChangeMiddleware:
    """Someone whose password was set by an admin must choose their own before doing anything else."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.must_change_password:
            allowed = {reverse("password_change"), reverse("logout")}
            if request.path not in allowed and not request.path.startswith("/static/"):
                if request.headers.get("X-Requested-With") != "fetch" and request.method == "GET":
                    messages.info(request, "Your password was set by an admin. Please choose your own to continue.")
                return redirect("password_change")
        return self.get_response(request)
