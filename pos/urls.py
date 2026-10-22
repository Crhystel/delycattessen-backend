from django.urls import path
from .views import (PreOrderCreateView, POSFaceIdentificationView, POSQrIdentificationView, PreOrderListView, PreOrderDeliverView, SaleCreateView, CreditHistoryView, ParentSaleListView, SalesSummaryView, SalesTimeseriesView, TopProductsView)

urlpatterns = [
    path('preorder/', PreOrderCreateView.as_view(), name='preorder_create'),
    path('preorders/', PreOrderListView.as_view(), name='preorder_list'),
    path('preorders/<int:pre_order_id>/deliver/', PreOrderDeliverView.as_view(), name='preorder_deliver'),
    path('sales/', SaleCreateView.as_view(), name='sale_create'),
    path('sales/history/', ParentSaleListView.as_view(), name='sale_history'),
    path('credit/history/', CreditHistoryView.as_view(), name='credit_history'),
    path('analytics/summary/', SalesSummaryView.as_view(), name='analytics_summary'),
    path('analytics/timeseries/', SalesTimeseriesView.as_view(), name='analytics_timeseries'),
    path('analytics/top-products/', TopProductsView.as_view(), name='analytics_top_products'),
    path('identify/face/', POSFaceIdentificationView.as_view(), name='pos_identify_face'),
    path('identify/qr/', POSQrIdentificationView.as_view(), name='pos_identify_qr'),
]