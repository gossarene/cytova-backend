"""
Cytova — File Storage Utility

Thin wrapper over Django's configured storage backend.
Keeps the results module decoupled from the underlying storage driver
(FileSystemStorage in dev, S3/MinIO in production).

Storage path convention:
    results/{result_id}/{uuid}{ext}

The generated key is stored in ResultFile.file_key — never returned to
clients as-is; all access is mediated through signed_urls.generate_download_url().
"""
import logging
import uuid as _uuid
import os

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


def store_result_file(file, result_id: str) -> tuple[str, int]:
    """
    Persist an uploaded file to the configured storage backend.

    Args:
        file:       Django UploadedFile (from request.FILES).
        result_id:  UUID of the parent ExamResult (used for path namespacing).

    Returns:
        (file_key, file_size) where file_key is the internal storage path.
    """
    original_name = getattr(file, 'name', 'file')
    _, ext = os.path.splitext(original_name)
    ext = ext.lower()

    unique_name = f'{_uuid.uuid4().hex}{ext}'
    file_key = f'results/{result_id}/{unique_name}'

    # Reset pointer in case it has been read already
    file.seek(0)
    default_storage.save(file_key, ContentFile(file.read()))

    file_size = getattr(file, 'size', 0)
    logger.debug('Stored result file: %s (%d bytes)', file_key, file_size)

    return file_key, file_size


def delete_stored_file(file_key: str) -> None:
    """
    Remove a file from the configured storage backend.
    Silently ignores files that do not exist (idempotent).
    """
    try:
        if default_storage.exists(file_key):
            default_storage.delete(file_key)
            logger.debug('Deleted stored file: %s', file_key)
    except Exception:
        # Log but do not raise — physical delete failure should not
        # block the DB record from being cleaned up.
        logger.exception('Failed to delete stored file: %s', file_key)
