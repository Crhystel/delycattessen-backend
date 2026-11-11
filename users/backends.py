from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

User = get_user_model()


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get('email')
        if username is None or password is None:
            return None
        try:
            user = User.objects.get(email__iexact=username)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            # The unique index on Lower(email) makes the second case impossible; it is
            # still treated as "not found" so it can never turn into a 500 at login.
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None