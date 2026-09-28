"""Store every export in the object store as soon as Label Studio generates it.

Label Studio has two separate export paths, and both are wrapped at the point where
each one has the raw JSON in hand:

  * DataExport.save_export_files -- the Data Manager's Export button, the project's
    easy-export download and the CLI's `label-studio export` all reach it through
    DataExport.generate_export_file, and it is handed the project's raw JSON before
    any conversion. That argument is what gets uploaded, so neither the format the
    annotator picked nor the media-bundling setting can change what is stored.
    Hooking generate_export_file instead would only ever see the converted file,
    which this deployment turns into a zip of media and the tasks.
  * Export.save_file -- the Export API's delayed export writes a snapshot of the
    whole project through it, always as plain JSON.

Neither is a Label Studio extension point; they are wrapped at startup instead.
Where the file goes, and on whose behalf, is settled by the export ticket this
deployment minted when the project was created and the SDK wrote into the
project's description. The ticket names an owner and an object key and nothing
else, so it can never point anywhere but at that project's own export.

The upload itself runs against the session of the annotator who asked for the
export: the middleware parks that request's platform cookie for the duration of
the request, and Storage is asked to sign the write with it. That is what replaced
the portal's long-lived key -- every export is authorised by a session that was
alive a moment ago. A fresh ticket is written back into the description on the way
out, so a project the agent never touches again still renews itself as long as
someone exports from it.
"""

import logging
import re
from functools import wraps

from pyromind_ls import portal_api, portal_views

logger = logging.getLogger(__name__)

# Storage refuses anything larger, so there is no point reading it into memory.
_MAX_BYTES = 64 * 1024 * 1024
_TICKET_LINE = re.compile(r'^export-token:[ \t]*(\S+)[ \t]*$', re.MULTILINE)


def install() -> None:
    """Send every export through this module, once per process."""
    from data_export.models import DataExport, Export

    _replace(Export, 'save_file', _hooked_save_file)
    # save_export_files is a staticmethod, so its replacement has to be one too:
    # every caller reaches it as DataExport.save_export_files. Upstream has carried
    # a "TODO: deprecated" on it for years, so re-check it when bumping Label Studio.
    _replace(DataExport, 'save_export_files', _hooked_save_export_files)

    config = portal_api.get_config()
    logger.info(
        'PyroMind export hook installed (target=%s)',
        config.portal_base_url or 'unconfigured',
    )


def _replace(owner, name, hooked):
    current = getattr(owner, name)
    if getattr(current, '_pyromind_export_hook', False):
        return
    replacement = hooked(current)
    if isinstance(owner.__dict__.get(name), staticmethod):
        replacement = staticmethod(replacement)
    setattr(owner, name, replacement)


def _hooked_save_file(original):
    @wraps(original)
    def save_file(self, file, md5):
        # Read first: the original hands the same handle to Django's storage, which
        # reads from wherever the file is left.
        body = _read_and_rewind(file)
        result = original(self, file, md5)
        _store_export(self.project, body)
        return result

    save_file._pyromind_export_hook = True
    return save_file


def _hooked_save_export_files(original):
    @wraps(original)
    def save_export_files(project, now, get_args, data, md5, name):
        result = original(project, now, get_args, data, md5, name)
        # `data` is the JSON Label Studio writes into its own export directory: the
        # tasks as serialized, before the converter reshapes them. Taking the body
        # from here is what keeps the stored object the raw export no matter what
        # the annotator chose in the export dialog.
        _store_export(project, _as_body(data))
        return result

    save_export_files._pyromind_export_hook = True
    return save_export_files


def _as_body(data) -> bytes:
    """Encode Label Studio's export JSON, refusing anything over the size cap."""
    body = data if isinstance(data, bytes) else data.encode('utf-8')
    if len(body) > _MAX_BYTES:
        logger.warning('An export exceeds %s bytes; nothing uploaded', _MAX_BYTES)
        return b''
    return body


def _read_and_rewind(file) -> bytes:
    """Read up to the size cap, leaving the handle where the caller expects it.

    The handle is not necessarily at the start: Export.export_to_file hashes the
    snapshot with eval_md5() immediately before calling save_file(), and that reads
    the file to the end without rewinding. Reading from wherever the caller left it
    would therefore always yield the empty body -- which _store_export then treats
    as "nothing to store" and skips in silence. Rewinding is also harmless to the
    original, whose own copy (Django's File.chunks) seeks to zero anyway.
    """
    try:
        file.seek(0)
        body = file.read(_MAX_BYTES + 1)
        file.seek(0)
    except (OSError, ValueError) as exc:
        logger.warning('Could not read a Label Studio export: %s', exc)
        return b''
    return _as_body(body)


def _store_export(project, body: bytes) -> None:
    """Store the export before the request that produced it returns.

    This runs synchronously on purpose: the export button's whole point is that the
    annotator ends up with the copy in Storage, and the page can only say "done"
    once that copy exists. The response they are already waiting for is the
    confirmation.

    Storage being unreachable still cannot fail the export -- every failure is
    logged and swallowed below, so Label Studio's own export is unaffected.
    """
    if not body:
        return

    description = project.description or ''
    match = _TICKET_LINE.search(description)
    if match is None:
        logger.warning(
            'Project %s carries no PyroMind export ticket; nothing uploaded. '
            'Re-run the agent that owns the project to obtain one.',
            project.id,
        )
        return

    cookie_header = portal_api.get_request_cookie()
    if not cookie_header:
        logger.warning(
            'Project %s was exported without a platform session on the request; '
            'nothing uploaded.',
            project.id,
        )
        return

    try:
        renewed = portal_views.upload_export(match.group(1), body, cookie_header)
    except portal_api.PortalIntegrationError as exc:
        # Nothing may escape: this now runs on the request the annotator is
        # waiting for, so an unhandled error here would fail their export over a
        # copy that is only a convenience.
        logger.warning('Could not store the export of project %s: %s', project.id, exc)
        return
    logger.info('Stored the export of project %s (%s bytes)', project.id, len(body))
    # 换票据只是顺带的好处，写回不能反过来影响已经成功的导出。
    if renewed:
        _write_ticket(project, renewed)


def _write_ticket(project, token: str) -> None:
    """Swap the ticket line in the description, leaving every other line alone.

    The description is re-read first so a concurrent write from the agent -- the
    "last export" line it adds after its own export -- is not clobbered by the
    copy this request started with.
    """
    try:
        from projects.models import Project

        description = (
            Project.objects.filter(pk=project.pk)
            .values_list('description', flat=True)
            .first()
        )
        if description is None:
            return
        updated = _TICKET_LINE.sub(
            lambda _match: 'export-token: ' + token, description, count=1
        )
        if updated == description:
            return
        Project.objects.filter(pk=project.pk).update(description=updated)
        logger.info('Refreshed the PyroMind export ticket of project %s', project.pk)
    except Exception as exc:
        # A missing ticket refresh costs nothing today; an unhandled error here
        # would fail the annotator's export over it.
        logger.warning(
            'Could not refresh the export ticket of project %s: %s', project.pk, exc
        )
