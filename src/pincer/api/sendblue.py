"""Sendblue ingress on the shared API; the channel verifies its own secret."""

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response

if TYPE_CHECKING:
    from pincer.channels.sendblue import SendblueChannel

router = APIRouter()


@router.post("/api/apps/sendblue/webhook", include_in_schema=False)
async def receive(request: Request) -> Response:
    channel: SendblueChannel | None = getattr(request.app.state, "sendblue_channel", None)
    if channel is None:
        return Response(status_code=503)
    return await channel.receive(request)
