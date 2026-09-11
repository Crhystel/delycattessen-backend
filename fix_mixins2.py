import os

file_path = 'pos/mixins.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    'from wallet.models import Transaction, TransactionType, TransactionStatus',
    'from wallet.models import Transaction'
)
content = content.replace(
    'transaction_type=TransactionType.CONSUMPTION,',
    'type=Transaction.Type.CONSUMPTION,'
)
content = content.replace(
    'status=TransactionStatus.COMPLETED,',
    'status=Transaction.Status.SUCCESS,'
)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Fixed pos/mixins.py")
