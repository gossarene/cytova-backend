"""
Cytova — Result PDF Password Protection

Derives a password from patient data according to the lab's configured
mode, then encrypts the PDF bytes using pypdf's standard encryption.
The encrypted PDF requires the password to open in any standard reader.

Password modes:
    PATIENT_DOB           → YYYYMMDD
    PATIENT_PHONE         → digits only
    REQUEST_REFERENCE     → public_reference
    DOB_PLUS_PHONE_SUFFIX → YYYYMMDD-XXXX (last 4 phone digits)

If required patient data is missing, generation is blocked with a
``ValidationError`` so an unprotected PDF is never stored.
"""
import io
import re

from pypdf import PdfReader, PdfWriter
from rest_framework.exceptions import ValidationError

from apps.lab_settings.models import LabSettings


def derive_password(analysis_request, settings: LabSettings) -> str:
    """
    Derive the PDF password from patient data + the configured mode.

    Raises ``ValidationError`` if required data is missing.
    """
    mode = settings.result_pdf_password_mode
    patient = analysis_request.patient

    dob_str = ''
    if patient.date_of_birth:
        dob_str = patient.date_of_birth.strftime('%Y%m%d')

    phone_digits = re.sub(r'\D', '', patient.phone or '')

    if mode == 'PATIENT_DOB':
        if not dob_str:
            raise ValidationError(
                'Cannot generate protected PDF: patient date of birth is missing.'
            )
        return dob_str

    if mode == 'PATIENT_PHONE':
        if not phone_digits:
            raise ValidationError(
                'Cannot generate protected PDF: patient phone number is missing.'
            )
        return phone_digits

    if mode == 'REQUEST_REFERENCE':
        ref = analysis_request.public_reference
        if not ref:
            raise ValidationError(
                'Cannot generate protected PDF: request public reference is missing.'
            )
        return ref

    if mode == 'DOB_PLUS_PHONE_SUFFIX':
        if not dob_str:
            raise ValidationError(
                'Cannot generate protected PDF: patient date of birth is missing.'
            )
        if len(phone_digits) < 4:
            raise ValidationError(
                'Cannot generate protected PDF: patient phone number must have '
                'at least 4 digits.'
            )
        suffix = phone_digits[-4:]
        return f'{dob_str}-{suffix}'

    if mode == 'DOB_PHONE_SECRET':
        if not dob_str:
            raise ValidationError(
                'Cannot generate protected PDF: patient date of birth is missing.'
            )
        if len(phone_digits) < 4:
            raise ValidationError(
                'Cannot generate protected PDF: patient phone number must have '
                'at least 4 digits.'
            )
        secret = settings.lab_secret_code or ''
        if not secret:
            raise ValidationError(
                'Cannot generate protected PDF: lab secret code is not configured.'
            )
        suffix = phone_digits[-4:]
        return f'{dob_str}-{suffix}-{secret}'

    raise ValidationError(f'Unknown PDF password mode: {mode}')


def encrypt_pdf(pdf_bytes: bytes, password: str) -> bytes:
    """
    Apply AES-128 user-password encryption to the PDF.

    The resulting bytes require ``password`` to open in any standard
    reader. No owner password is set — the user password is the only
    key.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def protect_if_enabled(pdf_bytes: bytes, analysis_request, settings=None) -> bytes:
    """
    If PDF protection is enabled in lab settings, derive the password
    and return encrypted bytes. Otherwise return the input unchanged.
    """
    if settings is None:
        settings = LabSettings.get_solo()
    if not settings.result_pdf_password_enabled:
        return pdf_bytes
    password = derive_password(analysis_request, settings)
    return encrypt_pdf(pdf_bytes, password)
