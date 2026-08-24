"""Version-one HTTP routes for the first end-to-end delivery slice."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from eventharbor.config import Settings, get_settings
from eventharbor.database import get_session
from eventharbor.errors import DomainError
from eventharbor.repositories import DeliveryRepository, EndpointRepository, EventRepository
from eventharbor.schemas import (
    DeliveryAttemptResponse,
    DeliveryAttemptsResponse,
    DeliverySummaryResponse,
    EndpointCreatedResponse,
    EndpointCreateRequest,
    EventAcceptedResponse,
    EventDetailResponse,
    EventPublishRequest,
)
from eventharbor.services import EndpointService, EventService, QueryService

router = APIRouter(prefix="/v1")
Session = Annotated[AsyncSession, Depends(get_session)]
ApplicationSettings = Annotated[Settings, Depends(get_settings)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


def _delivery_response(delivery: object) -> DeliverySummaryResponse:
    return DeliverySummaryResponse.model_validate(delivery)


@router.post(
    "/endpoints",
    response_model=EndpointCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["endpoints"],
)
async def create_endpoint(
    request: EndpointCreateRequest,
    session: Session,
    settings: ApplicationSettings,
) -> EndpointCreatedResponse:
    """Register Receiver Lab and reveal its new signing secret exactly once."""

    async with session.begin():
        result = await EndpointService(
            EndpointRepository(session),
            settings.receiver_lab_url,
        ).create(request)
    endpoint = result.endpoint
    return EndpointCreatedResponse(
        id=endpoint.id,
        name=endpoint.name,
        url=endpoint.target_url,
        enabled=endpoint.enabled,
        signing_secret=result.plaintext_signing_secret,
        secret_version=endpoint.secret_version,
        created_at=endpoint.created_at,
    )


@router.post(
    "/events",
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["events"],
)
async def publish_event(
    request: EventPublishRequest,
    response: Response,
    session: Session,
    idempotency_key: IdempotencyKey,
) -> EventAcceptedResponse:
    """Persist an event and initial delivery before acknowledging the request."""

    if not idempotency_key.strip():
        raise DomainError(
            status_code=422,
            code="invalid_idempotency_key",
            title="Idempotency key is invalid",
            detail="Idempotency-Key must contain at least one non-whitespace character.",
        )

    async with session.begin():
        result = await EventService(
            EndpointRepository(session),
            EventRepository(session),
            DeliveryRepository(session),
        ).publish(request, idempotency_key)

    response.headers["X-EventHarbor-Idempotent-Replay"] = str(result.idempotent_replay).lower()
    response.headers["Location"] = f"/v1/events/{result.event.id}"
    return EventAcceptedResponse(
        event_id=result.event.id,
        delivery_id=result.delivery.id,
        endpoint_id=result.delivery.endpoint_id,
        type=result.event.event_type,
        status=result.delivery.status,
        created_at=result.event.created_at,
    )


@router.get(
    "/events/{event_id}",
    response_model=EventDetailResponse,
    tags=["events"],
)
async def get_event(event_id: UUID, session: Session) -> EventDetailResponse:
    """Return one immutable event and every associated delivery generation."""

    async with session.begin():
        result = await QueryService(
            EventRepository(session), DeliveryRepository(session)
        ).event_details(event_id)
        response = EventDetailResponse(
            id=result.event.id,
            source=result.event.source,
            type=result.event.event_type,
            data=result.event.payload,
            payload_sha256=result.event.payload_sha256,
            request_fingerprint_sha256=result.event.request_fingerprint_sha256,
            idempotency_key=result.event.idempotency_key,
            created_at=result.event.created_at,
            deliveries=[_delivery_response(delivery) for delivery in result.deliveries],
        )
    return response


@router.get(
    "/deliveries/{delivery_id}/attempts",
    response_model=DeliveryAttemptsResponse,
    tags=["deliveries"],
)
async def get_delivery_attempts(
    delivery_id: UUID,
    session: Session,
) -> DeliveryAttemptsResponse:
    """Return current delivery state plus its durable attempt timeline."""

    async with session.begin():
        result = await QueryService(
            EventRepository(session), DeliveryRepository(session)
        ).delivery_attempts(delivery_id)
        response = DeliveryAttemptsResponse(
            delivery=_delivery_response(result.delivery),
            attempts=[
                DeliveryAttemptResponse.model_validate(attempt) for attempt in result.attempts
            ],
        )
    return response
