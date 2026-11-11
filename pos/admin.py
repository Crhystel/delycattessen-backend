from django.contrib import admin  # noqa: F401

# The superadmin does not list the wallet, sales and billing tables: their balances and totals
# are kept by the API services, and people are managed from users.admin.
