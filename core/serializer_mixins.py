from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from core.timezone import LOCAL_TIMEZONE


class LineSummaryMixin(serializers.Serializer):
    """
    Mixin Pattern: adds `display_name`, `time` and `items` to any serializer of
    a purchase made of product lines. Subclasses only implement get_lines(obj),
    returning (name, quantity, price) tuples, and list the three fields in Meta.
    Meant to be combined with a ModelSerializer that has a `created_at` field.
    """
    empty_display_name = _('Consumo')

    display_name = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()

    def get_lines(self, obj):
        raise NotImplementedError

    def get_display_name(self, obj):
        names = [name for name, *_rest in self.get_lines(obj)]
        if not names:
            return self.empty_display_name
        if len(names) == 1:
            return names[0]
        return _('%(first)s y %(count)s más') % {'first': names[0], 'count': len(names) - 1}

    def get_time(self, obj):
        local_time = obj.created_at.astimezone(LOCAL_TIMEZONE)
        return local_time.strftime('%I:%M %p').lstrip('0').lower()

    def get_items(self, obj):
        return [
            {'menu_item_name': name, 'quantity': quantity, 'price_at_purchase': str(price)}
            for name, quantity, price in self.get_lines(obj)
        ]
