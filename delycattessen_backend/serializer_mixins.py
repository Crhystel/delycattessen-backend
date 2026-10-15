import zoneinfo

from rest_framework import serializers

LOCAL_TIMEZONE = zoneinfo.ZoneInfo('America/Guayaquil')


class LineSummaryMixin(serializers.Serializer):
    """
    Mixin Pattern: adds `display_name`, `time` and `items` to any serializer of
    a purchase made of product lines. Subclasses only implement get_lines(obj),
    returning (name, quantity, price) tuples, and list the three fields in Meta.
    Meant to be combined with a ModelSerializer that has a `created_at` field.
    """
    empty_display_name = 'Consumo'

    display_name = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()

    def get_lines(self, obj):
        raise NotImplementedError

    def get_display_name(self, obj):
        names = [name for name, _quantity, _price in self.get_lines(obj)]
        if not names:
            return self.empty_display_name
        if len(names) == 1:
            return names[0]
        return f'{names[0]} y {len(names) - 1} más'

    def get_time(self, obj):
        local_time = obj.created_at.astimezone(LOCAL_TIMEZONE)
        return local_time.strftime('%I:%M %p').lstrip('0').lower()

    def get_items(self, obj):
        return [
            {'menu_item_name': name, 'quantity': quantity, 'price_at_purchase': str(price)}
            for name, quantity, price in self.get_lines(obj)
        ]
