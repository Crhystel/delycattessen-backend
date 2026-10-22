import secrets

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import BasePermission


class IsWalletOwnerParent(BasePermission):
    """Only a father or mother can access the wallet of their child."""
    message = _('No tienes permiso para operar sobre esta billetera.')

    def has_object_permission(self, request, view, obj):
        return obj.student.parent.user_id == request.user.id


class HasKushkiWebhookSecret(BasePermission):
    """Kushki webhook guard: when KUSHKI_WEBHOOK_SECRET is configured the call
    must carry it in the X-Webhook-Secret header. Left empty the webhook stays
    open (only until the secret is set in the environment)."""
    message = _('Webhook no autorizado.')

    def has_permission(self, request, view):
        expected = settings.KUSHKI_WEBHOOK_SECRET
        if not expected:
            return True
        received = request.headers.get('X-Webhook-Secret', '')
        return secrets.compare_digest(received, expected)
