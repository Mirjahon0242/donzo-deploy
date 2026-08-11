from django.urls import path
from . import export_orders

urlpatterns = [
    path('orders/csv/', export_orders.export_orders_csv, name='export-orders-csv'),
    path('orders/csv/token/', export_orders.export_orders_csv_token, name='export-orders-csv-token'),
]
