"""Replace Label Studio's export modal with a PyroMind one.

Label Studio's export UI is a React route (/data/export) rendered by the front-end
bundle baked into the nginx image, and this deployment does not rebuild that bundle.
The dialog is therefore replaced from outside the bundle: a small script is injected
into every HTML page and takes the Export button's click over before Label Studio's
own React handler on the root container sees it.

Serving that script from middleware, rather than from a template or a static file,
leaves Label Studio's templates and URL configuration untouched -- the two things a
Label Studio upgrade is most likely to rewrite.
"""

import json
import logging
import os
from functools import lru_cache

from django.http import HttpResponse
from django.template.response import TemplateResponse

logger = logging.getLogger(__name__)

# Served by this middleware, so no URL configuration entry is needed for it.
SCRIPT_URL = '/pyromind/export-ui.js'

_SCRIPT_FILENAME = 'export_ui.js'
_CONSOLE_BASE_ENV = 'PYROMIND_EXPORT_CONSOLE'

# Replaced with the console origin when the script is served. The quotes are part of
# the placeholder so the substitution yields a complete JavaScript string literal.
_CONSOLE_PLACEHOLDER = '"__PYROMIND_CONSOLE_BASE__"'


@lru_cache(maxsize=1)
def _script_source() -> str:
    """Read the browser script, which the ConfigMap mounts beside this module."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _SCRIPT_FILENAME)
    try:
        with open(path, encoding='utf-8') as handle:
            return handle.read()
    except OSError as exc:
        logger.warning('PyroMind export UI script is unreadable at %s: %s', path, exc)
        return ''


class ExportUiMiddleware:
    """Serve the export dialog script and splice it into Label Studio's pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == SCRIPT_URL:
            return self._script_response()
        return self._inject(self.get_response(request))

    @staticmethod
    def _script_response() -> HttpResponse:
        source = _script_source()
        if not source:
            # Better an explicit 404 than a page that silently keeps the stock modal.
            return HttpResponse(
                '', content_type='application/javascript; charset=utf-8', status=404
            )

        console_base = os.environ.get(_CONSOLE_BASE_ENV, '').strip().rstrip('/')
        if console_base:
            source = source.replace(_CONSOLE_PLACEHOLDER, json.dumps(console_base))
        else:
            # The dialog can still export; it just cannot offer the console link.
            logger.warning(
                '%s is not set, so the export dialog will offer no storage link',
                _CONSOLE_BASE_ENV,
            )

        response = HttpResponse(
            source, content_type='application/javascript; charset=utf-8'
        )
        # The inlined console origin differs per deployment, so a cached copy would go
        # stale the moment the deployment is repointed.
        response['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @staticmethod
    def _inject(response) -> HttpResponse:
        content_type = response.get('Content-Type', '')
        if response.status_code != 200 or 'text/html' not in content_type:
            return response

        # Django renders a TemplateResponse after the middleware chain unwinds, but by
        # the time a middleware sees the response the handler has already rendered it.
        # The check stays for safety, and rendering early is idempotent.
        if isinstance(response, TemplateResponse) and not response.is_rendered:
            response.render()

        if getattr(response, 'streaming', False):
            return response

        try:
            html = response.content.decode(response.charset or 'utf-8')
        except (AttributeError, UnicodeDecodeError):
            return response

        # Injecting twice would run the script twice; the script guards against that
        # itself, but an untouched page is cheaper to send.
        if SCRIPT_URL in html or '</body>' not in html:
            return response

        snippet = f'<script src="{SCRIPT_URL}" defer></script>'
        response.content = html.replace('</body>', f'{snippet}</body>', 1)
        # Assigning .content does not refresh a Content-Length that an earlier layer
        # set, and a stale one would truncate the page once it is sent.
        if 'Content-Length' in response:
            response['Content-Length'] = str(len(response.content))
        return response
