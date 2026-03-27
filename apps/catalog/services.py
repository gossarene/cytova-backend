"""
Cytova — Catalog Service

All catalog write operations that carry business logic live here.
Views are kept thin; they validate input and delegate to the service.
Audit logging covers create/update/deactivate for categories and exam definitions.
Pricing rules and lab settings are audited on creation/replacement.
"""
import logging
from datetime import date

from django.db.models import Q
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.users.models import StaffUser
from .models import ExamCategory, ExamDefinition, LabExamSettings, PricingRule

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
    def _check_overlap(exam: ExamDefinition, effective_from: date, effective_to, exclude_id=None) -> None:
        """
        Raise ValidationError if the proposed date range overlaps any existing
        rule for this exam.

        Overlap condition between [A_from, A_to) and [B_from, B_to):
            A_from < B_to_or_inf  AND  B_from < A_to_or_inf
        """
        qs = PricingRule.objects.filter(exam_definition=exam)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)

        for rule in qs:
            rule_end = rule.effective_to
            new_end = effective_to

            a_start, a_end = rule.effective_from, rule_end
            b_start, b_end = effective_from, new_end

            # [a_start, a_end) overlaps [b_start, b_end) if:
            # a_start < b_end (or b_end is open) AND b_start < a_end (or a_end is open)
            a_before_b_end = (b_end is None) or (a_start < b_end)
            b_before_a_end = (a_end is None) or (b_start < a_end)

            if a_before_b_end and b_before_a_end:
                raise ValidationError({
                    'effective_from': (
                        f'Date range overlaps with an existing pricing rule '
                        f'({rule.effective_from} → {rule.effective_to or "open"}).'
                    )
                })

    @staticmethod
    def create(
        exam: ExamDefinition,
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> PricingRule:
        PricingRuleService._check_overlap(
            exam,
            validated_data['effective_from'],
            validated_data.get('effective_to'),
        )

        rule = PricingRule(
            exam_definition=exam,
            created_by=created_by,
            **validated_data,
        )
        rule.save()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='PricingRule',
            entity_id=rule.id,
            diff={'after': {
                'exam_code': exam.code,
                'unit_price': str(rule.unit_price),
                'billed_price': str(rule.billed_price),
                'effective_from': str(rule.effective_from),
                'effective_to': str(rule.effective_to) if rule.effective_to else None,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return rule

    @staticmethod
    def close(rule: PricingRule, effective_to: date, closed_by: StaffUser, request) -> PricingRule:
        """
        Set effective_to to close an open-ended pricing rule.
        The rule is otherwise immutable; this is the single allowed mutation.
        """
        if rule.effective_to is not None:
            raise ValidationError({'effective_to': 'This pricing rule is already closed.'})

        if effective_to <= rule.effective_from:
            raise ValidationError(
                {'effective_to': 'effective_to must be after effective_from.'}
            )

        # Bypass the model-level immutability guard via queryset update
        PricingRule.objects.filter(id=rule.id).update(effective_to=effective_to)
        rule.effective_to = effective_to

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=closed_by.id,
            actor_email=closed_by.email,
            action=AuditAction.UPDATE,
            entity_type='PricingRule',
            entity_id=rule.id,
            diff={'before': {'effective_to': None}, 'after': {'effective_to': str(effective_to)}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return rule
