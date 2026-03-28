"""
Tests for slug validation edge cases.
"""
import pytest

from apps.tenants.onboarding_serializers import LaboratorySignupSerializer, RESERVED_SLUGS


@pytest.fixture(autouse=True)
def _in_tenant_schema():
    yield


def _make_data(slug=None, name='Test Lab'):
    data = {
        'laboratory_name': name,
        'admin_email': 'a@b.com',
        'admin_first_name': 'A',
        'admin_last_name': 'B',
        'admin_password': 'Str0ng!Pass#2026',
    }
    if slug is not None:
        data['slug'] = slug
    return data


@pytest.mark.django_db(transaction=True)
class TestSlugEdgeCases:

    def test_leading_hyphen_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='-bad-slug'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_trailing_hyphen_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='bad-slug-'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_numeric_only_rejected(self):
        """Slug must start with a letter."""
        s = LaboratorySignupSerializer(data=_make_data(slug='12345'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_single_char_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='a'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_two_chars_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='ab'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_three_chars_valid(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='abc'))
        assert s.is_valid(), s.errors

    def test_uppercase_normalized(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='MyLab'))
        assert s.is_valid(), s.errors
        assert s.validated_data['slug'] == 'mylab'

    def test_max_length_63_valid(self):
        slug = 'a' + 'b' * 61 + 'c'  # 63 chars
        s = LaboratorySignupSerializer(data=_make_data(slug=slug))
        assert s.is_valid(), s.errors

    def test_over_63_rejected(self):
        slug = 'a' * 64
        s = LaboratorySignupSerializer(data=_make_data(slug=slug))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_underscores_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='my_lab'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_spaces_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='my lab'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_dots_rejected(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='my.lab'))
        assert not s.is_valid()
        assert 'slug' in s.errors

    def test_valid_with_numbers(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='lab2026'))
        assert s.is_valid(), s.errors

    def test_valid_with_hyphens(self):
        s = LaboratorySignupSerializer(data=_make_data(slug='my-cool-lab'))
        assert s.is_valid(), s.errors

    def test_all_reserved_slugs_rejected(self):
        for slug in list(RESERVED_SLUGS)[:5]:  # spot-check 5
            s = LaboratorySignupSerializer(data=_make_data(slug=slug))
            assert not s.is_valid(), f'Reserved slug "{slug}" was accepted'

    def test_auto_slug_from_unicode_name(self):
        s = LaboratorySignupSerializer(data=_make_data(
            slug=None, name='Lübeck Kliniklabor'
        ))
        assert s.is_valid(), s.errors
        assert s.validated_data['slug'] == 'lubeck-kliniklabor'

    def test_auto_slug_from_name_with_special_chars(self):
        s = LaboratorySignupSerializer(data=_make_data(
            slug=None, name="Dr. Müller's Lab (Branch #2)"
        ))
        assert s.is_valid(), s.errors
        slug = s.validated_data['slug']
        assert slug.isascii()
        assert '_' not in slug
        assert ' ' not in slug

    def test_auto_slug_trims_trailing_hyphens(self):
        s = LaboratorySignupSerializer(data=_make_data(
            slug=None, name='Lab ---'
        ))
        assert s.is_valid(), s.errors
        assert not s.validated_data['slug'].endswith('-')
