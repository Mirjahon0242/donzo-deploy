# -*- coding: utf-8 -*-
"""Data migration: hash legacy plaintext TelegramLoginCode rows.

Before 0007, login codes were stored as plaintext 6-digit strings. The
0007 migration only widened the column; any codes created before deploy
would still sit in the DB as raw plaintext. This migration re-hashes them
so the audit guarantee ("never store plaintext") also holds for existing
rows. Codes are 6 digits and single-use with a 5-minute TTL, so there is
no behavioural risk in rewriting them.
"""
from django.db import migrations


def hash_legacy_codes(apps, schema_editor):
    import hashlib
    TelegramLoginCode = apps.get_model('users', 'TelegramLoginCode')
    for obj in TelegramLoginCode.objects.all().iterator():
        value = obj.code or ''
        # Legacy plaintext codes were exactly 6 digits (the SHA-256 hex form
        # is 64 chars). Rewrite anything that is not already a 64-char hex.
        if len(value) != 64:
            obj.code = hashlib.sha256(value.encode('utf-8')).hexdigest()
            obj.save(update_fields=['code'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_alter_telegramlogincode_code'),
    ]

    operations = [
        migrations.RunPython(hash_legacy_codes, noop),
    ]
