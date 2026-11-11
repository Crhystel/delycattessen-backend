"""Mixins that keep the Django superadmin from breaking the rest of the system.

Business invariants (wallet balances, stored allergens, totals, profiles created
together with their account) are maintained by the services behind the API, so
the superadmin only reads those tables. Nothing is ever hard-deleted here.
"""


class NoDeleteAdminMixin:
    """Hard deletes cascade into history; accounts are deactivated or soft-deleted instead."""

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyAdminMixin(NoDeleteAdminMixin):
    """Lists and detail pages only: no add, change or delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ReadOnlyInlineMixin(NoDeleteAdminMixin):
    """Same rule for inlines (their permission methods also receive the parent object)."""

    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
