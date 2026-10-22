from django.contrib.auth.backends import ModelBackend

from .models import User


class EmailOrUsernameBackend(ModelBackend):
    """Lets people log in with their username OR their email address."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        login = username.strip()
        if "@" in login:
            user = User.objects.filter(email__iexact=login).order_by("id").first()
        else:
            user = User.objects.filter(username__iexact=login).first()
        if user is None:
            User().set_password(password)  # same timing as a real check
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
