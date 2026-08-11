import json
from datetime import datetime

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from apps.orders.models import Order
from apps.payments.models import Payment

channel_layer = get_channel_layer()


def _send_to_group(group: str, event_type: str, data: dict):
    """Helper to send a message to a channel layer group."""
    try:
        async_to_sync(channel_layer.group_send)(
            group,
            {
                'type': event_type,
                **data,
            }
        )
    except Exception as e:
        # Don't crash if channel layer fails (e.g. no Redis)
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"WebSocket send failed to {group}: {e}")


# ── Order Signals ──

def _order_to_dict(order):
    return {
        'id': order.id,
        'order_number': order.order_number,
        'status': order.status,
        'payment_status': order.payment_status,
        'total_price': float(order.total_price),
        'customer_name': order.customer_name or '',
        'customer_telegram': order.customer_telegram or '',
        'service_name': order.service.name if order.service else '',
        'package_name': order.package.name if order.package else '',
        'customer_id': order.customer.id if order.customer else None,
        'assigned_operator_id': order.assigned_operator.id if order.assigned_operator else None,
        'assigned_operator_name': order.assigned_operator.username if order.assigned_operator else '',
        'created_at': order.created_at.isoformat() if order.created_at else '',
        'updated_at': order.updated_at.isoformat() if order.updated_at else '',
    }


@receiver(post_save, sender=Order)
def order_saved(sender, instance, created, **kwargs):
    """Broadcast order events when an Order is saved."""
    order_data = _order_to_dict(instance)

    if created:
        # Notify all operators/admins about the new order
        _send_to_group('operator_orders', 'order_created', {
            'order': order_data,
        })
        _send_to_group('admin_all', 'order_created', {
            'order': order_data,
        })
    else:
        # Notify the customer about the status change
        if instance.customer:
            _send_to_group(f'user_{instance.customer.id}', 'order_updated', {
                'order': order_data,
            })
        
        # Notify operators/admins about the update
        _send_to_group('operator_orders', 'order_updated', {
            'order': order_data,
        })
        _send_to_group('admin_all', 'order_updated', {
            'order': order_data,
        })

    # If an operator was just assigned, notify them specifically
    if instance.assigned_operator:
        _send_to_group(f'user_{instance.assigned_operator.id}', 'order_updated', {
            'order': order_data,
            'changes': {'status': instance.status},
        })

    # Notify admins if payment status changed
    if not created and instance.payment_status:
        _send_to_group('admin_all', 'order_updated', {
            'order': order_data,
            'changes': {'payment_status': instance.payment_status},
        })


# ── Payment Signals ──

def _payment_to_dict(payment):
    return {
        'id': payment.id,
        'provider': payment.provider,
        'transaction_id': payment.transaction_id or '',
        'amount': float(payment.amount),
        'status': payment.status,
        'order_id': payment.order.id if payment.order else None,
        'created_at': payment.created_at.isoformat() if payment.created_at else '',
    }


@receiver(post_save, sender=Payment)
def payment_saved(sender, instance, created, **kwargs):
    """Broadcast payment events when a Payment is saved."""
    payment_data = _payment_to_dict(instance)

    if instance.status == 'success':
        # Notify the customer who made the payment
        if instance.order and instance.order.customer:
            _send_to_group(f'user_{instance.order.customer.id}', 'payment_received', {
                'payment': payment_data,
                'order': _order_to_dict(instance.order),
            })

        # Notify operators/admins
        _send_to_group('operator_orders', 'payment_received', {
            'payment': payment_data,
            'order': _order_to_dict(instance.order) if instance.order else None,
        })
        _send_to_group('admin_all', 'payment_received', {
            'payment': payment_data,
            'order': _order_to_dict(instance.order) if instance.order else None,
        })

    # Notify admins about failed/pending payments too
    _send_to_group('admin_all', 'payment_received', {
        'payment': payment_data,
        'order': _order_to_dict(instance.order) if instance.order else None,
    })
