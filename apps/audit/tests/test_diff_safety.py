"""
Tests that AuditLog.diff is always JSON-safe after save.

Covers:
- Decimal values in diff are stored as strings
- UUID values in diff are stored as strings
- datetime/date values in diff are stored as ISO 8601 strings
- Nested structures are recursively sanitized
- None diff is left as None
"""
import json
import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from apps.audit.models import AuditLog, AuditAction, ActorType


class TestAuditLogDiffSafety:

    def _create_log(self, diff):
        return AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=uuid4(),
            actor_email='test@example.com',
            action=AuditAction.UPDATE,
            entity_type='TestEntity',
            entity_id=uuid4(),
            diff=diff,
        )

    def test_decimal_in_diff(self):
        log = self._create_log({
            'before': {'price': Decimal('50.0000')},
            'after': {'price': Decimal('75.0000')},
        })
        log.refresh_from_db()
        assert log.diff['before']['price'] == '50.0000'
        assert log.diff['after']['price'] == '75.0000'

    def test_uuid_in_diff(self):
        uid = uuid4()
        log = self._create_log({'after': {'patient_id': uid}})
        log.refresh_from_db()
        assert log.diff['after']['patient_id'] == str(uid)

    def test_datetime_in_diff(self):
        dt = datetime.datetime(2026, 3, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)
        log = self._create_log({'after': {'confirmed_at': dt}})
        log.refresh_from_db()
        assert '2026-03-01' in log.diff['after']['confirmed_at']

    def test_date_in_diff(self):
        d = datetime.date(2026, 3, 1)
        log = self._create_log({'after': {'effective_date': d}})
        log.refresh_from_db()
        assert log.diff['after']['effective_date'] == '2026-03-01'

    def test_none_diff_left_as_none(self):
        log = self._create_log(None)
        log.refresh_from_db()
        assert log.diff is None

    def test_nested_structure(self):
        log = self._create_log({
            'after': {
                'items': [
                    {'id': uuid4(), 'price': Decimal('10.0000')},
                    {'id': uuid4(), 'price': Decimal('20.0000')},
                ],
            },
        })
        log.refresh_from_db()
        items = log.diff['after']['items']
        assert isinstance(items[0]['id'], str)
        assert items[0]['price'] == '10.0000'

    def test_diff_is_json_serializable_after_save(self):
        log = self._create_log({
            'before': {'id': uuid4(), 'amount': Decimal('99.99')},
            'after': {'id': uuid4(), 'amount': Decimal('100.00')},
        })
        log.refresh_from_db()
        # Must not raise
        serialized = json.dumps(log.diff)
        assert isinstance(serialized, str)

    def test_mixed_types_in_single_diff(self):
        log = self._create_log({
            'after': {
                'uuid_val': uuid4(),
                'decimal_val': Decimal('42.1234'),
                'date_val': datetime.date(2026, 6, 15),
                'datetime_val': datetime.datetime(2026, 6, 15, 12, 0, tzinfo=datetime.timezone.utc),
                'str_val': 'hello',
                'int_val': 7,
                'bool_val': True,
                'none_val': None,
            },
        })
        log.refresh_from_db()
        after = log.diff['after']
        assert isinstance(after['uuid_val'], str)
        assert after['decimal_val'] == '42.1234'
        assert after['date_val'] == '2026-06-15'
        assert '2026-06-15' in after['datetime_val']
        assert after['str_val'] == 'hello'
        assert after['int_val'] == 7
        assert after['bool_val'] is True
        assert after['none_val'] is None
