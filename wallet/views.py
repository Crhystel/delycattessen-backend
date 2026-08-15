from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from wallet.services import PayphonePreparer

from .models import Wallet
from .permissions import IsWalletOwnerParent
from .serializers import RechargeRequestSerializer, TransactionSerializer
from .tasks import process_recharge_task


class WalletRechargeView(APIView):

    permission_classes = [IsAuthenticated, IsWalletOwnerParent]

    def post(self, request):
        serializer = RechargeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        wallet = get_object_or_404(Wallet, pk=serializer.validated_data['wallet_id'])
        self.check_object_permissions(request, wallet)

        amount = serializer.validated_data['amount']

        is_payphone_range = amount < settings.WALLET_RECHARGE_GATEWAY_THRESHOLD
        if is_payphone_range and not settings.WALLET_USE_FAKE_GATEWAYS:
            result = PayphonePreparer().prepare(wallet, amount)
            return Response(
                {
                    'transaction_id': result['transaction_id'],
                    'payment_url': result['payment_url'],
                },
                status=status.HTTP_200_OK,
            )

        process_recharge_task.delay(wallet.id, str(amount))
        return Response(
            {'detail': 'Recharge request received, processing.'},
            status=status.HTTP_202_ACCEPTED,
        )


class PayphoneCallbackView(APIView):
    """GET /api/wallet/payphone/callback/?id=...&clientTransactionId=..."""

    permission_classes = [AllowAny]

    def get(self, request):
        payphone_id = request.query_params.get('id')
        client_transaction_id = request.query_params.get('clientTransactionId')

        if not payphone_id or not client_transaction_id:
            return Response({'detail': 'Missing id or clientTransactionId.'}, status=400)

        transaction = PayphonePreparer().confirm(int(payphone_id), client_transaction_id)
        return Response({'status': transaction.status})


class WalletTransactionListView(APIView):
    """GET /api/wallet/<wallet_id>/transactions/ — para que Flutter haga
    polling del estado mientras la recarga se procesa en background."""

    permission_classes = [IsAuthenticated, IsWalletOwnerParent]

    def get(self, request, wallet_id):
        wallet = get_object_or_404(Wallet, pk=wallet_id)
        self.check_object_permissions(request, wallet)

        transactions = wallet.transactions.all()[:10]
        serializer = TransactionSerializer(transactions, many=True)
        return Response(serializer.data)