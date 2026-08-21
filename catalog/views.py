from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from .models import MenuItem
from .serializers import MenuItemSerializer

class MenuListView(generics.ListAPIView):
    queryset = MenuItem.objects.filter(is_active=True)
    serializer_class = MenuItemSerializer
    permission_classes = [IsAuthenticated]
