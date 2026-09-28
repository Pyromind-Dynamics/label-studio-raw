"""The PyroMind integration endpoints, hosted by Label Studio itself.

The portal used to own these routes: it validated the platform session, signed the
media tokens and export tickets, and forwarded them to Storage. Label Studio needs
the same facts and already sees the browser's platform cookie -- the portal sets
``auth_token`` for ``.pyromind.ai``, the parent domain both hosts live under -- so
hosting the routes here keeps the platform's shared web secret out of the
deployment. Every request re-derives access from the caller's own live session
instead of from a key that outlives it.

Identity is the one thing this process cannot work out for itself: verifying the
platform JWT needs the platform's web secret. It asks the portal's existing
``/account/checkLogin`` instead, which keeps that secret where it is.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# Configuration names. The non-secret values live in the label-studio ConfigMap and
# the two secrets in the label-studio Secret; both reach the process through envFrom.
PORTAL_BASE_URL_ENV = 'PYROMIND_PORTAL_BASE_URL'
CLUSTER_MAP_ENV = 'PYROMIND_CLUSTER_MAP'
SSO_SECRET_ENV = 'LABEL_STUDIO_SSO_SECRET'
MEDIA_SECRET_ENV = 'LABEL_STUDIO_MEDIA_SECRET'
# How this deployment is reached from a browser, so the login round trip can come
# back to it. Label Studio already requires this value in the same environment.
LABEL_STUDIO_BASE_URL_ENV = 'LABEL_STUDIO_HOST'

SSO_PATH = '/label_studio/sso'
MEDIA_PATH = '/label_studio/media'
TOKEN_PATH = '/label_studio/token'
MEDIA_URLS_PATH = '/label_studio/media-urls'
EXPORT_TOKEN_PATH = '/label_studio/export_token'
EXPORT_UPLOAD_PATH = '/label_studio/export_upload'

PORTAL_CHECK_LOGIN_PATH = '/account/checkLogin'
STORAGE_GET_URL_PATH = '/storage_api/get_url'
STORAGE_PRESIGNED_UPLOAD_PATH = '/storage_api/presigned_upload_url'

REQUEST_TIMEOUT_SECONDS = 5.0
EXPORT_UPLOAD_TIMEOUT_SECONDS = 300.0

# Task data bakes the signed media URL in at import time and Label Studio never
# re-signs it, so the token has to outlive the whole annotation window, not one
# page load.
MEDIA_URL_EXPIRY_SECONDS = 7 * 24 * 60 * 60
MEDIA_TOKEN_AUDIENCE = 'label-studio-media-v1'
# Purpose strings, so a token minted for one use can never verify as another.
MEDIA_SIGNING_PURPOSE = 'label-studio-media-signing-v1'
EXPORT_SIGNING_PURPOSE = 'label-studio-export-signing-v1'

EXPORT_TICKET_AUDIENCE = 'label-studio-export-v1'
EXPORT_TICKET_LEEWAY_SECONDS = 60
# Label Studio bakes the object key into the project description and never
# refreshes it on its own, so a ticket has to outlive a long annotation window.
# The SDK mints a fresh one every time it writes that description.
EXPORT_TICKET_EXPIRY_SECONDS = 180 * 24 * 60 * 60
EXPORT_OBJECT_NAME = 'label_studio_export.json'
EXPORT_OBJECT_KEY_PATTERN = re.compile(
    r'^/\.pyromind-agent/label-studio/[A-Za-z0-9._-]+/export/'
    + re.escape(EXPORT_OBJECT_NAME)
    + r'$'
)
EXPORT_PROJECT_REF_PATTERN = re.compile(r'^[A-Za-z0-9._-]+$')
EXPORT_UPLOAD_MAX_BYTES = 64 * 1024 * 1024


class PortalIntegrationError(RuntimeError):
    """The integration is misconfigured, or an upstream refused the request."""


class PortalSessionError(PortalIntegrationError):
    """The caller presented no usable platform session."""


class TokenError(ValueError):
    pass


class MediaTokenError(TokenError):
    pass


class ExportTicketError(TokenError):
    pass


@dataclass(frozen=True)
class PortalConfig:
    """Everything the integration needs, resolved once per call."""

    portal_base_url: str
    sso_secret: str
    media_secret: str
    label_studio_base_url: str = ''
    cluster_map: dict[str, str] = field(default_factory=dict)
    timeout: float = REQUEST_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.portal_base_url and self.sso_secret)

    @property
    def browser_enabled(self) -> bool:
        """Whether a login round trip can name this deployment as its target."""
        return self.enabled and bool(self.label_studio_base_url)

    @property
    def media_signing_secret(self) -> str:
        """Key used to sign media tokens.

        A dedicated secret keeps media signing independent from the Label Studio
        account password derivation.
        """
        if self.media_secret:
            return self.media_secret
        return derive_purpose_secret(self.sso_secret, MEDIA_SIGNING_PURPOSE)

    @property
    def export_signing_secret(self) -> str:
        """Key used to sign export tickets.

        Derived per purpose rather than shared with the login or media keys, so a
        ticket can never verify as either of them.
        """
        return derive_purpose_secret(self.sso_secret, EXPORT_SIGNING_PURPOSE)


def _cluster_map_from_env(raw: str) -> dict[str, str]:
    """Parse the cluster -> Storage gateway table.

    Storage is replicated per cluster, so a request that names its cluster has to
    resolve against this table rather than against anything inferred from the
    request; an unknown name can then never be turned into an arbitrary host.
    """
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.error('%s is not valid JSON; media routes stay off', CLUSTER_MAP_ENV)
        return {}
    if not isinstance(parsed, dict):
        logger.error('%s is not a JSON object; media routes stay off', CLUSTER_MAP_ENV)
        return {}
    return {
        str(name): str(url).rstrip('/')
        for name, url in parsed.items()
        if isinstance(name, str) and isinstance(url, str) and url.startswith('http')
    }


def get_config() -> PortalConfig:
    return PortalConfig(
        portal_base_url=os.environ.get(PORTAL_BASE_URL_ENV, '').strip().rstrip('/'),
        sso_secret=os.environ.get(SSO_SECRET_ENV, '').strip(),
        media_secret=os.environ.get(MEDIA_SECRET_ENV, '').strip(),
        label_studio_base_url=(
            os.environ.get(LABEL_STUDIO_BASE_URL_ENV, '').strip().rstrip('/')
        ),
        cluster_map=_cluster_map_from_env(os.environ.get(CLUSTER_MAP_ENV, '')),
    )


def cluster_base_url(config: PortalConfig, cluster: Optional[str]) -> str:
    candidate = (cluster or '').strip()
    base_url = config.cluster_map.get(candidate, '')
    if not base_url:
        raise PortalIntegrationError(
            f'Unknown storage cluster for Label Studio: {candidate!r}'
        )
    return base_url


# ---------------------------------------------------------------------------
# Platform identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortalUser:
    uid: int
    email: str
    username: str


def resolve_user(config: PortalConfig, cookie_header: str) -> Optional[PortalUser]:
    """Ask the portal who owns a session cookie.

    Returns None when the portal answers that nobody is logged in, and raises when
    it cannot answer at all -- the caller has to tell "expired session" apart from
    "portal is down", because one means log in again and the other means retry.
    """
    if not config.portal_base_url or not cookie_header:
        return None
    try:
        response = requests.post(
            f'{config.portal_base_url}{PORTAL_CHECK_LOGIN_PATH}',
            headers={'Accept': 'application/json', 'Cookie': cookie_header},
            timeout=config.timeout,
        )
    except requests.RequestException as exc:
        raise PortalIntegrationError(f'Portal is unavailable: {exc}') from exc
    if response.status_code >= 400:
        raise PortalIntegrationError(
            f'Portal checkLogin returned HTTP {response.status_code}'
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PortalIntegrationError('Portal checkLogin returned invalid JSON') from exc
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get('isLoggedIn') is not True:
        return None
    user = data.get('user')
    if not isinstance(user, dict):
        return None
    uid = user.get('uid')
    email = (user.get('email') or '').strip().lower()
    if not isinstance(uid, int) or uid <= 0 or not email:
        logger.warning('Portal checkLogin returned an unusable account: %s', user)
        return None
    return PortalUser(
        uid=uid,
        email=email,
        username=str(user.get('username') or email.split('@')[0]),
    )


# The export hook runs deep inside Label Studio's export code, which carries no
# request object, so the platform cookie is captured by middleware on the request
# that is already waiting on the export and picked up here on the same thread.
_request_cookie = threading.local()


def set_request_cookie(cookie_header: str) -> None:
    _request_cookie.value = cookie_header


def get_request_cookie() -> str:
    return getattr(_request_cookie, 'value', '')


# ---------------------------------------------------------------------------
# Label Studio accounts
# ---------------------------------------------------------------------------


def derive_sso_password(email: str, secret: str) -> str:
    """Derive a purpose-separated password for a portal-managed LS account."""
    if not email or not secret:
        raise PortalIntegrationError('Label Studio SSO is not configured')
    digest = hmac.new(
        secret.encode('utf-8'),
        f'label-studio-account-v1:{email.strip().lower()}'.encode('utf-8'),
        hashlib.sha256,
    ).digest()
    material = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return f'Pm_{material}Aa1!'


def ensure_label_studio_user(config: PortalConfig, email: str):
    """Return the Label Studio account for a portal user, creating it if needed.

    The password is derived, not chosen, because nobody ever types it: the SSO
    route logs the browser in directly and the token route only reads the account's
    API token. Deriving it from the email keeps the account reachable by Label
    Studio's own login form for as long as the deployment keeps the secret.
    """
    from users.models import User

    from pyromind_ls.isolation import ensure_private_organization

    address = email.strip().lower()
    user = User.objects.filter(email__iexact=address).first()
    if user is not None:
        return user
    user = User.objects.create_user(
        email=address,
        password=derive_sso_password(address, config.sso_secret),
    )
    user.username = address.split('@')[0]
    user.save(update_fields=['username'])
    ensure_private_organization(user)
    logger.info('Created Label Studio account %s for portal user', address)
    return user


def issue_api_token(user) -> str:
    """Return the account's API token, creating one if the signal ever missed."""
    from rest_framework.authtoken.models import Token

    token = Token.objects.filter(user=user).first()
    if token is None:
        token = Token.objects.create(user=user)
    return str(token)


