#!/usr/bin/env python
"""Entry point for the djangocms-automation demo project.

The demo exists so reliability work can be seen rather than only asserted: it
gives you an editor to build workflows in, a worker to drain a real queue, and
seeded reference automations to break on purpose.
"""

import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demoproject.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
