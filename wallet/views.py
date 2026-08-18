from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Transaction, Wallet
from .permissions import IsWalletOwnerParent
from .serializers import RechargeRequestSerializer, TransactionSerializer
from .services import GatewayError, PayphonePreparer
from .tasks import process_recharge_task, send_recharge_confirmation_email


class PayphoneRedirectView(APIView):
    """Intermediate page served from OUR OWN domain (the one registered in
    Payphone Developer). Flutter's WebView opens this URL first, not
    Payphone's directly, so the browser generates a valid Referer before
    jumping to Payphone's checkout."""

    permission_classes = [AllowAny]

    def get(self, request):
        target_url = request.query_params.get('target')
        if not target_url:
            return HttpResponse('Missing target parameter.', status=400)

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <meta name="referrer" content="strict-origin-when-cross-origin">
        </head>
        <body>
          <script>window.location.replace({target_url!r});</script>
        </body>
        </html>
        """
        response = HttpResponse(html)
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response


class WalletRechargeView(APIView):
    """POST /api/wallet/recharge/
    Amount >= threshold -> Kushki, tokenized on the frontend, processed
    async via Celery (RNF-05). Amount < threshold -> Payphone: returns a
    payment_url to open in a WebView."""

    permission_classes = [IsAuthenticated, IsWalletOwnerParent]

    def post(self, request):
        serializer = RechargeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        wallet = get_object_or_404(Wallet, pk=serializer.validated_data['wallet_id'])
        self.check_object_permissions(request, wallet)

        amount = serializer.validated_data['amount']
        kushki_token = serializer.validated_data.get('kushki_token')
        document_type = serializer.validated_data.get('document_type') or 'CC'
        document_number = serializer.validated_data.get('document_number')
        phone_number = serializer.validated_data.get('phone_number')

        is_payphone_range = amount < settings.WALLET_RECHARGE_GATEWAY_THRESHOLD
        if is_payphone_range:
            try:
                result = PayphonePreparer().prepare(wallet, amount)
            except GatewayError as exc:
                return Response({'detail': str(exc)}, status=400)
            return Response(
                {'transaction_id': result['transaction_id'], 'payment_url': result['payment_url']},
                status=status.HTTP_200_OK,
            )

        if not kushki_token:
            return Response({'detail': 'Falta el token de Kushki.'}, status=400)

        process_recharge_task.delay(
            wallet.id, str(amount), kushki_token, document_type, document_number, phone_number,
        )
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
    """GET /api/wallet/<wallet_id>/transactions/"""

    permission_classes = [IsAuthenticated, IsWalletOwnerParent]

    def get(self, request, wallet_id):
        wallet = get_object_or_404(Wallet, pk=wallet_id)
        self.check_object_permissions(request, wallet)

        transactions = wallet.transactions.all()[:10]
        serializer = TransactionSerializer(transactions, many=True)
        return Response(serializer.data)


class KushkiWebhookView(APIView):
    """POST /api/wallet/kushki/webhook/
    Kushki notifies transaction events here."""

    permission_classes = [AllowAny]

    def post(self, request):
        ticket_number = request.data.get('ticketNumber')
        transaction_status = request.data.get('transactionStatus')

        if not ticket_number:
            return Response({'detail': 'Missing ticketNumber.'}, status=400)

        transaction = Transaction.objects.filter(external_transaction_id=ticket_number).first()
        if transaction and transaction_status == 'APPROVED' and transaction.status != Transaction.Status.SUCCESS:
            transaction.status = Transaction.Status.SUCCESS
            transaction.save(update_fields=['status'])
            send_recharge_confirmation_email.delay(transaction.id)

        return Response({'received': True}, status=200)