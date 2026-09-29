"""Django views for the routes the SDK and the browser call on Label Studio.

Every route resolves its caller the same way: the platform cookie that travels
with the request is handed to the portal, which answers who it belongs to. Nothing
here holds a credential of its own, so a session that expires stops working here
at exactly the moment it stops working on the platform.
"""

import hmac
import json
import logging
import time
from urllib.parse import urlsplit

import requests
from django.http import (
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
    StreamingHttpResponse,
)
from django.views.decorators.csrf import csrf_exempt

from pyromind_ls import portal_api

logger = logging.getLogger(__name__)

MEDIA_PROXY_CHUNK_BYTES = 64 * 1024
MEDIA_PROXY_TIMEOUT_SECONDS = 60.0
# The proxy response is per-user, so it may only be cached by that user's browser.
# A presigned object URL lives seven days; a day of browser caching covers the
# repeat views inside one annotation session without holding the bytes that long.
MEDIA_PROXY_CACHE_SECONDS = 24 * 60 * 60
JSON_BODY_MAX_BYTES = 1024 * 1024


def _error(detail: str, status: int) -> JsonResponse:
    return JsonResponse({'detail': detail}, status=status)


def _cookie_header(request) -> str:
    return request.META.get('HTTP_COOKIE', '') or ''


def _caller(config: portal_api.PortalConfig, request):
    """Resolve the platform account behind a request, or raise a response."""
    try:
        user = portal_api.resolve_user(config, _cookie_header(request))
    except portal_api.PortalIntegrationError as exc:
        logger.error('Label Studio could not reach the portal: %s', exc)
        raise _Rejected(_error('Portal is unavailable', 502)) from exc
    if user is None:
        raise _Rejected(_error('Login required or session expired', 401))
    return user


class _Rejected(Exception):
    def __init__(self, response):
        super().__init__('rejected')
        self.response = response


def normalize_target(target) -> str:
    """Keep a post-login redirect on this host, or fall back to the project list."""
    value = (target or '/').strip()
    if not value or len(value) > 2048 or '\\' in value:
        return '/'
    if not value.startswith('/') or value.startswith('//'):
        return '/'
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or any(ord(char) < 32 for char in value):
        return '/'
    if parsed.path.rstrip('/').lower() in {'/user/login', '/user/signup'}:
        return '/'
    return value


def sso(request):
    """Log a browser with a live platform session into its Label Studio account."""
    config = portal_api.get_config()
    if not config.browser_enabled:
        return _error('Label Studio integration is not configured', 503)

    target = normalize_target(request.GET.get('target'))
    try:
        user = portal_api.resolve_user(config, _cookie_header(request))
    except portal_api.PortalIntegrationError as exc:
        logger.error('Label Studio SSO could not reach the portal: %s', exc)
        return _error('Portal is unavailable', 502)
    if user is None:
        # The browser goes to the platform login and comes back here, which is what
        # keeps a saved project link working after the session has expired.
        callback = portal_api.sso_callback_url(config, target)
        return HttpResponseRedirect(portal_api.portal_login_url(config, callback))

    # Label Studio's inactivity middleware logs out any session without a
    # `last_login` stamp, and only its own login helper writes one. Logging in
    # through django.contrib.auth directly is therefore undone on the next
    # request, which turns this redirect into an endless SSO loop.
    from users.functions.common import login as label_studio_login

    try:
        from pyromind_ls.isolation import organization_for

        account = portal_api.ensure_label_studio_user(config, user)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 502 to the browser
        logger.exception('Label Studio SSO could not prepare the account: %s', exc)
        return _error('Label Studio account is unavailable', 502)

    label_studio_login(
        request, account, backend='django.contrib.auth.backends.ModelBackend'
    )
    organization = organization_for(account)
    if organization is not None and account.active_organization_id != organization.pk:
        account.active_organization_id = organization.pk
        account.save(update_fields=['active_organization'])

    response = HttpResponseRedirect(target)
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


def token(request):
    """Return the calling user's own Label Studio API token.

    The agent creates projects and fills them on the user's behalf, and Label
    Studio only shows an account the projects of its own organization. A token
    shared by every conversation would file each user's projects under one
    account, where the user who asked for them could not see them.
    """
    config = portal_api.get_config()
    if not config.enabled:
        return _error('Label Studio integration is not configured', 503)
    try:
        user = _caller(config, request)
    except _Rejected as rejected:
        return rejected.response
    try:
        account = portal_api.ensure_label_studio_user(config, user)
        value = portal_api.issue_api_token(account)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 502 to the caller
        logger.exception('Label Studio token lookup failed: %s', exc)
        return _error('Label Studio token is unavailable', 502)
    return JsonResponse({'token': value})


