"""
Tests for common.utils.serialization.json_safe.

Covers:
- Primitive passthrough (str, int, float, bool, None)
- Decimal → str
- UUID → str
- datetime → ISO 8601 str
- date → ISO 8601 str
- Enum → value
- set → sorted list
- Nested dict/list structures
- Unknown object → str fallback
"""
import datetime
import enum
import json
from decimal import Decimal
from uuid import UUID, uuid4

from common.utils.serialization import json_safe


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

class TestPrimitives:

    def test_none(self):
        assert json_safe(None) is None

    def test_bool_true(self):
        assert json_safe(True) is True

    def test_bool_false(self):
        assert json_safe(False) is False

    def test_int(self):
        assert json_safe(42) == 42

    def test_float(self):
        assert json_safe(3.14) == 3.14

    def test_str(self):
        assert json_safe('hello') == 'hello'

    def test_empty_string(self):
        assert json_safe('') == ''


# ---------------------------------------------------------------------------
# Decimal
# ---------------------------------------------------------------------------

class TestDecimal:

    def test_decimal_to_str(self):
        assert json_safe(Decimal('142.5000')) == '142.5000'

    def test_decimal_zero(self):
        assert json_safe(Decimal('0')) == '0'

    def test_decimal_preserves_precision(self):
        result = json_safe(Decimal('99.1234'))
        assert result == '99.1234'
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# UUID
# ---------------------------------------------------------------------------

class TestUUID:

    def test_uuid_to_str(self):
        uid = UUID('a1b2c3d4-e5f6-7890-abcd-ef1234567890')
        result = json_safe(uid)
        assert result == 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
        assert isinstance(result, str)

    def test_uuid4(self):
        uid = uuid4()
        result = json_safe(uid)
        assert isinstance(result, str)
        assert UUID(result) == uid


# ---------------------------------------------------------------------------
# datetime / date
# ---------------------------------------------------------------------------

class TestDatetime:

    def test_datetime_to_iso(self):
        dt = datetime.datetime(2026, 3, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)
        result = json_safe(dt)
        assert '2026-03-01' in result
        assert isinstance(result, str)

    def test_naive_datetime(self):
        dt = datetime.datetime(2026, 3, 1, 10, 0, 0)
        result = json_safe(dt)
        assert result == '2026-03-01T10:00:00'

    def test_date_to_iso(self):
        d = datetime.date(2026, 3, 1)
        result = json_safe(d)
        assert result == '2026-03-01'
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

class TestEnum:

    def test_enum_value(self):
        class Color(enum.Enum):
            RED = 'RED'
            BLUE = 'BLUE'

        assert json_safe(Color.RED) == 'RED'

    def test_text_choices_style(self):
        class Status(str, enum.Enum):
            DRAFT = 'DRAFT'
            CONFIRMED = 'CONFIRMED'

        assert json_safe(Status.DRAFT) == 'DRAFT'


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

class TestCollections:

    def test_dict_passthrough(self):
        data = {'name': 'Alice', 'age': 30}
        assert json_safe(data) == {'name': 'Alice', 'age': 30}

    def test_list_passthrough(self):
        assert json_safe([1, 'two', 3]) == [1, 'two', 3]

    def test_tuple_to_list(self):
        assert json_safe((1, 2, 3)) == [1, 2, 3]

    def test_set_to_sorted_list(self):
        result = json_safe({3, 1, 2})
        assert result == [1, 2, 3]

    def test_empty_dict(self):
        assert json_safe({}) == {}

    def test_empty_list(self):
        assert json_safe([]) == []


# ---------------------------------------------------------------------------
# Nested structures
# ---------------------------------------------------------------------------

class TestNested:

    def test_dict_with_mixed_types(self):
        data = {
            'id': UUID('a1b2c3d4-e5f6-7890-abcd-ef1234567890'),
            'price': Decimal('50.0000'),
            'created_at': datetime.date(2026, 3, 1),
            'active': True,
            'name': 'Test',
        }
        result = json_safe(data)
        assert result == {
            'id': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'price': '50.0000',
            'created_at': '2026-03-01',
            'active': True,
            'name': 'Test',
        }

    def test_nested_dict(self):
        data = {
            'before': {'price': Decimal('10.0000')},
            'after': {'price': Decimal('20.0000')},
        }
        result = json_safe(data)
        assert result == {
            'before': {'price': '10.0000'},
            'after': {'price': '20.0000'},
        }

    def test_list_of_dicts(self):
        data = [
            {'id': uuid4(), 'value': Decimal('1.5')},
            {'id': uuid4(), 'value': Decimal('2.5')},
        ]
        result = json_safe(data)
        assert len(result) == 2
        assert isinstance(result[0]['id'], str)
        assert isinstance(result[0]['value'], str)

    def test_deeply_nested(self):
        data = {'a': {'b': {'c': Decimal('99.99')}}}
        assert json_safe(data) == {'a': {'b': {'c': '99.99'}}}


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

class TestFallback:

    def test_unknown_object_to_str(self):
        class Custom:
            def __str__(self):
                return 'custom-repr'

        result = json_safe(Custom())
        assert result == 'custom-repr'
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Round-trip: json.dumps must not raise
# ---------------------------------------------------------------------------

class TestJsonDumps:

    def test_complex_structure_is_json_serializable(self):
        data = {
            'id': uuid4(),
            'prices': [Decimal('10.0000'), Decimal('20.0000')],
            'meta': {
                'created': datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                'date': datetime.date(2026, 1, 1),
                'tags': {'a', 'b', 'c'},
            },
            'active': True,
            'count': 42,
            'ratio': 3.14,
            'label': None,
        }
        result = json_safe(data)
        # Must not raise
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

        # Round-trip
        parsed = json.loads(serialized)
        assert parsed['active'] is True
        assert parsed['count'] == 42
        assert isinstance(parsed['prices'], list)
        assert parsed['prices'][0] == '10.0000'
