from django.urls import path

from .views import (
    KushkiWebhookView,
    PayphoneCallbackView,
    PayphoneRedirectView,
    WalletRechargeView,
    WalletTransactionListView,
)

app_name = 'wallet'

urlpatterns = [
    path('recharge/', WalletRechargeView.as_view(), name='recharge'),
    path('payphone/callback/', PayphoneCallbackView.as_view(), name='payphone-callback'),
    path('<int:wallet_id>/transactions/', WalletTransactionListView.as_view(), name='transactions'),
    path('payphone/redirect/', PayphoneRedirectView.as_view(), name='payphone-redirect'),
    path('kushki/webhook/', KushkiWebhookView.as_view(), name='kushki-webhook'),
]