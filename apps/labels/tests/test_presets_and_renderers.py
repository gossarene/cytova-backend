"""
Tests for the public-schema label printing preset model and the
layout renderer engine.

Covers:
- System presets (A4 + thermal) are seeded by data migration
- Preset ``to_effective_config`` returns the expected keys
- A4 renderer produces multi-label pages and paginates
- Thermal renderer produces one page per label sized to label + gap
- Defaults dict helpers are stable
"""
import io
import pytest
from django_tenants.utils import get_public_schema_name, schema_context

from apps.labels.defaults import (
    A4_DEFAULTS, DEFAULTS_BY_MODE, THERMAL_DEFAULTS, get_defaults,
)
from apps.labels.models import LabelPrintMode, LabelPrintPreset
from apps.labels.renderers import (
    A4SheetRenderer, LabelLayoutConfig, LabelPayload, ThermalRollRenderer,
    render_labels,
)


pytestmark = pytest.mark.django_db


@pytest.fixture()
def in_public(_test_tenant_schema):
    with schema_context(get_public_schema_name()):
        yield


@pytest.fixture()
def a4_config():
    return LabelLayoutConfig(print_mode='A4_SHEET', **A4_DEFAULTS)


@pytest.fixture()
def thermal_config():
    return LabelLayoutConfig(print_mode='THERMAL_ROLL', **THERMAL_DEFAULTS)


def _payloads(n):
    return [
        LabelPayload(
            numeric_code=f'0001240100000{i}',
            patient_name='Jane Doe',
            patient_dob='1980-01-01',
            collection_date='2026-04-16',
            request_number=f'R-{i:03d}',
            family_name='Hematology' if i % 2 else '',
            label_index=i,
            label_total=n,
        )
        for i in range(1, n + 1)
    ]


class TestSystemPresetsSeeded:

    def test_both_system_presets_exist(self, in_public):
        codes = set(LabelPrintPreset.objects.filter(is_system=True).values_list('code', flat=True))
        assert {'SYS_A4_10_LABELS', 'SYS_THERMAL_40X25'} <= codes

    def test_a4_preset_has_expected_mode(self, in_public):
        p = LabelPrintPreset.objects.get(code='SYS_A4_10_LABELS')
        assert p.print_mode == LabelPrintMode.A4_SHEET
        assert p.is_active

    def test_thermal_preset_has_thermal_gap(self, in_public):
        p = LabelPrintPreset.objects.get(code='SYS_THERMAL_40X25')
        assert p.print_mode == LabelPrintMode.THERMAL_ROLL
        assert p.thermal_gap_mm > 0

    def test_to_effective_config_keys(self, in_public):
        p = LabelPrintPreset.objects.get(code='SYS_A4_10_LABELS')
        cfg = p.to_effective_config()
        required_keys = {
            'print_mode', 'page_width_mm', 'page_height_mm',
            'label_width_mm', 'label_height_mm',
            'margin_top_mm', 'margin_left_mm',
            'horizontal_gap_mm', 'vertical_gap_mm', 'thermal_gap_mm',
            'show_barcode', 'show_numeric_code',
        }
        assert required_keys <= cfg.keys()


class TestDefaultsHelpers:

    def test_defaults_by_mode_covers_both_modes(self):
        assert set(DEFAULTS_BY_MODE.keys()) == {'A4_SHEET', 'THERMAL_ROLL'}

    def test_get_defaults_returns_copy(self):
        d = get_defaults('A4_SHEET')
        d['page_width_mm'] = 999
        assert A4_DEFAULTS['page_width_mm'] != 999

    def test_get_defaults_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            get_defaults('BAD_MODE')


class TestA4SheetRenderer:

    def test_produces_pdf_magic_bytes(self, a4_config):
        pdf = A4SheetRenderer.render(_payloads(1), a4_config)
        assert pdf.startswith(b'%PDF-')

    def test_paginates_when_labels_exceed_grid(self, a4_config):
        # A4 default grid is 2 cols × 5 rows = 10 slots per page
        pdf = A4SheetRenderer.render(_payloads(11), a4_config)
        # A PDF with 2 pages contains two "/Type /Page" objects
        page_count = pdf.count(b'/Type /Page ') + pdf.count(b'/Type /Page\n')
        assert page_count >= 2


class TestThermalRollRenderer:

    def test_produces_pdf_magic_bytes(self, thermal_config):
        pdf = ThermalRollRenderer.render(_payloads(1), thermal_config)
        assert pdf.startswith(b'%PDF-')

    def test_one_page_per_label(self, thermal_config):
        pdf = ThermalRollRenderer.render(_payloads(3), thermal_config)
        page_count = pdf.count(b'/Type /Page ') + pdf.count(b'/Type /Page\n')
        assert page_count >= 3


class TestRenderDispatcher:

    def test_dispatches_by_mode(self, a4_config, thermal_config):
        a = render_labels(_payloads(1), a4_config)
        t = render_labels(_payloads(1), thermal_config)
        assert a.startswith(b'%PDF-')
        assert t.startswith(b'%PDF-')

    def test_unknown_mode_raises(self, a4_config):
        bad = LabelLayoutConfig(
            **{**a4_config.__dict__, 'print_mode': 'NOT_REAL'}
        )
        with pytest.raises(ValueError):
            render_labels(_payloads(1), bad)
