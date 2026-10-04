from django.urls import path
from .views import (PreOrderCreateView, POSFaceIdentificationView, POSQrIdentificationView, PreOrderListView, PreOrderDeliverView)

urlpatterns = [
    path('preorder/', PreOrderCreateView.as_view(), name='preorder_create'),
    path('preorders/', PreOrderListView.as_view(), name='preorder_list'),
    path('preorders/<int:pre_order_id>/deliver/', PreOrderDeliverView.as_view(), name='preorder_deliver'),
    path('identify/face/', POSFaceIdentificationView.as_view(), name='pos_identify_face'),
    path('identify/qr/', POSQrIdentificationView.as_view(), name='pos_identify_qr'),
]