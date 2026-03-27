"""
Cytova — Signed URL Utility

Generates time-limited, pre-signed download URLs for files stored in
S3 or MinIO. Raw storage paths (file_key) are never returned to clients;
every access goes through this module.

In development (USE_S3=False) the function falls back to Django's local
media URL so tests and local dev work without S3 credentials.

Usage:
    from apps.files.signed_urls import generate_download_url

    url = generate_download_url(result_file.file_key)
    # → "https://bucket.s3.amazonaws.com/results/...?X-Amz-Signature=..."
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Default TTL for signed URLs — 15 minutes. Override via RESULT_FILE_SIGNED_URL_EXPIRY.
_DEFAULT_EXPIRY = 900


def generate_download_url(file_key: str, expires_in: int | None = None) -> str:
    """
    Return a pre-signed GET URL for `file_key`.

    Args:
        file_key:   Internal storage key (e.g. "results/{uuid}/{uuid}.pdf").
                    Never exposed to clients directly.
        expires_in: TTL in seconds. Defaults to RESULT_FILE_SIGNED_URL_EXPIRY
                    setting or 900 seconds.

    Returns:
        A time-limited URL string. On S3/MinIO this is a pre-signed URL.
        On local storage (dev) this is a plain media URL.
    """
    if expires_in is None:
        expires_in = getattr(settings, 'RESULT_FILE_SIGNED_URL_EXPIRY', _DEFAULT_EXPIRY)

    if getattr(settings, 'USE_S3', False):
        return _s3_presigned_url(file_key, expires_in)

    # Development fallback — Django local media URL (not secure, dev only)
    from django.core.files.storage import default_storage
    return default_storage.url(file_key)


def _s3_presigned_url(file_key: str, expires_in: int) -> str:
    """
    Generate a boto3 pre-signed URL for S3 / MinIO.

    Requires settings:
        AWS_ACCESS_KEY_ID
        AWS_SECRET_ACCESS_KEY
        AWS_STORAGE_BUCKET_NAME
        AWS_S3_ENDPOINT_URL   (optional; set for MinIO)
        AWS_S3_REGION_NAME    (optional; defaults to us-east-1)
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            'boto3 is required for S3 signed URLs. '
            'Install it: pip install boto3'
        )

    client = boto3.client(
        's3',
        endpoint_url=getattr(settings, 'AWS_S3_ENDPOINT_URL', None),
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1'),
    )

    return client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': settings.AWS_STORAGE_BUCKET_NAME,
            'Key': file_key,
        },
        ExpiresIn=expires_in,
    )
