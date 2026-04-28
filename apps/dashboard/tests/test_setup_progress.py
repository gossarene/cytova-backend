"""
Tests for the lab-onboarding setup-progress endpoint.

Coverage:
  - HTTP route resolves and returns the documented top-level shape
  - non-LAB_ADMIN roles get ``null`` (only owners see the checklist)
  - each task's ``completed`` flag reflects real DB state
  - the percentage is computed off REQUIRED tasks only — completing the
    recommended partner task does not move it, and skipping it does not
    block reaching 100%
  - tenant isolation handled by the autouse ``_in_tenant_schema`` fixture
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.partners.models import OrganizationType
from apps.partners.services import PartnerOrganizationService


@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    from apps.tenants.models import (
        Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
    )
    with django_db_blocker.unblock():
        with schema_context(get_public_schema_name()):
            plan, _ = SubscriptionPlan.objects.get_or_create(
                code='TEST_TRIAL',
                defaults={
                    'name': 'Test Trial', 'is_trial': True,
                    'trial_duration_days': 30, 'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


def _client(user) -> APIClient:
    client = APIClient()
    token = RefreshToken.for_user(user).access_token
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _get(user):
    resp = _client(user).get(
        '/api/v1/dashboard/setup-progress/',
        HTTP_HOST='testlab.localhost',
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


def _reset_lab_settings():
    """Wipe LabSettings back to a fresh-install state. The migration
    pre-seeds some fields, so each test starts from the same baseline."""
    lab = LabSettings.get_solo()
    lab.lab_name = ''
    lab.address = ''
    lab.phone = ''
    lab.email = ''
    lab.logo_file_key = ''
    lab.logo_url = ''
    lab.legal_footer = ''
    lab.result_pdf_password_enabled = False
    lab.notification_enable_email = False
    lab.save()
    return lab


@pytest.fixture()
def fresh_lab():
    return _reset_lab_settings()


def _task(body, key):
    return next(t for t in body['tasks'] if t['key'] == key)


# ---------------------------------------------------------------------------
# Top-level shape + role gating
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSetupProgressShape:

    def test_top_level_keys(self, fresh_lab, lab_admin):
        body = _get(lab_admin)
        assert set(body.keys()) == {
            'percentage', 'completed_count', 'total_count', 'tasks',
            'next_step',
        }

    def test_task_shape(self, fresh_lab, lab_admin):
        for t in _get(lab_admin)['tasks']:
            assert {
                'key', 'label', 'description', 'completed', 'required', 'href',
            } <= set(t.keys())

    def test_total_count_matches_tasks(self, fresh_lab, lab_admin):
        body = _get(lab_admin)
        assert body['total_count'] == len(body['tasks'])


@pytest.mark.django_db(transaction=True)
class TestRoleGating:

    def test_non_lab_admin_gets_null(self, fresh_lab, technician):
        assert _get(technician) is None

    def test_receptionist_gets_null(self, fresh_lab, receptionist):
        assert _get(receptionist) is None

    def test_lab_admin_gets_payload(self, fresh_lab, lab_admin):
        assert _get(lab_admin) is not None


# ---------------------------------------------------------------------------
# Per-task completion logic — each derived from real DB state
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestTaskCompletion:

    def test_lab_profile_starts_incomplete(self, fresh_lab, lab_admin):
        assert _task(_get(lab_admin), 'lab_profile')['completed'] is False

    def test_lab_profile_completes_when_filled(self, fresh_lab, lab_admin):
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.address = '12 rue de la Paix'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.save()
        assert _task(_get(lab_admin), 'lab_profile')['completed'] is True

    def test_lab_profile_needs_address_too(self, fresh_lab, lab_admin):
        # Name + email without address → still incomplete
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.save()
        assert _task(_get(lab_admin), 'lab_profile')['completed'] is False

    def test_lab_logo_completes_with_file_key(self, fresh_lab, lab_admin):
        fresh_lab.logo_file_key = 'logos/abcd.png'
        fresh_lab.save()
        assert _task(_get(lab_admin), 'lab_logo')['completed'] is True

    def test_lab_logo_completes_with_url(self, fresh_lab, lab_admin):
        fresh_lab.logo_url = 'https://example.com/logo.png'
        fresh_lab.save()
        assert _task(_get(lab_admin), 'lab_logo')['completed'] is True

    def test_pdf_settings_completes_with_legal_footer(self, fresh_lab, lab_admin):
        fresh_lab.legal_footer = 'Confidential — for the addressee only.'
        fresh_lab.save()
        assert _task(_get(lab_admin), 'pdf_settings')['completed'] is True

    def test_pdf_settings_completes_with_password_enabled(self, fresh_lab, lab_admin):
        fresh_lab.result_pdf_password_enabled = True
        fresh_lab.save()
        assert _task(_get(lab_admin), 'pdf_settings')['completed'] is True

    def test_catalog_exams_completes_when_active_exam_exists(
        self, fresh_lab, lab_admin, default_technique,
    ):
        cat = ExamCategory.objects.create(name='Cat', display_order=1)
        fam = ExamFamily.objects.create(name='Fam', display_order=1)
        ExamDefinition.objects.create(
            category=cat, family=fam, technique=default_technique,
            code='SP-CBC', name='SetupCBC',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit_price=Decimal('10'), is_active=True,
        )
        assert _task(_get(lab_admin), 'catalog_exams')['completed'] is True

    def test_catalog_inactive_exam_does_not_count(
        self, fresh_lab, lab_admin, default_technique,
    ):
        cat = ExamCategory.objects.create(name='C2', display_order=1)
        fam = ExamFamily.objects.create(name='F2', display_order=1)
        ExamDefinition.objects.create(
            category=cat, family=fam, technique=default_technique,
            code='SP-INACTIVE', name='Inactive',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit_price=Decimal('10'), is_active=False,
        )
        assert _task(_get(lab_admin), 'catalog_exams')['completed'] is False

    def test_partners_recommended_only(self, fresh_lab, lab_admin):
        # Default state — no partners yet. Task is recommended, not required.
        task = _task(_get(lab_admin), 'partners')
        assert task['required'] is False
        assert task['completed'] is False

    def test_partners_completes_when_one_exists(
        self, fresh_lab, lab_admin, make_request,
    ):
        PartnerOrganizationService.create(
            validated_data={
                'code': 'SP-P1', 'name': 'Setup Partner 1',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=lab_admin, request=make_request(lab_admin),
        )
        assert _task(_get(lab_admin), 'partners')['completed'] is True

    def test_team_users_completes_when_other_active_user_exists(
        self, fresh_lab, lab_admin, technician,
    ):
        # ``technician`` fixture creates another active StaffUser — so the
        # task is already complete by virtue of the fixture wiring.
        assert _task(_get(lab_admin), 'team_users')['completed'] is True

    def test_patient_notifications_follows_lab_setting(
        self, fresh_lab, lab_admin,
    ):
        assert _task(_get(lab_admin), 'patient_notifications')['completed'] is False
        fresh_lab.notification_enable_email = True
        fresh_lab.save()
        assert _task(_get(lab_admin), 'patient_notifications')['completed'] is True


# ---------------------------------------------------------------------------
# Percentage = required-only; recommended cannot block 100%
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPercentage:

    def test_zero_when_nothing_done(self, fresh_lab, lab_admin):
        body = _get(lab_admin)
        assert body['percentage'] == 0

    def test_one_required_completed_moves_percentage(
        self, fresh_lab, lab_admin,
    ):
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.address = '12 rue de la Paix'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.save()
        body = _get(lab_admin)
        # 1 required of 6 = 17% (rounded). The exact denominator can shift
        # if the task list grows; assert the slot moved off zero rather
        # than pinning a specific number.
        assert body['percentage'] > 0
        assert body['completed_count'] >= 1

    def test_recommended_partner_does_not_block_full_completion(
        self, fresh_lab, lab_admin, technician, default_technique,
    ):
        """All required tasks done, partner deliberately skipped → 100%."""
        # lab_profile + logo + pdf + notifications
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.address = '12 rue de la Paix'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.logo_file_key = 'logos/x.png'
        fresh_lab.legal_footer = 'Confidential.'
        fresh_lab.notification_enable_email = True
        fresh_lab.save()
        # catalog_exams
        cat = ExamCategory.objects.create(name='C3', display_order=1)
        fam = ExamFamily.objects.create(name='F3', display_order=1)
        ExamDefinition.objects.create(
            category=cat, family=fam, technique=default_technique,
            code='SP-FULL', name='Full',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit_price=Decimal('10'), is_active=True,
        )
        # team_users — ``technician`` fixture supplies it.
        body = _get(lab_admin)
        assert body['percentage'] == 100, body
        # The recommended partner task is still incomplete.
        assert _task(body, 'partners')['completed'] is False
        # And the all-up counter reflects the unfilled optional task.
        assert body['completed_count'] == body['total_count'] - 1


# ---------------------------------------------------------------------------
# next_step — drives the banner's smart CTA
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestNextStep:

    def test_next_step_shape(self, fresh_lab, lab_admin):
        ns = _get(lab_admin)['next_step']
        assert ns is not None
        assert {'key', 'label', 'url'} <= set(ns.keys())

    def test_next_step_picks_first_incomplete_required(self, fresh_lab, lab_admin):
        # Default fixture state — no required tasks done. The first
        # incomplete required task is ``lab_profile``.
        ns = _get(lab_admin)['next_step']
        assert ns['key'] == 'lab_profile'
        assert ns['label'] == 'Complete profile'
        assert ns['url'] == '/settings/laboratory'

    def test_next_step_skips_completed_required(self, fresh_lab, lab_admin):
        # Tick lab_profile + lab_logo → next required is pdf_settings.
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.address = '12 rue de la Paix'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.logo_file_key = 'logos/x.png'
        fresh_lab.save()
        ns = _get(lab_admin)['next_step']
        assert ns['key'] == 'pdf_settings'
        assert ns['label'] == 'Configure PDF'

    def test_next_step_falls_back_to_recommended_when_required_done(
        self, fresh_lab, lab_admin, technician, default_technique,
    ):
        """Once all required tasks are green, next_step points at the
        first incomplete recommended task — the partner step in the
        default checklist."""
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.address = '12 rue de la Paix'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.logo_file_key = 'logos/x.png'
        fresh_lab.legal_footer = 'Confidential.'
        fresh_lab.notification_enable_email = True
        fresh_lab.save()
        cat = ExamCategory.objects.create(name='CN', display_order=1)
        fam = ExamFamily.objects.create(name='FN', display_order=1)
        ExamDefinition.objects.create(
            category=cat, family=fam, technique=default_technique,
            code='NEXT-FULL', name='Full',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit_price=Decimal('10'), is_active=True,
        )
        ns = _get(lab_admin)['next_step']
        assert ns is not None
        assert ns['key'] == 'partners'
        assert ns['label'] == 'Add partners'
        # Required completion is 100% even though next_step is set.
        assert _get(lab_admin)['percentage'] == 100

    def test_next_step_is_null_when_everything_done(
        self, fresh_lab, lab_admin, technician, default_technique, make_request,
    ):
        from apps.partners.services import PartnerOrganizationService
        from apps.partners.models import OrganizationType
        fresh_lab.lab_name = 'Cytova Test Lab'
        fresh_lab.address = '12 rue de la Paix'
        fresh_lab.email = 'lab@example.com'
        fresh_lab.logo_file_key = 'logos/x.png'
        fresh_lab.legal_footer = 'Confidential.'
        fresh_lab.notification_enable_email = True
        fresh_lab.save()
        cat = ExamCategory.objects.create(name='CD', display_order=1)
        fam = ExamFamily.objects.create(name='FD', display_order=1)
        ExamDefinition.objects.create(
            category=cat, family=fam, technique=default_technique,
            code='NEXT-DONE', name='Done',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit_price=Decimal('10'), is_active=True,
        )
        PartnerOrganizationService.create(
            validated_data={
                'code': 'NEXT-P', 'name': 'Done Partner',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=lab_admin, request=make_request(lab_admin),
        )
        body = _get(lab_admin)
        assert body['next_step'] is None
        assert body['percentage'] == 100
        assert body['completed_count'] == body['total_count']
