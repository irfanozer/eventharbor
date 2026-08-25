"""Version-one HTTP routes for the first end-to-end delivery slice."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from eventharbor.config import Settings, get_settings
from eventharbor.control_room import (
    ControlRoomQueryService,
    ControlRoomRepository,
    DeadLetterRecord,
)
from eventharbor.database import get_session
from eventharbor.deliveries.retry import ReplayBlockCode, replay_block_code
from eventharbor.deliveries.state_machine import DeliveryStatus
from eventharbor.demo import ReceiverLabDemoService
from eventharbor.demo_runs import DEMO_RUN_ID_MAX_LENGTH, DEMO_RUN_ID_PATTERN
from eventharbor.errors import DomainError
from eventharbor.models import Endpoint
from eventharbor.repositories import DeliveryRepository, EndpointRepository, EventRepository
from eventharbor.schemas import (
    ControlRoomOverviewResponse,
    DeadLetterItemResponse,
    DeadLetterListResponse,
    DeliveryAttemptResponse,
    DeliveryAttemptsResponse,
    DeliveryStatusCounts,
    DeliverySummaryResponse,
    EndpointCreatedResponse,
    EndpointCreateRequest,
    EndpointListResponse,
    EndpointPublicResponse,
    EventAcceptedResponse,
    EventDetailResponse,
    EventListItemResponse,
    EventListResponse,
    EventPublishRequest,
    ReceiverLabPresetRequest,
    ReceiverLabStateResponse,
    ReplayAcceptedResponse,
)
from eventharbor.services import EndpointService, EventService, QueryService, ReplayService

router = APIRouter(prefix="/v1")
Session = Annotated[AsyncSession, Depends(get_session)]
ApplicationSettings = Annotated[Settings, Depends(get_settings)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
DemoRunId = Annotated[
    str | None,
    Query(max_length=DEMO_RUN_ID_MAX_LENGTH, pattern=DEMO_RUN_ID_PATTERN),
]


def _delivery_response(delivery: object) -> DeliverySummaryResponse:
    return DeliverySummaryResponse.model_validate(delivery)


def _endpoint_response(endpoint: Endpoint) -> EndpointPublicResponse:
    """Map persistence names to the public contract without exposing a secret."""

    return EndpointPublicResponse(
        id=endpoint.id,
        name=endpoint.name,
        url=endpoint.target_url,
        enabled=endpoint.enabled,
        secret_version=endpoint.secret_version,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def _dead_letter_response(item: DeadLetterRecord) -> DeadLetterItemResponse:
    block_code = replay_block_code(
        item.last_http_status_code,
        item.last_response_body_excerpt,
    )
    if block_code is None and not item.endpoint.enabled:
        block_code = ReplayBlockCode.ENDPOINT_DISABLED
    blocked_reasons = {
        ReplayBlockCode.PAYLOAD_CORRECTION_REQUIRED: (
            "Payload correction required. Publish a corrected event instead of "
            "replaying the unchanged body."
        ),
        ReplayBlockCode.ENDPOINT_DISABLED: "Endpoint is disabled.",
    }
    return DeadLetterItemResponse(
        event_id=item.event.id,
        event_type=item.event.event_type,
        event_created_at=item.event.created_at,
        endpoint=_endpoint_response(item.endpoint),
        delivery=_delivery_response(item.delivery),
        replayable=block_code is None,
        blocked_code=block_code,
        blocked_reason=blocked_reasons.get(block_code) if block_code is not None else None,
    )


def _disable_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


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


@router.post(
    "/deliveries/{delivery_id}/replays",
    response_model=ReplayAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["deliveries"],
)
async def replay_delivery(
    delivery_id: UUID,
    response: Response,
    session: Session,
    idempotency_key: IdempotencyKey,
) -> ReplayAcceptedResponse:
    """Create a new generation from the latest dead-lettered delivery."""

    if not idempotency_key.strip():
        raise DomainError(
            status_code=422,
            code="invalid_idempotency_key",
            title="Idempotency key is invalid",
            detail="Idempotency-Key must contain at least one non-whitespace character.",
        )

    async with session.begin():
        result = await ReplayService(
            EndpointRepository(session),
            EventRepository(session),
            DeliveryRepository(session),
        ).replay(delivery_id, idempotency_key)

    replay = result.delivery
    response.headers["X-EventHarbor-Idempotent-Replay"] = str(result.idempotent_replay).lower()
    response.headers["Location"] = f"/v1/deliveries/{replay.id}/attempts"
    return ReplayAcceptedResponse(
        source_delivery_id=result.source_delivery.id,
        delivery_id=replay.id,
        event_id=replay.event_id,
        endpoint_id=replay.endpoint_id,
        replay_generation=replay.replay_generation,
        # An idempotent retry must reproduce the original acceptance body even if the
        # worker has since advanced the delivery to a terminal state.
        status=DeliveryStatus.PENDING,
        created_at=replay.created_at,
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


@router.get(
    "/control-room/overview",
    response_model=ControlRoomOverviewResponse,
    tags=["control room"],
)
async def get_control_room_overview(
    response: Response,
    session: Session,
) -> ControlRoomOverviewResponse:
    """Return a current, bounded operational snapshot for the Control Room."""

    async with session.begin():
        snapshot = await ControlRoomQueryService(ControlRoomRepository(session)).overview()
    counts = snapshot.deliveries_by_status
    _disable_cache(response)
    return ControlRoomOverviewResponse(
        generated_at=snapshot.generated_at,
        events_total=snapshot.events_total,
        endpoints_total=snapshot.endpoints_total,
        endpoints_enabled=snapshot.endpoints_enabled,
        deliveries=DeliveryStatusCounts(
            pending=counts.get(DeliveryStatus.PENDING, 0),
            in_progress=counts.get(DeliveryStatus.IN_PROGRESS, 0),
            retry_wait=counts.get(DeliveryStatus.RETRY_WAIT, 0),
            delivered=counts.get(DeliveryStatus.DELIVERED, 0),
            dead_lettered=counts.get(DeliveryStatus.DEAD_LETTERED, 0),
        ),
        actionable_dead_letters=snapshot.actionable_dead_letters,
    )


@router.get(
    "/events",
    response_model=EventListResponse,
    tags=["events"],
)
async def list_events(
    response: Response,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    event_type: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    endpoint_id: UUID | None = None,
    delivery_status: DeliveryStatus | None = None,
) -> EventListResponse:
    """List recent events by their latest delivery generation."""

    async with session.begin():
        page = await ControlRoomQueryService(ControlRoomRepository(session)).events(
            limit=limit,
            cursor=cursor,
            event_type=event_type,
            endpoint_id=endpoint_id,
            delivery_status=delivery_status,
        )
    _disable_cache(response)
    return EventListResponse(
        items=[
            EventListItemResponse(
                id=item.event.id,
                source=item.event.source,
                type=item.event.event_type,
                created_at=item.event.created_at,
                endpoint=_endpoint_response(item.endpoint),
                latest_delivery=_delivery_response(item.latest_delivery),
                generation_count=item.latest_delivery.replay_generation + 1,
            )
            for item in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/endpoints",
    response_model=EndpointListResponse,
    tags=["endpoints"],
)
async def list_endpoints(
    response: Response,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> EndpointListResponse:
    """List safe endpoint metadata without returning signing secrets."""

    async with session.begin():
        page = await ControlRoomQueryService(ControlRoomRepository(session)).endpoints(
            limit=limit,
            cursor=cursor,
        )
    _disable_cache(response)
    return EndpointListResponse(
        items=[_endpoint_response(endpoint) for endpoint in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/endpoints/{endpoint_id}",
    response_model=EndpointPublicResponse,
    tags=["endpoints"],
)
async def get_endpoint(
    endpoint_id: UUID,
    response: Response,
    session: Session,
) -> EndpointPublicResponse:
    """Return one endpoint's safe public metadata."""

    async with session.begin():
        endpoint = await ControlRoomQueryService(ControlRoomRepository(session)).endpoint(
            endpoint_id
        )
    _disable_cache(response)
    return _endpoint_response(endpoint)


