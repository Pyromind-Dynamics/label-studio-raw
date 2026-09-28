"""The deployment's root URLconf: the integration's routes, then Label Studio's.

DJANGO_SETTINGS_MODULE points ROOT_URLCONF here instead of at core.urls, because
Label Studio's own URLconf ends in a series of catch-all includes -- anything not
listed before them is answered by those. The integration's routes are therefore
prepended, and the stock patterns are reused unchanged rather than copied.
"""

from core.urls import urlpatterns as upstream_urlpatterns
from django.urls import path

from pyromind_ls import portal_views

urlpatterns = [
    path('label_studio/sso', portal_views.sso, name='pyromind-label-studio-sso'),
    path('label_studio/token', portal_views.token, name='pyromind-label-studio-token'),
    path(
        'label_studio/media-urls',
        portal_views.media_urls,
        name='pyromind-label-studio-media-urls',
    ),
    path('label_studio/media', portal_views.media, name='pyromind-label-studio-media'),
    path(
        'label_studio/export_token',
        portal_views.export_token,
        name='pyromind-label-studio-export-token',
    ),
    path(
        'label_studio/export_upload',
        portal_views.export_upload,
        name='pyromind-label-studio-export-upload',
    ),
] + upstream_urlpatterns
