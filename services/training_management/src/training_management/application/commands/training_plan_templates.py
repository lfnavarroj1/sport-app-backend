"""HU028: publicar y retirar plantillas de plan de entrenamiento (ADR-041).

Mismo patrón que las rutas de HU026:

Publicación (`POST /v1/training-plan-templates`, CA4):
1. Permiso por actor existente `training.plan.author` (solo `service_provider`).
2. Validar la plantilla con las reglas del agregado; si incumple alguna, `validation_error` con
   todos los detalles y nada se publica.
3. Clave de idempotencia por profesional y operación, vigente 24 horas: el reintento devuelve el
   resultado original; la misma clave con otra plantilla es `idempotency_key_reused`.
4. En una transacción: plantilla `PUBLISHED` versión 1, `plantilla_entrenamiento.publicada.v1`
   en la outbox y el resultado idempotente. Si una solicitud concurrente con la misma clave
   confirma primero, se devuelve su resultado.

Retiro (`POST /v1/training-plan-templates/{template_id}/retire`, CA3), con la fila bloqueada:
- inexistente o de otro profesional → `template_not_found`, sin revelar cuál;
- ya retirada → se devuelve tal cual, sin otro evento;
- `expected_version` distinto de la vigente → `version_conflict` con la versión vigente;
- si no, `RETIRED` con la versión siguiente y `plantilla_entrenamiento.retirada.v1`.
Publicar una plantilla no asigna ni inicia ningún plan (HU010).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from training_management.application.access import require_plan_author
from training_management.application.errors import (
    IdempotencyKeyConflict,
    IdempotencyKeyReused,
    PermissionRequired,
)
from training_management.application.ports import (
    Caller,
    Clock,
    CommandRecord,
    OutboxMessage,
    PermissionPolicy,
    TemplateUnitOfWork,
)
from training_management.application.template_documents import template_from_document
from training_management.domain.templates.events import (
    AGGREGATE_TYPE,
    PUBLISHED_EVENT_TYPE,
    RETIRED_EVENT_TYPE,
    SCHEMA_VERSION,
    template_document,
)
from training_management.domain.templates.model import (
    InvalidTemplate,
    TemplateInput,
    TemplateNotFound,
    TemplateStatus,
    TemplateVersionConflict,
    TrainingPlanTemplate,
)

PUBLISH_OPERATION = "publish_training_plan_template"
IDEMPOTENCY_TTL = timedelta(hours=24)
_FINGERPRINT_FIELDS = (
    "professional_id",
    "title",
    "objectives",
    "sport",
    "goal",
    "level",
    "duration_weeks",
    "weekly_sessions",
)

_audit = logging.getLogger("sportapp.audit")


@dataclass(frozen=True)
class PublishTemplateCommand:
    data: TemplateInput
    idempotency_key: UUID
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class RetireTemplateCommand:
    template_id: UUID
    expected_version: int
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class TemplateResult:
    template: TrainingPlanTemplate
    replayed: bool = False


def request_fingerprint(template: TrainingPlanTemplate) -> str:
    """Identifica la solicitud con los datos normalizados (sin identificador ni instantes)."""
    document = template_document(template)
    canonical = json.dumps(
        [PUBLISH_OPERATION, {key: document[key] for key in _FINGERPRINT_FIELDS}],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _outbox(
    template: TrainingPlanTemplate,
    event_type: str,
    event_id: UUID,
    correlation_id: UUID | None,
    causation: str,
) -> OutboxMessage:
    return OutboxMessage(
        event_id=event_id,
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=template.template_id,
        aggregate_version=template.version,
        occurred_at=template.updated_at,
        correlation_id=correlation_id,
        causation_id=causation,
        payload=template_document(template),
    )


def _audit_result(name: str, caller: Caller, result: str, **fields: str) -> None:
    # Solo identificadores y el resultado: ni títulos ni descripciones.
    _audit.info(
        name,
        extra={"fields": {"professional_id": str(caller.user_id), "result": result, **fields}},
    )


class PublishTemplate:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], TemplateUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: PublishTemplateCommand) -> TemplateResult:
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, InvalidTemplate, IdempotencyKeyReused) as error:
            _audit_result("audit.template_published", caller, "rejected", reason=error.code)
            raise
        _audit_result(
            "audit.template_published",
            caller,
            "replayed" if result.replayed else "published",
            template_id=str(result.template.template_id),
        )
        return result

    def _execute(self, caller: Caller, command: PublishTemplateCommand) -> TemplateResult:
        require_plan_author(caller, self._permissions)
        template = TrainingPlanTemplate.publish(
            template_id=self._new_id(),
            professional_id=caller.user_id,
            data=command.data,
            now=self._clock.now(),
        )
        fingerprint = request_fingerprint(template)
        previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
        if previous is not None:
            return previous
        try:
            return self._publish(template, command, fingerprint)
        except IdempotencyKeyConflict:
            previous = self._previous(caller.user_id, command.idempotency_key, fingerprint)
            if previous is None:
                raise
            return previous

    def _previous(
        self, professional_id: UUID, key: UUID, fingerprint: str
    ) -> TemplateResult | None:
        with self._unit_of_work() as unit_of_work:
            record = unit_of_work.find_command(professional_id, PUBLISH_OPERATION, key)
        if record is None or record.expires_at <= self._clock.now():
            return None
        if record.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused()
        return TemplateResult(template=template_from_document(record.response), replayed=True)

    def _publish(
        self, template: TrainingPlanTemplate, command: PublishTemplateCommand, fingerprint: str
    ) -> TemplateResult:
        now = template.created_at
        owner, key = template.professional_id, command.idempotency_key
        with self._unit_of_work() as unit_of_work:
            existing = unit_of_work.find_command(owner, PUBLISH_OPERATION, key)
            if existing is not None:
                if existing.expires_at > now:
                    raise IdempotencyKeyConflict()
                unit_of_work.delete_command(owner, PUBLISH_OPERATION, key)
            unit_of_work.add_template(template)
            unit_of_work.add_outbox(
                _outbox(
                    template, PUBLISHED_EVENT_TYPE, self._new_id(), command.correlation_id, str(key)
                )
            )
            unit_of_work.add_command(
                CommandRecord(
                    actor_id=owner,
                    operation=PUBLISH_OPERATION,
                    key=key,
                    request_fingerprint=fingerprint,
                    resource_id=template.template_id,
                    response=template_document(template),
                    created_at=now,
                    expires_at=now + IDEMPOTENCY_TTL,
                )
            )
            unit_of_work.commit()
        return TemplateResult(template=template)


class RetireTemplate:
    def __init__(
        self,
        *,
        unit_of_work: Callable[[], TemplateUnitOfWork],
        permissions: PermissionPolicy,
        clock: Clock,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._permissions = permissions
        self._clock = clock
        self._new_id = new_id

    def __call__(self, caller: Caller, command: RetireTemplateCommand) -> TemplateResult:
        fields = {"template_id": str(command.template_id)}
        try:
            result = self._execute(caller, command)
        except (PermissionRequired, TemplateNotFound, TemplateVersionConflict) as error:
            _audit_result("audit.template_retired", caller, "rejected", reason=error.code, **fields)
            raise
        _audit_result(
            "audit.template_retired", caller, "replayed" if result.replayed else "retired", **fields
        )
        return result

    def _execute(self, caller: Caller, command: RetireTemplateCommand) -> TemplateResult:
        require_plan_author(caller, self._permissions)
        with self._unit_of_work() as unit_of_work:
            template = unit_of_work.get_template_for_update(command.template_id)
            if template is None or template.professional_id != caller.user_id:
                raise TemplateNotFound()
            if template.status is TemplateStatus.RETIRED:
                return TemplateResult(template=template, replayed=True)
            if template.version != command.expected_version:
                raise TemplateVersionConflict(template.version)
            retired = template.retire(self._clock.now())
            unit_of_work.update_template(retired)
            unit_of_work.add_outbox(
                _outbox(
                    retired,
                    RETIRED_EVENT_TYPE,
                    self._new_id(),
                    command.correlation_id,
                    str(retired.template_id),
                )
            )
            unit_of_work.commit()
        return TemplateResult(template=retired)