@router.get(
    "/dead-letters",
    response_model=DeadLetterListResponse,
    tags=["deliveries"],
)
async def list_dead_letters(
    response: Response,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    endpoint_id: UUID | None = None,
) -> DeadLetterListResponse:
    """List only latest-generation dead letters that still need operator action."""

    async with session.begin():
        page = await ControlRoomQueryService(ControlRoomRepository(session)).dead_letters(
            limit=limit,
            cursor=cursor,
            endpoint_id=endpoint_id,
        )
    _disable_cache(response)
    return DeadLetterListResponse(
        items=[_dead_letter_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/demo/receiver-lab",
    response_model=ReceiverLabStateResponse,
    tags=["control room"],
)
async def get_receiver_lab_state(
    response: Response,
    settings: ApplicationSettings,
    run_id: DemoRunId = None,
) -> ReceiverLabStateResponse:
    """Read bounded Receiver Lab evidence through the same-origin API facade."""

    async with httpx.AsyncClient(
        base_url=settings.receiver_lab_control_url,
        timeout=3.0,
        follow_redirects=False,
    ) as client:
        result = await ReceiverLabDemoService(client).state(run_id)
    _disable_cache(response)
    return result


@router.put(
    "/demo/receiver-lab",
    response_model=ReceiverLabStateResponse,
    tags=["control room"],
)
async def configure_receiver_lab(
    request: ReceiverLabPresetRequest,
    response: Response,
    settings: ApplicationSettings,
    run_id: DemoRunId = None,
) -> ReceiverLabStateResponse:
    """Apply one named, server-owned Receiver Lab scenario."""

    async with httpx.AsyncClient(
        base_url=settings.receiver_lab_control_url,
        timeout=3.0,
        follow_redirects=False,
    ) as client:
        result = await ReceiverLabDemoService(client).configure(request.preset, run_id)
    _disable_cache(response)
    return result
