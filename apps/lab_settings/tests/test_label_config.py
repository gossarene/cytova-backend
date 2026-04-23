"""
Tests for the label-printing configuration on LabSettings.

Covers:
- Applying a preset copies every layout field into the effective config
- After a preset is applied, the preset row itself could be edited and
  the lab's effective values would be untouched (frozen snapshot)
- The PATCH endpoint copies preset values on select
- The label-defaults endpoint returns sensible per-mode defaults
- The label-presets endpoint lists active presets
- ``to_label_layout_config`` returns the renderer config dataclass
"""
import pytest
from rest_framework.test import APIClient

from apps.labels.models import LabelPrintPreset
from apps.lab_settings.models import LabSettings


pytestmark = [pytest.mark.django_db, pytest.mark.no_auto_labels]


API = '/api/v1/lab-settings'


@pytest.fixture()
def client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


@pytest.fixture()
def a4_preset(db, _test_tenant_schema):
    from django_tenants.utils import get_public_schema_name, schema_context
    with schema_context(get_public_schema_name()):
        return LabelPrintPreset.objects.get(code='SYS_A4_10_LABELS')


@pytest.fixture()
def thermal_preset(db, _test_tenant_schema):
    from django_tenants.utils import get_public_schema_name, schema_context
    with schema_context(get_public_schema_name()):
        return LabelPrintPreset.objects.get(code='SYS_THERMAL_40X25')


class TestApplyPreset:

    def test_apply_preset_copies_all_values(self, thermal_preset):
        settings = LabSettings.get_solo()
        settings.apply_preset(thermal_preset)
        settings.save()

        assert settings.label_print_mode == 'THERMAL_ROLL'
        assert settings.label_label_width_mm == thermal_preset.label_width_mm
        assert settings.label_thermal_gap_mm == thermal_preset.thermal_gap_mm
        assert settings.label_preset_id == thermal_preset.id

    def test_preset_edit_does_not_change_effective_config(self, thermal_preset):
        settings = LabSettings.get_solo()
        settings.apply_preset(thermal_preset)
        settings.save()

        original = settings.label_label_width_mm

        # A platform admin mutates the preset later
        from django_tenants.utils import get_public_schema_name, schema_context
        with schema_context(get_public_schema_name()):
            thermal_preset.label_width_mm = 999
            thermal_preset.save()

        settings.refresh_from_db()
        # Lab's effective value is untouched — preset is only an audit ref
        assert settings.label_label_width_mm == original


class TestLayoutConfig:

    def test_returns_matching_dataclass(self):
        settings = LabSettings.get_solo()
        cfg = settings.to_label_layout_config()
        assert cfg.print_mode == settings.label_print_mode
        assert cfg.label_width_mm == settings.label_label_width_mm


class TestLabelDefaultsEndpoint:

    def test_returns_defaults_for_requested_mode(self, client):
        r = client.get(f'{API}/label-defaults/?mode=A4_SHEET')
        assert r.status_code == 200
        body = r.json()
        assert body['mode'] == 'A4_SHEET'
        assert body['defaults']['page_width_mm'] == 210

    def test_returns_all_modes_when_no_query(self, client):
        r = client.get(f'{API}/label-defaults/')
        assert r.status_code == 200
        body = r.json()
        assert 'A4_SHEET' in body['defaults']
        assert 'THERMAL_ROLL' in body['defaults']

    def test_unknown_mode_is_400(self, client):
        r = client.get(f'{API}/label-defaults/?mode=NOT_REAL')
        assert r.status_code == 400


class TestLabelPresetsEndpoint:

    def test_lists_active_system_presets(self, client):
        r = client.get(f'{API}/label-presets/')
        assert r.status_code == 200
        codes = [p['code'] for p in r.json()['results']]
        assert 'SYS_A4_10_LABELS' in codes
        assert 'SYS_THERMAL_40X25' in codes


class TestPatchCopiesPreset:

    def test_patch_with_preset_copies_values(self, client, thermal_preset):
        r = client.patch(
            f'{API}/',
            data={'label_preset': str(thermal_preset.id)},
            format='json',
        )
        assert r.status_code == 200, r.content
        body = r.json()
        assert body['label_print_mode'] == 'THERMAL_ROLL'
        assert body['label_label_width_mm'] == thermal_preset.label_width_mm
        assert body['label_thermal_gap_mm'] == thermal_preset.thermal_gap_mm
