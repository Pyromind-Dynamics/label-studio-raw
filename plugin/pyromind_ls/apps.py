"""Django app entry point for the PyroMind plugin.

The export hook has to replace a method on a Label Studio model, and models cannot
be imported while settings are still loading, so the hook installs itself from
ready() instead of from settings.py.
"""

from django.apps import AppConfig


class PyromindLabelStudioConfig(AppConfig):
    name = 'pyromind_ls'

    def ready(self):
        from pyromind_ls import export_hook

        export_hook.install()