# ---------------------------------------------------------------------------
# Token signing
# ---------------------------------------------------------------------------


def derive_purpose_secret(root_secret: str, purpose: str) -> str:
    """Derive a purpose-scoped subkey so one secret cannot be replayed as another."""
    if not root_secret or not purpose:
        raise PortalIntegrationError('Label Studio secret is not configured')
    return hmac.new(
        root_secret.encode('utf-8'), purpose.encode('utf-8'), hashlib.sha256
    ).hexdigest()


def _encode_token_payload(payload: dict) -> str:
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(payload_bytes).rstrip(b'=').decode('ascii')


def _sign_token_payload(encoded_payload: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode('utf-8'), encoded_payload.encode('ascii'), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def _decode_token(token: Optional[str], secret: str, error):
    if not token or not secret or len(token) > 8192:
        raise error('Invalid token')
    try:
        version, encoded_payload, encoded_signature = token.split('.', 2)
    except ValueError as exc:
        raise error('Malformed token') from exc
    if version != 'v1':
        raise error('Unsupported token version')
    expected = _sign_token_payload(encoded_payload, secret)
    if not hmac.compare_digest(encoded_signature, expected):
        raise error('Token signature mismatch')
    try:
        padded = encoded_payload + '=' * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise error('Malformed token payload') from exc
    if not isinstance(payload, dict):
        raise error('Malformed token payload')
    return payload


def create_media_token(
    uid: int,
    object_path: str,
    signing_secret: str,
    expiry_seconds: int = MEDIA_URL_EXPIRY_SECONDS,
    issued_at: Optional[int] = None,
    cluster: str = '',
) -> str:
    if not object_path or not signing_secret or uid <= 0 or expiry_seconds <= 0:
        raise MediaTokenError('Invalid media token input')
    current_time = int(time.time() if issued_at is None else issued_at)
    payload = {
        'aud': MEDIA_TOKEN_AUDIENCE,
        'uid': int(uid),
        'path': object_path,
        'iat': current_time,
        'exp': current_time + int(expiry_seconds),
    }
    if cluster:
        payload['cluster'] = cluster
    encoded_payload = _encode_token_payload(payload)
    return f'v1.{encoded_payload}.{_sign_token_payload(encoded_payload, signing_secret)}'


def verify_media_token(token: Optional[str], signing_secret: str) -> dict:
    """Verify a media token's signature and bindings, ignoring its age.

    The token carries ``iat``/``exp``, but this never rejects one for being stale.
    Label Studio stores the signed URL verbatim inside project task data and has no
    way to re-mint it, so a hard expiry would blank out the images of every project
    older than the TTL with no way to repair them from inside Label Studio. What
    guards the bytes is the rest of the route: the signature binds the token to one
    (uid, path, cluster) triple, and every request re-checks that owner against the
    portal before Storage is asked for anything.
    """
    payload = _decode_token(token, signing_secret, MediaTokenError)
    if payload.get('aud') != MEDIA_TOKEN_AUDIENCE:
        raise MediaTokenError('Invalid media token audience')
    if not isinstance(payload.get('uid'), int):
        raise MediaTokenError('Invalid media token uid')
    object_path = payload.get('path')
    if not isinstance(object_path, str) or not object_path:
        raise MediaTokenError('Invalid media token path')
    if not isinstance(payload.get('iat'), int) or not isinstance(payload.get('exp'), int):
        raise MediaTokenError('Invalid media token lifetime')
    cluster = payload.get('cluster', '')
    if not isinstance(cluster, str) or len(cluster) > 128 or '://' in cluster:
        raise MediaTokenError('Invalid media token cluster')
    return payload


def create_export_ticket(
    uid: int,
    object_path: str,
    signing_secret: str,
    cluster: str,
    project_ref: str = '',
    expiry_seconds: int = EXPORT_TICKET_EXPIRY_SECONDS,
    issued_at: Optional[int] = None,
) -> str:
    """Mint the capability Label Studio presents when it pushes an export.

    The ticket carries the owner, the object key and the cluster, so the upload
    route reads none of them off the request it receives.
    """
    if not object_path or not signing_secret or uid <= 0 or expiry_seconds <= 0:
        raise ExportTicketError('Invalid export ticket input')
    current_time = int(time.time() if issued_at is None else issued_at)
    payload = {
        'aud': EXPORT_TICKET_AUDIENCE,
        'uid': int(uid),
        'path': object_path,
        'cluster': cluster,
        'iat': current_time,
        'exp': current_time + int(expiry_seconds),
    }
    if project_ref:
        payload['project_ref'] = project_ref
    encoded_payload = _encode_token_payload(payload)
    return f'v1.{encoded_payload}.{_sign_token_payload(encoded_payload, signing_secret)}'


def verify_export_ticket(
    token: Optional[str], signing_secret: str, allow_expired: bool = False
) -> dict:
    payload = _decode_token(token, signing_secret, ExportTicketError)
    if payload.get('aud') != EXPORT_TICKET_AUDIENCE:
        raise ExportTicketError('Invalid export ticket audience')
    if not isinstance(payload.get('uid'), int) or payload['uid'] <= 0:
        raise ExportTicketError('Invalid export ticket uid')
    object_key = payload.get('path')
    # Checked again on the way in, so a ticket minted under an older layout can
    # never name an object this deployment would refuse to write.
    if not isinstance(object_key, str) or not EXPORT_OBJECT_KEY_PATTERN.match(object_key):
        raise ExportTicketError('Invalid export ticket path')
    if not isinstance(payload.get('iat'), int) or not isinstance(payload.get('exp'), int):
        raise ExportTicketError('Invalid export ticket lifetime')
    if not allow_expired and time.time() + EXPORT_TICKET_LEEWAY_SECONDS >= payload['exp']:
        raise ExportTicketError('Export ticket expired')
    cluster = payload.get('cluster')
    if not isinstance(cluster, str) or not cluster or len(cluster) > 128:
        raise ExportTicketError('Invalid export ticket cluster')
    return payload


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


def export_object_key(project_ref: str) -> str:
    """The one object key a project's export is ever allowed to overwrite."""
    reference = (project_ref or '').strip()
    if not EXPORT_PROJECT_REF_PATTERN.match(reference):
        raise PortalIntegrationError('Invalid Label Studio project reference')
    return f'/.pyromind-agent/label-studio/{reference}/export/{EXPORT_OBJECT_NAME}'


def normalize_storage_object_path(raw_path: Optional[str], user_uid: int) -> Optional[str]:
    if not raw_path or '\\' in raw_path:
        return None
    object_path = raw_path.lstrip('/')
    try:
        normalized = str(PurePosixPath(object_path))
    except (TypeError, ValueError):
        return None
    if not normalized or normalized.startswith('/') or normalized == '.':
        return None
    parts = normalized.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        return None
    if not normalized.startswith(f'{user_uid}/'):
        return None
    return normalized


def normalize_relative_storage_object_path(
    raw_path: Optional[str], user_uid: int
) -> Optional[str]:
    if not isinstance(raw_path, str) or len(raw_path) > 2048:
        return None
    raw = raw_path.strip().lstrip('/')
    if not raw:
        return None
    if not raw.startswith(f'{user_uid}/'):
        # A leading numeric segment is a bucket claim, so numeric top-level folders
        # have to be passed uid-prefixed rather than silently absorbed as relative.
        head, _, _ = raw.partition('/')
        if head.isdigit():
            return None
        raw = f'{user_uid}/{raw}'
    return normalize_storage_object_path(f'/{raw}', user_uid)


def object_key_within_user_bucket(object_path: str, uid: int) -> str:
    """Map a validated ``{uid}/...`` path to its key inside bucket ``{uid}``.

    Storage keys carry no uid: the bucket is already named after the user, and
    every writer stores the caller's path verbatim. The prefix is an authorization
    claim validated above, not part of the stored key, so it must not be replayed
    into the presign call.
    """
    prefix = f'{uid}/'
    return object_path[len(prefix):] if object_path.startswith(prefix) else object_path


def media_url(config: PortalConfig, uid: int, object_path: str, cluster: str) -> str:
    """Build the stable URL that gets baked into task data.

    It points at this Label Studio, not at the portal, which is what makes the
    browser's request same-origin: the platform cookie then travels with it.
    """
    token = create_media_token(
        uid=uid,
        object_path=object_path,
        signing_secret=config.media_signing_secret,
        cluster=cluster,
    )
    request_path = quote(f'/{object_path}', safe='/')
    return f'{MEDIA_PATH}?path={request_path}&media_token={token}'


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _storage_data(response: requests.Response, cluster: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PortalIntegrationError(
            f'Storage service {cluster!r} returned an invalid response'
        ) from exc
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise PortalIntegrationError(
            f'Storage service {cluster!r} returned an invalid response'
        )
    if data.get('isLoggedIn') is False:
        raise PortalSessionError('Storage session is not valid')
    return data


def storage_download_url(
    config: PortalConfig, cluster: str, object_key: str, cookie_header: str
) -> str:
    """Ask Storage to sign a download URL for one object on the caller's behalf."""
    base_url = cluster_base_url(config, cluster)
    try:
        response = requests.post(
            f'{base_url}{STORAGE_GET_URL_PATH}',
            json={'path': object_key},
            headers={'Accept': 'application/json', 'Cookie': cookie_header},
            timeout=config.timeout,
        )
    except requests.RequestException as exc:
        raise PortalIntegrationError(f'Storage service is unavailable: {exc}') from exc
    if response.status_code >= 400:
        raise PortalIntegrationError(
            f'Storage service {cluster!r} rejected the media request: '
            f'HTTP {response.status_code}'
        )
    url = _storage_data(response, cluster).get('url')
    if not isinstance(url, str) or not url:
        raise PortalIntegrationError('Storage service returned no media URL')
    return url


def storage_upload_object(
    config: PortalConfig,
    cluster: str,
    object_key: str,
    filename: str,
    content: bytes,
    cookie_header: str,
) -> None:
    """Write one object through Storage's presigned upload.

    The object-key layout and the per-user bucket check belong to Storage, so this
    only forwards the caller's session; it holds no gateway credentials of its own.
    """
    base_url = cluster_base_url(config, cluster)
    headers = {'Accept': 'application/json', 'Cookie': cookie_header}
    try:
        response = requests.post(
            f'{base_url}{STORAGE_PRESIGNED_UPLOAD_PATH}',
            json={
                'filename': filename,
                'path': str(PurePosixPath(object_key).parent),
                'content_type': 'application/json',
                'size': len(content),
            },
            headers=headers,
            timeout=EXPORT_UPLOAD_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            raise PortalIntegrationError(
                f'Storage service {cluster!r} rejected the export upload: '
                f'HTTP {response.status_code}'
            )
        data = _storage_data(response, cluster)
        if data.get('multipart') is True:
            raise PortalIntegrationError(
                'Storage service asked for a multipart upload, which the Label '
                'Studio export path does not support'
            )
        upload_url = data.get('upload_url')
        if not isinstance(upload_url, str) or not upload_url.strip():
            raise PortalIntegrationError(
                'Storage service returned no upload URL for this export'
            )
        extra_headers = data.get('headers')
        uploaded = requests.request(
            str(data.get('method') or 'PUT').upper(),
            upload_url,
            data=content,
            headers=dict(extra_headers) if isinstance(extra_headers, dict) else {},
            timeout=EXPORT_UPLOAD_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PortalIntegrationError(f'Storage service is unavailable: {exc}') from exc
    if uploaded.status_code >= 400:
        raise PortalIntegrationError(
            f'Storage service rejected the uploaded export: HTTP '
            f'{uploaded.status_code}'
        )


def sso_callback_url(config: PortalConfig, target: str) -> str:
    """The absolute URL the platform login should return the browser to."""
    return (
        f'{config.label_studio_base_url}{SSO_PATH}'
        f'?target={quote(target, safe="")}'
    )


def portal_login_url(config: PortalConfig, callback: str) -> str:
    """Send a browser with an expired session through the platform login."""
    return (
        f'{config.portal_base_url}/account/routeLogin'
        f'?v=v2&target={quote(callback, safe="")}'
    )
