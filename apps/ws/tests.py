"""
Live-session WebSocket consumer tests (DEMO MODE).

Login tizimi olib tashlangani uchun auth view'lar endi telegram_session
push qilmaydi — faqat OrderConsumer'ning telegram_session handler'i sinov
qilinadi (admin "Jonli sessiyalar" ekrani o'z poll fallback'iga ega).
"""
import json
import asyncio

from django.test import TestCase


class SessionConsumerHandlerTests(TestCase):
    """OrderConsumer.telegram_session forwards the event to the client."""

    def test_handler_emits_json_message(self):
        from apps.ws.consumers import OrderConsumer

        consumer = OrderConsumer()
        received = []

        async def fake_send(text_data):
            received.append(json.loads(text_data))

        consumer.send = fake_send  # type: ignore[method-assign]

        asyncio.run(consumer.telegram_session({
            'type': 'telegram_session',
            'session': {'id': 99, 'telegram_id': 'demo', 'is_authenticated': True},
        }))

        self.assertEqual(len(received), 1)
        msg = received[0]
        self.assertEqual(msg['type'], 'telegram_session')
        self.assertEqual(msg['session']['id'], 99)
        self.assertIn('timestamp', msg)
