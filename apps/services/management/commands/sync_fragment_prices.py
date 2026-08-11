"""
Qo'lda ishga tushirish: python manage.py sync_fragment_prices [--force]

Fragment API jonli narxlari bilan Telegram Premium/Stars paketlari narxlarini
yangilaydi. Kunlik avtomatik sinxronlash bot.py dagi loop orqali ishlaydi — bu
buyruq faqat qo'lda / scheduler orqali chaqirish uchun.
"""

from django.core.management.base import BaseCommand

from apps.services.fragment_price_sync import sync_fragment_prices


class Command(BaseCommand):
    help = 'Fragment API jonli narxlari bilan Telegram Premium/Stars paketlarini yangilaydi'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='24 soatlik intervalni chetlab o\'tib, hoziroq sinxronlash',
        )

    def handle(self, *args, **options):
        result = sync_fragment_prices(force=options['force'])
        self.stdout.write(self.style.SUCCESS(f"[SYNC] {result['result']}"))
        for detail in result.get('details', []):
            self.stdout.write(f"  • {detail}")