@csrf_exempt
def media_urls(request):
    """Turn Storage paths into stable media URLs for one import batch."""
    config = portal_api.get_config()
    if not config.enabled:
        return _error('Label Studio integration is not configured', 503)
    if request.method != 'POST':
        return _error('Method not allowed', 405)
    try:
        user = _caller(config, request)
    except _Rejected as rejected:
        return rejected.response

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _error('Invalid JSON body', 400)
    paths = payload.get('paths') if isinstance(payload, dict) else None
    if not isinstance(paths, list) or not paths or len(paths) > 1000:
        return _error('paths must be a non-empty list of at most 1000 entries', 400)
    for path in paths:
        if not isinstance(path, str) or len(path) > 2048:
            return _error('Invalid storage path', 400)

    cluster = (payload.get('cluster') or '').strip()
    try:
        portal_api.cluster_base_url(config, cluster)
    except portal_api.PortalIntegrationError as exc:
        return _error(str(exc), 400)

    urls = {}
    for requested_path in paths:
        object_path = portal_api.normalize_relative_storage_object_path(
            requested_path, user.uid
        )
        if object_path is None:
            return _error('Invalid storage path', 403)
        urls[requested_path] = portal_api.media_url(
            config, user.uid, object_path, cluster
        )
    return JsonResponse(
        {'urls': urls, 'expires_in': portal_api.MEDIA_URL_EXPIRY_SECONDS}
    )


def _same_path(left, right) -> bool:
    # compare_digest() rejects non-ASCII strings outright, and object paths carry
    # whatever Unicode lives in the user's directory names, so compare the UTF-8
    # bytes of two paths whose signatures have already been verified.
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def media(request):
    """Serve one Storage object to the browser that is allowed to see it.

    Label Studio bakes this URL into task data, so it has to work for the life of
    the project rather than for the life of a presigned URL. That is why the route
    proxies the bytes instead of redirecting: the browser talks only to this host,
    and Storage's own CORS rules never enter the picture.
    """
    config = portal_api.get_config()
    if not config.enabled:
        return _error('Label Studio integration is not configured', 503)

    cookie_header = _cookie_header(request)
    try:
        user = portal_api.resolve_user(config, cookie_header)
    except portal_api.PortalIntegrationError as exc:
        logger.error('Label Studio media could not reach the portal: %s', exc)
        return _error('Portal is unavailable', 502)
    if user is None:
        return _error('Login required or session expired', 401)

    raw_path = request.GET.get('path', '')
    cluster = (request.GET.get('cluster') or '').strip()
    media_token = request.GET.get('media_token')
    if media_token:
        try:
            payload = portal_api.verify_media_token(
                media_token, config.media_signing_secret
            )
        except portal_api.MediaTokenError as exc:
            logger.warning('Rejected invalid Label Studio media token: %s', exc)
            return _error('Invalid media token', 403)
        object_path = portal_api.normalize_storage_object_path(
            payload['path'], payload['uid']
        )
        query_path = portal_api.normalize_storage_object_path(raw_path, user.uid)
        # The token names an owner as well as a path: a URL handed to somebody else
        # must not become a way for them to read it through their own session.
        if (
            payload['uid'] != user.uid
            or object_path is None
            or query_path is None
            or not _same_path(object_path, query_path)
        ):
            return _error('Invalid media token path', 403)
        cluster = payload.get('cluster') or cluster
    else:
        object_path = portal_api.normalize_storage_object_path(raw_path, user.uid)
        if object_path is None:
            return _error('Invalid storage path', 403)

    try:
        presigned_url = portal_api.storage_download_url(
            config,
            cluster,
            portal_api.object_key_within_user_bucket(object_path, user.uid),
            cookie_header,
        )
    except portal_api.PortalSessionError as exc:
        logger.warning('Label Studio media session rejected: %s', exc)
        return _error('Storage session is not valid', 401)
    except portal_api.PortalIntegrationError as exc:
        logger.error('Label Studio media could not be signed: %s', exc)
        return _error('Failed to create media URL', 502)

    return _proxy_object(request, presigned_url)


