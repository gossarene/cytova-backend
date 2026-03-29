"""
Cytova — Catalog Service

All catalog write operations that carry business logic live here.
Views are kept thin; they validate input and delegate to the service.
Audit logging covers create/update/deactivate for categories and exam definitions.
Pricing rules are audited on creation/update/deactivation.
"""
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.users.models import StaffUser
from .models import ExamCategory, ExamDefinition, LabExamSettings, PricingRule, PricingType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExamCategory
# ---------------------------------------------------------------------------

class ExamCategoryService:

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> ExamCategory:
        category = ExamCategory(**validated_data)
        category.save()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='ExamCategory',
            entity_id=category.id,
            diff={'after': {'name': category.name}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return category

    @staticmethod
    def update(category: ExamCategory, validated_data: dict, updated_by: StaffUser, request) -> ExamCategory:
        before = {k: getattr(category, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(category, field, value)
        category.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(category, k) for k in validated_data}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=AuditAction.UPDATE,
            entity_type='ExamCategory',
            entity_id=category.id,
            diff={'before': before, 'after': after},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return category

    @staticmethod
    def deactivate(category: ExamCategory, deactivated_by: StaffUser, request) -> ExamCategory:
        if not category.is_active:
            return category

        category.is_active = False
        category.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deactivated_by.id,
            actor_email=deactivated_by.email,
            action=AuditAction.DEACTIVATE,
            entity_type='ExamCategory',
            entity_id=category.id,
            diff={'after': {'is_active': False}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return category


# ---------------------------------------------------------------------------
# ExamDefinition
# ---------------------------------------------------------------------------

class ExamDefinitionService:

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> ExamDefinition:
        exam = ExamDefinition(**validated_data)
        exam.save()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='ExamDefinition',
            entity_id=exam.id,
            diff={'after': {'code': exam.code, 'name': exam.name}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return exam

    @staticmethod
    def update(exam: ExamDefinition, validated_data: dict, updated_by: StaffUser, request) -> ExamDefinition:
        # Resolve category_id → category FK if provided
        category_id = validated_data.pop('category_id', None)
        if category_id is not None:
            validated_data['category_id'] = category_id

        before = {k: getattr(exam, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(exam, field, value)
        exam.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(exam, k) for k in validated_data}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=AuditAction.UPDATE,
            entity_type='ExamDefinition',
            entity_id=exam.id,
            diff={'before': before, 'after': after},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return exam

    @staticmethod
    def deactivate(exam: ExamDefinition, deactivated_by: StaffUser, request) -> ExamDefinition:
        if not exam.is_active:
            return exam

        exam.is_active = False
        exam.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deactivated_by.id,
            actor_email=deactivated_by.email,
            action=AuditAction.DEACTIVATE,
            entity_type='ExamDefinition',
            entity_id=exam.id,
            diff={'after': {'is_active': False}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return exam

    @staticmethod
    def upsert_lab_settings(
        exam: ExamDefinition,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> LabExamSettings:
        """
        PUT semantics: create or fully replace the lab settings for this exam.
        Returns the settings instance.
        """
        settings, created = LabExamSettings.objects.update_or_create(
            exam_definition=exam,
            defaults={**validated_data, 'updated_by': updated_by},
        )

        action = AuditAction.CREATE if created else AuditAction.UPDATE
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=action,
            entity_type='LabExamSettings',
            entity_id=settings.id,
            diff={'after': validated_data},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return settings


# ---------------------------------------------------------------------------
# PricingRule
# ---------------------------------------------------------------------------

class PricingRuleService:

    @staticmethod
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> PricingRule:
        rule = PricingRule(created_by=created_by, **validated_data)
        rule.full_clean()
        rule.save()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='PricingRule',
            entity_id=rule.id,
            diff={'after': {
                'exam_code': rule.exam_definition.code,
                'pricing_type': rule.pricing_type,
                'value': str(rule.value),
                'partner_organization_id': str(rule.partner_organization_id) if rule.partner_organization_id else None,
                'source_type': rule.source_type or None,
                'priority': rule.priority,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return rule

    @staticmethod
    def update(
        rule: PricingRule,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> PricingRule:
        if not validated_data:
            return rule

        before = {}
        for field in validated_data:
            before[field] = getattr(rule, field)

        for field, value in validated_data.items():
            setattr(rule, field, value)
        rule.full_clean()
        rule.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(rule, k) for k in validated_data}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=AuditAction.UPDATE,
            entity_type='PricingRule',
            entity_id=rule.id,
            diff={
                'before': {k: str(v) if v is not None else None for k, v in before.items()},
                'after': {k: str(v) if v is not None else None for k, v in after.items()},
            },
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return rule

    @staticmethod
    def deactivate(rule: PricingRule, deactivated_by: StaffUser, request) -> PricingRule:
        if not rule.is_active:
            return rule

        rule.is_active = False
        rule.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deactivated_by.id,
            actor_email=deactivated_by.email,
            action=AuditAction.DEACTIVATE,
            entity_type='PricingRule',
            entity_id=rule.id,
            diff={'after': {'is_active': False}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return rule


# ---------------------------------------------------------------------------
# PricingResolver — deterministic rule resolution
# ---------------------------------------------------------------------------

class PricingResolver:
    """
    Resolves the applicable pricing rule for an exam in a given context.

    Resolution order (most specific first):
        1. exam + partner_organization
        2. exam + source_type
        3. exam only (no partner, no source_type)

    Within the same specificity level: highest ``priority``, then most recently
    created. Returns None if no rule matches — caller falls back to the exam's
    ``unit_price``.
    """

    @staticmethod
    def _active_rules_qs(exam_definition):
        """Base queryset: active rules for this exam within their date window."""
        today = date.today()
        return (
            PricingRule.objects
            .filter(exam_definition=exam_definition, is_active=True)
            .filter(Q(start_date__isnull=True) | Q(start_date__lte=today))
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        )

    @staticmethod
    def resolve(exam_definition, partner_organization=None, source_type=''):
        """
        Find the best matching pricing rule for the given context.

        Returns the PricingRule instance or None.
        """
        base_qs = PricingResolver._active_rules_qs(exam_definition)
        ordering = ['-priority', '-created_at']

        # Level 1: exam + partner_organization (most specific)
        if partner_organization is not None:
            rule = (
                base_qs
                .filter(partner_organization=partner_organization)
                .order_by(*ordering)
                .first()
            )
            if rule:
                return rule

        # Level 2: exam + source_type (no partner)
        if source_type:
            rule = (
                base_qs
                .filter(partner_organization__isnull=True, source_type=source_type)
                .order_by(*ordering)
                .first()
            )
            if rule:
                return rule

        # Level 3: exam only (broadest)
        rule = (
            base_qs
            .filter(partner_organization__isnull=True, source_type='')
            .order_by(*ordering)
            .first()
        )
        return rule

    @staticmethod
    def compute_billed_price(rule, unit_price):
        """
        Compute the billed price from a rule and the exam's unit_price.

        Returns a Decimal rounded to 4 decimal places.
        """
        if rule.pricing_type == PricingType.FIXED_PRICE:
            return rule.value

        if rule.pricing_type == PricingType.PERCENTAGE_DISCOUNT:
            discount = unit_price * rule.value / Decimal('100')
            return (unit_price - discount).quantize(
                Decimal('0.0001'), rounding=ROUND_HALF_UP,
            )

        # Unknown type — no adjustment
        return unit_price
