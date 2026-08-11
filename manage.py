#!/usr/bin/env python
"""DONZO — Django management entrypoint."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django'ni o'rnatib bo'lmadi. backend/venv faollashtirilganiga ishonch hosil qiling: "
            "venv/Scripts/python.exe manage.py ..."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