def _proxy_object(request, url: str) -> HttpResponse:
    """Stream an object through this host, forwarding range requests untouched."""
    headers = {}
    if request.META.get('HTTP_RANGE'):
        headers['Range'] = request.META['HTTP_RANGE']
    try:
        upstream = requests.get(
            url, headers=headers, stream=True, timeout=MEDIA_PROXY_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        logger.error('Label Studio media fetch failed: %s', exc)
        return _error('Failed to read the media object', 502)
    if upstream.status_code >= 400:
        upstream.close()
        logger.error('Storage answered HTTP %s for a media object', upstream.status_code)
        return _error('Failed to read the media object', 502)

    def chunks():
        try:
            for chunk in upstream.iter_content(MEDIA_PROXY_CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    response = StreamingHttpResponse(chunks(), status=upstream.status_code)
    for name in (
        'Content-Type',
        'Content-Length',
        'Content-Range',
        'Accept-Ranges',
        'ETag',
        'Last-Modified',
    ):
        if name in upstream.headers:
            response[name] = upstream.headers[name]
    response['Cache-Control'] = f'private, max-age={MEDIA_PROXY_CACHE_SECONDS}'
    return response


@csrf_exempt
def export_token(request):
    """Mint the ticket that lets one project's exports land in its owner's storage.

    The caller names only the project and the cluster. The owner comes from the
    authenticated account and the object key is built here, so a ticket can never
    point at another user's storage nor at a file other than the project's export.
    """
    config = portal_api.get_config()
    if not config.enabled:
        return _error('Label Studio integration is not configured', 503)
    if request.method != 'POST':
        return _error('Method not allowed', 405)
    try:
        user = _caller(config, request)
    except _Rejected as rejected:
        return rejected.response

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _error('Invalid JSON body', 400)
    if not isinstance(payload, dict):
        return _error('Invalid JSON body', 400)
    cluster = (payload.get('cluster') or '').strip()
    project_ref = (payload.get('project_ref') or '').strip()
    try:
        portal_api.cluster_base_url(config, cluster)
        object_key = portal_api.export_object_key(project_ref)
    except portal_api.PortalIntegrationError as exc:
        return _error(str(exc), 400)

    issued_at = int(time.time())
    value = portal_api.create_export_ticket(
        uid=user.uid,
        object_path=object_key,
        signing_secret=config.export_signing_secret,
        cluster=cluster,
        project_ref=project_ref,
        issued_at=issued_at,
    )
    return JsonResponse(
        {
            'token': value,
            'path': object_key,
            'cluster': cluster,
            'expires_at': issued_at + portal_api.EXPORT_TICKET_EXPIRY_SECONDS,
        }
    )


def upload_export(ticket: str, body: bytes, cookie_header: str) -> str:
    """Store one export and return the ticket that replaces the one presented.

    Called from inside Label Studio's own export code, which runs on the request
    the annotator is waiting for. The renewal is what keeps a project that nobody
    has re-imported working: every successful export hands back a fresh ticket.
    """
    config = portal_api.get_config()
    if not config.enabled:
        raise portal_api.PortalIntegrationError('Label Studio integration is off')
    if not body:
        raise portal_api.PortalIntegrationError('Empty Label Studio export')
    if len(body) > portal_api.EXPORT_UPLOAD_MAX_BYTES:
        raise portal_api.PortalIntegrationError('Label Studio export is too large')
    if not cookie_header:
        raise portal_api.PortalSessionError('No platform session on this request')
    # An expired ticket is still accepted. Its signature binds the owner, key and
    # cluster, and the write is authorised by the live platform session forwarded
    # to Storage, so refusing a stale one would only strand a project that nobody
    # re-imported -- with no way to refresh it from inside Label Studio.
    claims = portal_api.verify_export_ticket(
        ticket, config.export_signing_secret, allow_expired=True
    )
    cluster = claims['cluster']
    object_key = claims['path']
    portal_api.storage_upload_object(
        config,
        cluster,
        object_key,
        portal_api.EXPORT_OBJECT_NAME,
        body,
        cookie_header,
    )
    issued_at = int(time.time())
    return portal_api.create_export_ticket(
        uid=claims['uid'],
        object_path=object_key,
        signing_secret=config.export_signing_secret,
        cluster=cluster,
        project_ref=str(claims.get('project_ref') or ''),
        issued_at=issued_at,
    )


@csrf_exempt
def export_upload(request):
    """HTTP entry point for the same upload, kept for manual and tooling calls."""
    if request.method != 'POST':
        return _error('Method not allowed', 405)
    ticket = request.headers.get('X-Pyromind-Export-Token', '')
    if not ticket:
        return _error('Missing export ticket', 401)
    try:
        renewed = upload_export(ticket, request.body, _cookie_header(request))
    except portal_api.ExportTicketError as exc:
        logger.warning('Label Studio export ticket rejected: %s', exc)
        return _error('Invalid Label Studio export ticket', 401)
    except portal_api.PortalSessionError as exc:
        logger.warning('Label Studio export session rejected: %s', exc)
        return _error('Storage session is not valid', 401)
    except portal_api.PortalIntegrationError as exc:
        logger.error('Label Studio export upload failed: %s', exc)
        return _error('Failed to store the Label Studio export', 502)
    return JsonResponse({'bytes': len(request.body), 'token': renewed})
