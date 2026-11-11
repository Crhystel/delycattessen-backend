"""Audit log: who did what and when. Used by the API views, the superadmin and the
system (failed tasks). It is best effort and never stores secrets."""
import logging

from .models import AuditLog, CustomUser, Institution

logger = logging.getLogger(__name__)

_SECRET_MARKERS = ('password', 'token', 'secret', 'code', 'pin')


def display_name(user):
    parts = [user.first_name, user.second_name, user.last_name, user.second_last_name]
    return ' '.join(part for part in parts if part) or user.username


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    return (forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')) or None


def _clean_detail(detail):
    """Drops anything that looks like a secret: the log must never hold credentials."""
    return {
        key: value for key, value in (detail or {}).items()
        if not any(marker in str(key).lower() for marker in _SECRET_MARKERS)
    }


def record_audit(action, *, actor=None, target=None, institution=None, source=AuditLog.Source.ADMIN,
                 request=None, detail=None):
    """Writes one audit entry. Best effort: a failure here is logged but never breaks
    the operation being recorded. `target` is a CustomUser or an Institution."""
    try:
        target_type, target_id, target_label = '', None, ''
        if isinstance(target, CustomUser):
            target_type, target_id, target_label = 'usuario', target.pk, display_name(target)
            institution = institution or target.institution
        elif isinstance(target, Institution):
            target_type, target_id, target_label = 'sede', target.pk, target.name
            institution = institution or target
        has_actor = getattr(actor, 'pk', None) is not None
        return AuditLog.objects.create(
            actor=actor if has_actor else None,
            actor_label=display_name(actor) if has_actor else '',
            action=action, target_type=target_type, target_id=target_id, target_label=target_label[:200],
            institution=institution, source=source, ip=_client_ip(request), detail=_clean_detail(detail),
        )
    except Exception:  # noqa: BLE001 - auditing must never take the operation down
        logger.exception('Could not write the audit entry %s', action)
        return None
