# -*- coding: utf-8 -*-
"""Auto-fix AI kod tuzatish: backup + revert testlari."""
import os
import tempfile
import unittest
from unittest import mock

import django
from django.test import TestCase

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.security import auto_fix


class AiFixBackupRevertTests(TestCase):
    def setUp(self):
        # Haqiqiy backend fayllariga tegmaslik uchun BASE_DIR va backup
        # papkasini vaqtinchalik joyga yo'naltiramiz — test fayllar ham shu
        # papkada yoziladi, hech qanday real kodga tegmaydi.
        self._tmp = tempfile.mkdtemp(prefix='ai_fix_test_')
        self._orig_base = auto_fix.BASE_DIR
        self._orig_backup = auto_fix.AI_FIX_BACKUP_DIR
        self._orig_roots = auto_fix.ALLOWED_ROOTS
        auto_fix.BASE_DIR = self._tmp
        auto_fix.AI_FIX_BACKUP_DIR = os.path.join(self._tmp, 'backups', 'ai_fix')
        os.makedirs(auto_fix.AI_FIX_BACKUP_DIR, exist_ok=True)
        # _resolve_abs faqat ALLOWED_ROOTS ichiga ruxsat beradi — vaqtinchalik
        # papkani ham qo'shamiz (test fayllar shu yerda yoziladi).
        auto_fix.ALLOWED_ROOTS = tuple(auto_fix.ALLOWED_ROOTS) + (self._tmp,)

    def tearDown(self):
        auto_fix.BASE_DIR = self._orig_base
        auto_fix.AI_FIX_BACKUP_DIR = self._orig_backup
        auto_fix.ALLOWED_ROOTS = self._orig_roots
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass

    def _tmp_rel(self, name):
        """Vaqtinchalik papkadagi fayl uchun rel path (BASE_DIR ichida)."""
        return name

    def _touch(self, rel, content='original'):
        abs_path = auto_fix._resolve_abs(rel)
        self.assertIsNotNone(abs_path, f'{rel} ruxsat etilishi kerak')
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return abs_path

    def test_apply_patch_backups_and_changes(self):
        rel = self._tmp_rel('test_fix_dummy.py')
        abs_path = self._touch(rel, 'line1\nORIGINAL_MARKER\nline3\n')
        res = auto_fix.apply_ai_patch(
            {'file': rel, 'old': 'ORIGINAL_MARKER', 'new': 'NEW_VALUE'},
            actor_username='test_owner',
        )
        self.assertTrue(res['ok'])
        self.assertIn(rel, res['applied'])
        with open(abs_path, encoding='utf-8') as f:
            self.assertIn('NEW_VALUE', f.read())
        # Backup mavjud
        backup_dir = os.path.join(auto_fix.AI_FIX_BACKUP_DIR, res['backup_dir'])
        self.assertTrue(os.path.isdir(backup_dir))
        names = [n for n in os.listdir(backup_dir) if not n.endswith('.pyc')]
        self.assertTrue(any('test_fix_dummy' in n for n in names), names)

    def test_revert_restores_original(self):
        rel = self._tmp_rel('test_fix_dummy2.py')
        abs_path = self._touch(rel, 'A\nB\nC\n')
        res = auto_fix.apply_ai_patch(
            {'file': rel, 'old': 'B', 'new': 'X'},
            actor_username='test_owner',
        )
        self.assertTrue(res['ok'])
        with open(abs_path, encoding='utf-8') as f:
            self.assertIn('X', f.read())
        rev = auto_fix.revert_last_fix(actor_username='test_owner')
        self.assertTrue(rev['ok'])
        with open(abs_path, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('B', content)
        self.assertNotIn('X', content)

    def test_multiple_files_patch(self):
        rel1 = self._tmp_rel('test_fix_m1.py')
        rel2 = self._tmp_rel('test_fix_m2.py')
        self._touch(rel1, 'ONE\n')
        self._touch(rel2, 'TWO\n')
        res = auto_fix.apply_ai_patch({
            'replacements': [
                {'file': rel1, 'old': 'ONE', 'new': 'one'},
                {'file': rel2, 'old': 'TWO', 'new': 'two'},
            ]
        }, actor_username='test_owner')
        self.assertTrue(res['ok'])
        self.assertEqual(len(res['applied']), 2)
        rev = auto_fix.revert_last_fix(actor_username='test_owner')
        self.assertTrue(rev['ok'])
        self.assertEqual(len(rev['restored']), 2)

    def test_disallowed_path_rejected(self):
        res = auto_fix.apply_ai_patch(
            {'file': '../../outside.py', 'old': 'x', 'new': 'y'},
            actor_username='test_owner',
        )
        self.assertFalse(res['ok'])
        self.assertTrue(any('ruxsat' in e for e in res.get('errors', [])))

    def test_no_backup_no_revert(self):
        rev = auto_fix.revert_last_fix(actor_username='test_owner')
        self.assertFalse(rev['ok'])
        self.assertIn('topilmadi', rev.get('error', ''))

    def test_old_string_not_found(self):
        rel = self._tmp_rel('test_fix_nf.py')
        self._touch(rel, 'AAA\n')
        res = auto_fix.apply_ai_patch(
            {'file': rel, 'old': 'DOES_NOT_EXIST', 'new': 'X'},
            actor_username='test_owner',
        )
        self.assertFalse(res['ok'])
        self.assertTrue(any('topilmadi' in e for e in res.get('errors', [])))

    @mock.patch.object(auto_fix, '_gemini_patch')
    def test_ai_code_fix_no_change(self, mock_patch):
        mock_patch.return_value = {'ok': True, 'patch': {'replacements': []},
                                   'analysis': 'hammasi yaxshi'}
        res = auto_fix.ai_code_fix('nimadir ishlamayapti', 'test_owner')
        self.assertTrue(res['ok'])
        self.assertEqual(res.get('note'), 'no_change')

    @mock.patch.object(auto_fix, '_gemini_patch')
    def test_ai_code_fix_applies(self, mock_patch):
        rel = self._tmp_rel('test_fix_ai.py')
        self._touch(rel, 'ORIG\n')
        mock_patch.return_value = {
            'ok': True,
            'patch': {'replacements': [{'file': rel, 'old': 'ORIG', 'new': 'FIXED'}]},
            'analysis': 'xato topildi',
        }
        res = auto_fix.ai_code_fix('login xato', 'test_owner')
        self.assertTrue(res['ok'])
        self.assertIn(rel, res['applied'])
        with open(auto_fix._resolve_abs(rel), encoding='utf-8') as f:
            self.assertIn('FIXED', f.read())

    @mock.patch.object(auto_fix, '_gemini_patch')
    def test_ai_code_fix_gemini_error(self, mock_patch):
        mock_patch.return_value = {'ok': False, 'error': 'Gemini patch xato: Test'}
        res = auto_fix.ai_code_fix('xato', 'test_owner')
        self.assertFalse(res['ok'])
        self.assertIn('Gemini', res.get('error', ''))
