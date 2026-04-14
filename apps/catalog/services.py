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
from .models import (
    ExamCategory, ExamFamily, ExamSubFamily, TubeType, ExamTechnique,
    ExamDefinition, LabExamSettings, PricingRule, PricingType,
)

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
# Reference-data services (ExamFamily, ExamSubFamily, TubeType, ExamTechnique)
#
# These mirror ExamCategoryService's shape: create / update / deactivate, each
# writing an AuditLog entry. Keeping all four in the same module avoids
# scattering catalog write-paths across files and keeps audit semantics
# uniform — the ``entity_type`` column in AuditLog is the only thing that
# changes between them.
# ---------------------------------------------------------------------------


def _audit(actor, action, entity_type, entity_id, diff, request):
    """Small helper so every reference service writes audit logs identically."""
    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        diff=diff,
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', ''),
    )


class ExamFamilyService:
    ENTITY = 'ExamFamily'

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> ExamFamily:
        family = ExamFamily(**validated_data)
        family.save()
        _audit(
            created_by, AuditAction.CREATE, ExamFamilyService.ENTITY, family.id,
            {'after': {'name': family.name}}, request,
        )
        return family

    @staticmethod
    def update(family: ExamFamily, validated_data: dict, updated_by: StaffUser, request) -> ExamFamily:
        if not validated_data:
            return family
        before = {k: getattr(family, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(family, field, value)
        family.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(family, k) for k in validated_data}
        _audit(
            updated_by, AuditAction.UPDATE, ExamFamilyService.ENTITY, family.id,
            {'before': before, 'after': after}, request,
        )
        return family

    @staticmethod
    def deactivate(family: ExamFamily, deactivated_by: StaffUser, request) -> ExamFamily:
        if not family.is_active:
            return family
        family.is_active = False
        family.save(update_fields=['is_active', 'updated_at'])
        _audit(
            deactivated_by, AuditAction.DEACTIVATE, ExamFamilyService.ENTITY, family.id,
            {'after': {'is_active': False}}, request,
        )
        return family

    @staticmethod
    def reactivate(family: ExamFamily, reactivated_by: StaffUser, request) -> ExamFamily:
        # Idempotent: already-active → return silently without writing a
        # duplicate audit event. Only the actual False → True transition is
        # recorded, which keeps the audit trail meaningful for lifecycle
        # queries (``action=REACTIVATE`` counts are real transitions, not
        # no-op clicks).
        if family.is_active:
            return family
        family.is_active = True
        family.save(update_fields=['is_active', 'updated_at'])
        _audit(
            reactivated_by, AuditAction.REACTIVATE, ExamFamilyService.ENTITY, family.id,
            {'before': {'is_active': False}, 'after': {'is_active': True}}, request,
        )
        return family


class ExamSubFamilyService:
    ENTITY = 'ExamSubFamily'

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> ExamSubFamily:
        sub = ExamSubFamily(**validated_data)
        sub.save()
        _audit(
            created_by, AuditAction.CREATE, ExamSubFamilyService.ENTITY, sub.id,
            {'after': {'family_id': str(sub.family_id), 'name': sub.name}}, request,
        )
        return sub

    @staticmethod
    def update(sub: ExamSubFamily, validated_data: dict, updated_by: StaffUser, request) -> ExamSubFamily:
        if not validated_data:
            return sub
        before = {k: getattr(sub, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(sub, field, value)
        sub.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(sub, k) for k in validated_data}
        _audit(
            updated_by, AuditAction.UPDATE, ExamSubFamilyService.ENTITY, sub.id,
            {'before': before, 'after': after}, request,
        )
        return sub

    @staticmethod
    def deactivate(sub: ExamSubFamily, deactivated_by: StaffUser, request) -> ExamSubFamily:
        if not sub.is_active:
            return sub
        sub.is_active = False
        sub.save(update_fields=['is_active', 'updated_at'])
        _audit(
            deactivated_by, AuditAction.DEACTIVATE, ExamSubFamilyService.ENTITY, sub.id,
            {'after': {'is_active': False}}, request,
        )
        return sub

    @staticmethod
    def reactivate(sub: ExamSubFamily, reactivated_by: StaffUser, request) -> ExamSubFamily:
        # Reactivating a sub-family whose parent family is inactive would
        # create a zombie: the record would show as active but no new exam
        # could reference it (``ExamSubFamilyCreateSerializer.validate_family_id``
        # already rejects inactive parents). We reject here so the catalog
        # stays internally consistent and the UI gives the admin an actionable
        # error instead of a silent half-broken row.
        if sub.is_active:
            return sub
        if not sub.family.is_active:
            raise ValidationError({
                'family_id': (
                    'Cannot reactivate a sub-family whose parent family is '
                    'inactive. Reactivate the family first.'
                ),
            })
        sub.is_active = True
        sub.save(update_fields=['is_active', 'updated_at'])
        _audit(
            reactivated_by, AuditAction.REACTIVATE, ExamSubFamilyService.ENTITY, sub.id,
            {'before': {'is_active': False}, 'after': {'is_active': True}}, request,
        )
        return sub


class TubeTypeService:
    ENTITY = 'TubeType'

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> TubeType:
        tube = TubeType(**validated_data)
        tube.save()
        _audit(
            created_by, AuditAction.CREATE, TubeTypeService.ENTITY, tube.id,
            {'after': {'name': tube.name}}, request,
        )
        return tube

    @staticmethod
    def update(tube: TubeType, validated_data: dict, updated_by: StaffUser, request) -> TubeType:
        if not validated_data:
            return tube
        before = {k: getattr(tube, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(tube, field, value)
        tube.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(tube, k) for k in validated_data}
        _audit(
            updated_by, AuditAction.UPDATE, TubeTypeService.ENTITY, tube.id,
            {'before': before, 'after': after}, request,
        )
        return tube

    @staticmethod
    def deactivate(tube: TubeType, deactivated_by: StaffUser, request) -> TubeType:
        if not tube.is_active:
            return tube
        tube.is_active = False
        tube.save(update_fields=['is_active', 'updated_at'])
        _audit(
            deactivated_by, AuditAction.DEACTIVATE, TubeTypeService.ENTITY, tube.id,
            {'after': {'is_active': False}}, request,
        )
        return tube

    @staticmethod
    def reactivate(tube: TubeType, reactivated_by: StaffUser, request) -> TubeType:
        if tube.is_active:
            return tube
        tube.is_active = True
        tube.save(update_fields=['is_active', 'updated_at'])
        _audit(
            reactivated_by, AuditAction.REACTIVATE, TubeTypeService.ENTITY, tube.id,
            {'before': {'is_active': False}, 'after': {'is_active': True}}, request,
        )
        return tube


class ExamTechniqueService:
    ENTITY = 'ExamTechnique'

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> ExamTechnique:
        tech = ExamTechnique(**validated_data)
        tech.save()
        _audit(
            created_by, AuditAction.CREATE, ExamTechniqueService.ENTITY, tech.id,
            {'after': {'name': tech.name}}, request,
        )
        return tech

    @staticmethod
    def update(tech: ExamTechnique, validated_data: dict, updated_by: StaffUser, request) -> ExamTechnique:
        if not validated_data:
            return tech
        before = {k: getattr(tech, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(tech, field, value)
        tech.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(tech, k) for k in validated_data}
        _audit(
            updated_by, AuditAction.UPDATE, ExamTechniqueService.ENTITY, tech.id,
            {'before': before, 'after': after}, request,
        )
        return tech

    @staticmethod
    def deactivate(tech: ExamTechnique, deactivated_by: StaffUser, request) -> ExamTechnique:
        if not tech.is_active:
            return tech
        tech.is_active = False
        tech.save(update_fields=['is_active', 'updated_at'])
        _audit(
            deactivated_by, AuditAction.DEACTIVATE, ExamTechniqueService.ENTITY, tech.id,
            {'after': {'is_active': False}}, request,
        )
        return tech

    @staticmethod
    def reactivate(tech: ExamTechnique, reactivated_by: StaffUser, request) -> ExamTechnique:
        if tech.is_active:
            return tech
        tech.is_active = True
        tech.save(update_fields=['is_active', 'updated_at'])
        _audit(
            reactivated_by, AuditAction.REACTIVATE, ExamTechniqueService.ENTITY, tech.id,
            {'before': {'is_active': False}, 'after': {'is_active': True}}, request,
        )
        return tech


# ---------------------------------------------------------------------------
# ExamDefinition
# ---------------------------------------------------------------------------

class ExamDefinitionService:

    @staticmethod
    def create(validated_data: dict, created_by: StaffUser, request) -> ExamDefinition:
        from .models import ExamParameter
        params_data = validated_data.pop('parameters', [])
        exam = ExamDefinition(**validated_data)
        exam.save()

        for p in params_data:
            ExamParameter.objects.create(exam_definition=exam, **p)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='ExamDefinition',
            entity_id=exam.id,
            diff={'after': {
                'code': exam.code,
                'name': exam.name,
                'result_structure': exam.result_structure,
                'parameters_count': len(params_data),
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return exam

    @staticmethod
    def update(exam: ExamDefinition, validated_data: dict, updated_by: StaffUser, request) -> ExamDefinition:
        """
        Apply a partial update to an exam definition and write an audit log.

        Historical integrity: existing ``AnalysisRequestItem`` rows snapshot
        ``unit_price`` into their own column at creation time, so changing
        ``ExamDefinition.unit_price`` here never back-propagates into past
        requests. This function therefore does not need to touch any
        request-related model — the guarantee is enforced by the data model.

        Audit: every meaningful write is recorded as one ``UPDATE`` entry
        with a full before/after diff of the fields the caller actually
        changed. Only the keys present in ``validated_data`` are snapshotted,
        so untouched columns are not flagged as "changed" by accident.
        """
        if not validated_data:
            return exam

        # Note on FK clears: when the caller passes e.g. ``sub_family_id: None``
        # that is a legitimate "detach the sub-family" intent. Django's ORM
        # handles ``setattr(exam, 'sub_family_id', None)`` natively, so we
        # iterate ``validated_data`` as-is without any pre-filtering. The
        # previous implementation stripped ``None`` FK values and silently
        # failed to apply legitimate clears.
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
            diff={
                'before': {k: str(v) if v is not None else None for k, v in before.items()},
                'after': {k: str(v) if v is not None else None for k, v in after.items()},
            },
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
# ExamParameter
# ---------------------------------------------------------------------------

class ExamParameterService:

    @staticmethod
    def create(exam: ExamDefinition, validated_data: dict, created_by: StaffUser, request):
        from .models import ExamParameter, ResultStructure
        if exam.result_structure != ResultStructure.MULTI_PARAMETER:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(
                'Parameters can only be added to MULTI_PARAMETER exams.'
            )

        param = ExamParameter(exam_definition=exam, **validated_data)
        param.save()

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=created_by.id,
            actor_email=created_by.email,
            action=AuditAction.CREATE,
            entity_type='ExamParameter',
            entity_id=param.id,
            diff={'after': {'code': param.code, 'name': param.name,
                            'exam_id': str(exam.id)}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )
        return param

    @staticmethod
    def update(param, validated_data: dict, updated_by: StaffUser, request):
        if not validated_data:
            return param

        from .models import ExamParameter
        before = {k: getattr(param, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(param, field, value)
        param.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(param, k) for k in validated_data}

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=updated_by.id,
            actor_email=updated_by.email,
            action=AuditAction.UPDATE,
            entity_type='ExamParameter',
            entity_id=param.id,
            diff={
                'before': {k: str(v) for k, v in before.items()},
                'after': {k: str(v) for k, v in after.items()},
            },
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )
        return param

    @staticmethod
    def deactivate(param, deactivated_by: StaffUser, request):
        if not param.is_active:
            return param
        param.is_active = False
        param.save(update_fields=['is_active', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=deactivated_by.id,
            actor_email=deactivated_by.email,
            action=AuditAction.DEACTIVATE,
            entity_type='ExamParameter',
            entity_id=param.id,
            diff={'after': {'is_active': False}},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )
        return param


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
