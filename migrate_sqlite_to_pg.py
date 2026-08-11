#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite -> PostgreSQL ma'lumot ko'chirish scripti.

Barcha jadval qatorlarini PK'larni SAQLAB ko'chiradi (boshqa DB bo'sh bo'lsa
FK'lar o'z joyida qoladi). Faqat content type'lar natural key (app_label,
model) bo'yicha qayta xaritalanadi (django_admin_log uchun).

Ishlatish:
  # SQLite (manba) uchun DB_NAME bo'sh, target default (PostgreSQL)
  python migrate_sqlite_to_pg.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django

# Default (PostgreSQL) .env'dan yuklanadi; SQLite faqat qo'shimcha alias.
django.setup()

from django.apps import apps
from django.conf import settings
from django.db import connections, router

# SQLite uchun qo'shimcha alias (to'liq sozlamalar bilan)
settings.DATABASES['sqlite'] = {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db.sqlite3'),
    'USER': '',
    'PASSWORD': '',
    'HOST': '',
    'PORT': '',
    'CONN_MAX_AGE': 0,
    'CONN_HEALTH_CHECKS': False,
    'OPTIONS': {'timeout': 20},
    'TIME_ZONE': settings.TIME_ZONE,
    'AUTOCOMMIT': True,
    'ATOMIC_REQUESTS': False,
}
connections['sqlite']

SKIP_MODELS = {'django_migrations', 'django_content_type', 'auth_permission', 'auth_group'}


def content_type_map():
    """(app_label, model) -> id, SQLite va target uchun."""
    from django.contrib.contenttypes.models import ContentType
    src, dst = {}, {}
    for ct in ContentType.objects.using('sqlite').all():
        src[(ct.app_label, ct.model)] = ct.id
    for ct in ContentType.objects.using('default').all():
        dst[(ct.app_label, ct.model)] = ct.id
    return src, dst


def copy_model(model, src_ct_map=None, dst_ct_map=None, quiet=False):
    """Model qatorlarini sqlite -> default ko'chiradi (PK saqlanadi)."""
    meta = model._meta
    if meta.label_lower in SKIP_MODELS:
        return 0
    # Idempotent: oldin mavjud qatorlarni TRUNCATE bilan tozalaymiz
    # (CASCADE — FK bog'liqliklarini ham tozalaydi, delete() ProtectedError beradi)
    try:
        from django.db import connection
        with connection.cursor() as c:
            c.execute(f'TRUNCATE TABLE "{meta.db_table}" RESTART IDENTITY CASCADE')
    except Exception:
        pass
    objs = list(model.objects.using('sqlite').all())
    if not objs:
        if not quiet:
            print(f'  {meta.label_lower}: 0 (bo\'sh)')
        return 0

    # PK'ni saqlab ko'chirish
    for obj in objs:
        obj.pk = obj.pk  # PK ni o'zgartirmaymiz
        obj._state.adding = True  # INSERT sifatida yoziladi

    # content_type FK bo'lsa qayta xaritalash
    remapped = 0
    for obj in objs:
        for f in meta.fields:
            if f.get_internal_type() == 'ForeignKey' and f.related_model and \
               f.related_model._meta.label_lower == 'contenttypes.contenttype':
                old_id = getattr(obj, f.attname, None)
                if old_id:
                    key = None
                    for (app, mdl), cid in src_ct_map.items():
                        if cid == old_id:
                            key = (app, mdl)
                            break
                    if key and key in dst_ct_map:
                        setattr(obj, f.attname, dst_ct_map[key])
                        remapped += 1
    try:
        model.objects.using('default').bulk_create(objs, ignore_conflicts=False, batch_size=500)
    except Exception as e:
        print(f'  XATO {meta.label_lower}: {e}')
        return -1
    if not quiet:
        print(f'  {meta.label_lower}: {len(objs)} ko\'chirildi (ct_remap={remapped})')
    return len(objs)


def main():
    src_ct, dst_ct = content_type_map()
    print(f'Content types: sqlite={len(src_ct)}, target={len(dst_ct)}')

    print('\n=== Ko\'chirish ===')
    total = 0
    # Barcha loyiha app'laridagi modellar (PK saqlangani uchun FK tartibi
    # ahamiyatsiz — users avval ko'chiriladi)
    for app_config in apps.get_app_configs():
        if app_config.name.startswith('apps.'):
            for model in app_config.get_models():
                n = copy_model(model, src_ct, dst_ct)
                total += max(n, 0)

    # Django ichki modellar (users mavjud bo'lgandan keyin — FK uchun)
    for label in ('admin.LogEntry', 'sessions.Session'):
        model = apps.get_model(label)
        n = copy_model(model, src_ct, dst_ct)
        total += max(n, 0)

    print(f'\nJAMI ko\'chirildi: {total}')
    print('Tugadi.')


if __name__ == '__main__':
    main()
