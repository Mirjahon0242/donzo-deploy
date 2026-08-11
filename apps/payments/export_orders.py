import csv
from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework import permissions
from rest_framework import status as drf_status
from rest_framework.response import Response

from apps.orders.models import Order
from apps.users.permissions import IsAdmin


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def export_orders_csv(request):
    """Export orders as CSV file."""
    status_filter = request.query_params.get('status', '')
    date_from = request.query_params.get('date_from', '')
    date_to = request.query_params.get('date_to', '')

    queryset = Order.objects.select_related('service', 'package', 'customer', 'assigned_operator')

    if status_filter:
        queryset = queryset.filter(status=status_filter)
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)

    queryset = queryset.order_by('-created_at')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="buyurtmalar.csv"'
    response.write('\ufeff')  # BOM for Excel

    writer = csv.writer(response)
    writer.writerow([
        'ID', 'Buyurtma Raqami', 'Mijoz', 'Xizmat', 'Paket',
        'Narx', 'Holat', "To'lov Holati", "To'lov Usuli",
        'Operator', 'Yaratilgan Sana',
    ])

    for order in queryset:
        writer.writerow([
            order.id,
            order.order_number,
            order.customer_name,
            order.service.name if order.service else '',
            order.package.name if order.package else '',
            float(order.total_price),
            order.get_status_display(),
            order.get_payment_status_display(),
            order.payment_method or '',
            order.assigned_operator.username if order.assigned_operator else '',
            order.created_at.strftime('%Y-%m-%d %H:%M'),
        ])

    return response


@api_view(['GET'])
@permission_classes([])
def export_orders_csv_token(request):
    """Export orders using token in query param (for browser download)."""
    token = request.query_params.get('token', '')
    if not token:
        return Response({'detail': 'Token majburiy'}, status=drf_status.HTTP_401_UNAUTHORIZED)

    # Validate token manually
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework_simplejwt.exceptions import TokenError
    try:
        access_token = AccessToken(token)
        user_id = access_token['user_id']
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(id=user_id)
    except (TokenError, User.DoesNotExist):
        return Response({'detail': 'Yaroqsiz token'}, status=drf_status.HTTP_401_UNAUTHORIZED)

    if user.role not in ['admin', 'super_admin']:
        return Response({'detail': 'Ruxsat yo\'q'}, status=drf_status.HTTP_403_FORBIDDEN)

    # Generate CSV
    status_filter = request.query_params.get('status', '')
    date_from = request.query_params.get('date_from', '')
    date_to = request.query_params.get('date_to', '')

    queryset = Order.objects.select_related('service', 'package', 'customer', 'assigned_operator')
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)
    queryset = queryset.order_by('-created_at')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="buyurtmalar.csv"'
    response.write('\ufeff')

    writer = csv.writer(response)
    writer.writerow(['ID', 'Buyurtma Raqami', 'Mijoz', 'Xizmat', 'Paket',
        'Narx', 'Holat', "To'lov Holati", "To'lov Usuli", 'Operator', 'Yaratilgan Sana'])

    for order in queryset:
        writer.writerow([
            order.id, order.order_number, order.customer_name,
            order.service.name if order.service else '',
            order.package.name if order.package else '',
            float(order.total_price), order.get_status_display(),
            order.get_payment_status_display(), order.payment_method or '',
            order.assigned_operator.username if order.assigned_operator else '',
            order.created_at.strftime('%Y-%m-%d %H:%M'),
        ])

    return response
