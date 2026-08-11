from django.urls import path
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from decimal import Decimal

from .models import PromoCode
from .serializers import PromoCodeValidateSerializer


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def validate_promo_code(request):
    """Validate a promo code and return discount info."""
    serializer = PromoCodeValidateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    code = serializer.validated_data['code'].strip().upper()
    order_amount = serializer.validated_data['order_amount']

    try:
        promo = PromoCode.objects.get(code=code)
    except PromoCode.DoesNotExist:
        return Response({
            'valid': False,
            'message': 'Promo kod topilmadi',
        }, status=status.HTTP_404_NOT_FOUND)

    user = request.user if request.user.is_authenticated else None
    valid, message = promo.is_valid(user=user, order_amount=order_amount)

    if not valid:
        return Response({
            'valid': False,
            'message': message,
        }, status=status.HTTP_400_BAD_REQUEST)

    discount = promo.calculate_discount(order_amount)
    final_amount = order_amount - discount

    return Response({
        'valid': True,
        'code': promo.code,
        'discount_type': promo.discount_type,
        'discount_value': float(promo.discount_value),
        'discount_amount': float(discount),
        'original_amount': float(order_amount),
        'final_amount': float(final_amount),
        'description': promo.description,
    })


urlpatterns = [
    path('validate/', validate_promo_code, name='promocode-validate'),
]
