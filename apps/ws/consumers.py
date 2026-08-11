import json
from datetime import datetime

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import TokenError

from apps.users.models import User, Role
from .metrics import metrics


class OrderConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time order updates.
    
    Connection flow:
      1. Client connects with ?token=<JWT> query param
      2. Server authenticates via JWT
      3. Based on user role, joins appropriate groups:
         - Customers: only their own orders group
         - Operators: the global operator group
         - Admins: the global admin group
    
    Groups:
      - user_{user_id}         → personal notifications (everyone)
      - global_users           → broadcast announcements (everyone)
      - operator_orders        → new/updated orders (operators, admins)
      - admin_all              → all events (admins)
    """

    async def connect(self):
        self.user, subprotocol = await self._authenticate()
        
        if self.user is None or isinstance(self.user, AnonymousUser):
            await self.close(code=4001)
            return

        # SECURITY: the token arrives via the WebSocket SUBPROTOCOL
        # (Sec-WebSocket-Protocol) instead of the URL query string, so it
        # never appears in proxy / access logs. If the client offered a
        # subprotocol we must echo it back or the handshake fails.
        if subprotocol:
            await self.accept(subprotocol=subprotocol)
        else:
            await self.accept()

        # Personal group — receives events about own orders/payments
        self.personal_group = f'user_{self.user.id}'
        await self.channel_layer.group_add(self.personal_group, self.channel_name)

        # Global group — receives broadcast announcements from admins
        self.global_group = 'global_users'
        await self.channel_layer.group_add(self.global_group, self.channel_name)

        # Role-based groups
        if self.user.role in [Role.ADMIN, Role.SUPER_ADMIN, Role.SENIOR_OPERATOR]:
            self.admin_group = 'admin_all'
            await self.channel_layer.group_add(self.admin_group, self.channel_name)
        
        if self.user.role in [Role.OPERATOR, Role.SENIOR_OPERATOR, Role.ADMIN, Role.SUPER_ADMIN]:
            self.operator_group = 'operator_orders'
            await self.channel_layer.group_add(self.operator_group, self.channel_name)

        # NOTE: do NOT call accept() again here — the connection was already
        # accepted above (with the subprotocol echoed). A second accept() on an
        # accepted connection raises and daphne closes the socket with 1011,
        # which silently killed every WebSocket connection.

        # Track connection
        metrics.increment_connections()

        # Send initial connection confirmation
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'user_id': self.user.id,
            'role': self.user.role,
            'timestamp': datetime.now().isoformat(),
        }))

    async def disconnect(self, close_code):
        # Leave all groups
        if hasattr(self, 'global_group'):
            await self.channel_layer.group_discard(self.global_group, self.channel_name)
        if hasattr(self, 'personal_group'):
            await self.channel_layer.group_discard(self.personal_group, self.channel_name)
        if hasattr(self, 'admin_group'):
            await self.channel_layer.group_discard(self.admin_group, self.channel_name)
        if hasattr(self, 'operator_group'):
            await self.channel_layer.group_discard(self.operator_group, self.channel_name)
        # Track disconnection
        metrics.decrement_connections()

    async def receive(self, text_data):
        """Handle incoming messages from the client (e.g. pings)."""
        try:
            data = json.loads(text_data)
            if data.get('type') == 'ping':
                await self.send(text_data=json.dumps({'type': 'pong'}))
        except json.JSONDecodeError:
            pass

    # ── Event Handlers (called by channel_layer.group_send) ──

    async def order_created(self, event):
        """New order created — notify operators."""
        metrics.record_event('order_created')
        await self.send(text_data=json.dumps({
            'type': 'order_created',
            'order': event['order'],
            'timestamp': datetime.now().isoformat(),
        }))

    async def order_updated(self, event):
        """Order status changed — notify relevant parties."""
        metrics.record_event('order_updated')
        await self.send(text_data=json.dumps({
            'type': 'order_updated',
            'order': event['order'],
            'changes': event.get('changes', {}),
            'timestamp': datetime.now().isoformat(),
        }))

    async def payment_received(self, event):
        """Payment completed — notify user + admins."""
        metrics.record_event('payment_received')
        await self.send(text_data=json.dumps({
            'type': 'payment_received',
            'payment': event['payment'],
            'order': event.get('order'),
            'timestamp': datetime.now().isoformat(),
        }))

    async def operator_assigned(self, event):
        """Operator assigned to an order."""
        metrics.record_event('operator_assigned')
        await self.send(text_data=json.dumps({
            'type': 'operator_assigned',
            'order': event['order'],
            'operator_name': event.get('operator_name', ''),
            'timestamp': datetime.now().isoformat(),
        }))

    async def notification(self, event):
        """Generic notification."""
        metrics.record_event('notification')
        await self.send(text_data=json.dumps({
            'type': 'notification',
            'title': event.get('title', ''),
            'message': event.get('message', ''),
            'level': event.get('level', 'info'),
            'timestamp': datetime.now().isoformat(),
        }))

    async def telegram_session(self, event):
        """
        New Telegram Web App session (successful login OR rejected attempt)
        — pushed live to admins for the 'Jonli sessiyalar' monitoring screen.
        """
        metrics.record_event('telegram_session')
        await self.send(text_data=json.dumps({
            'type': 'telegram_session',
            'session': event.get('session', {}),
            'timestamp': datetime.now().isoformat(),
        }))

    @database_sync_to_async
    def _authenticate(self):
        """
        Extract and validate the JWT token.

        Preferred transport: WebSocket subprotocol 'token.<jwt>' (keeps the
        token out of URLs/logs). Fallback: legacy '?token=<jwt>' query param
        for older clients. Returns (user, subprotocol_to_echo).
        """
        # 1) Subprotocol (preferred, no log leak)
        offered = self.scope.get('subprotocols') or []
        subprotocol = None
        token = None
        for sp in offered:
            if sp.startswith('token.'):
                token = sp[len('token.'):]
                subprotocol = sp
                break
        # 2) Legacy query-param fallback
        if not token:
            token_str = self.scope.get('query_string', b'').decode()
            params = {}
            for part in token_str.split('&'):
                if '=' in part:
                    k, v = part.split('=', 1)
                    params[k] = v
            token = params.get('token')
            subprotocol = None
        if not token:
            return None, None
        try:
            access = AccessToken(token)
            user = User.objects.get(id=access['user_id'])
            return user, subprotocol
        except (TokenError, User.DoesNotExist, KeyError):
            return None, None
