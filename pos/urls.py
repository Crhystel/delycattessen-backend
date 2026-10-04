from django.urls import path
from .views import PreOrderCreateView, POSFaceIdentificationView, POSQrIdentificationView, PreOrderListView

urlpatterns = [
    path('preorder/', PreOrderCreateView.as_view(), name='preorder_create'),
    path('preorders/', PreOrderListView.as_view(), name='preorder_list'),
    path('identify/face/', POSFaceIdentificationView.as_view(), name='pos_identify_face'),
    path('identify/qr/', POSQrIdentificationView.as_view(), name='pos_identify_qr'),
]