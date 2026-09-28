"""Send an anonymous page load through this deployment's own SSO endpoint.

An account on this Label Studio is created by the portal and its password is
derived from a deployment secret, so a browser that lands here without a session
cannot get one from Label Studio's own login page. /label_studio/sso can, and it
hands the browser the session cookie -- but only if it is told which page the
browser wanted, so the original path and query string travel along as the target.
That is what keeps a saved project link, deep link and all, working after the
session has expired.

The middleware also parks the request's platform cookie for the duration of the
request. Label Studio's export code runs deep inside that same request and is
handed no request object, so parking it here is how the export hook reaches the
session Storage has to authorise the write against. It is cleared on the way out
so a later request on the same worker thread can never inherit it.

Only page navigations are touched. Label Studio's own account views stay reachable,
and API, static and health requests carry no account and are left alone.
"""

import logging
import os
from urllib.parse import quote

from django.http import HttpResponseRedirect

from pyromind_ls import portal_api

logger = logging.getLogger(__name__)

_SSO_PATH = '/label_studio/sso'
# /label_studio/* is the integration's own API, /user/* is Label Studio's account UI,
# /logout must not be answered with a fresh login, and the rest are not pages.
_PASSTHROUGH_PREFIXES = (
    '/label_studio/',
    '/user/',
    '/logout',
    '/api/',
    '/static/',
    '/health/',
    '/version/',
    '/pyromind/',
    '/.well-known/',
)
# The login round trip refuses longer targets, so an over-long request goes to the
# project list instead of into a login that would fail on the way back.
_MAX_TARGET_LENGTH = 2048


class PortalSsoMiddleware:
    """Enter the portal SSO flow at the page the browser asked for."""

    def __init__(self, get_response):
        self.get_response = get_response
        if not os.environ.get(portal_api.PORTAL_BASE_URL_ENV, '').strip():
            logger.warning(
                '%s is not set, so anonymous visits keep Label Studio\'s own '
                'login page',
                portal_api.PORTAL_BASE_URL_ENV,
            )

    def __call__(self, request):
        portal_api.set_request_cookie(request.META.get('HTTP_COOKIE', '') or '')
        try:
            if self._wants_page(request):
                return self._redirect(request.get_full_path())
            return self.get_response(request)
        finally:
            portal_api.set_request_cookie('')

    @staticmethod
    def _wants_page(request) -> bool:
        if request.method != 'GET':
            return False
        path = getattr(request, 'path', '') or ''
        if path.startswith(_PASSTHROUGH_PREFIXES):
            return False
        # Browsers ask for text/html when following a link. The editor's own
        # XHR, asset and download requests do not, and must not be bounced.
        if 'text/html' not in (request.headers.get('Accept') or ''):
            return False
        user = getattr(request, 'user', None)
        return user is None or not user.is_authenticated

    @staticmethod
    def _redirect(full_path: str) -> HttpResponseRedirect:
        target = full_path if len(full_path) <= _MAX_TARGET_LENGTH else '/'
        # Same host as the page that was asked for, so the browser keeps the
        # platform cookie that the SSO endpoint needs to read.
        response = HttpResponseRedirect(
            f'{_SSO_PATH}?target={quote(target, safe="")}'
        )
        response['Cache-Control'] = 'no-store'
        return response
