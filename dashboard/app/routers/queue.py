"""Priority Queue API — simulated priority scan."""

from fastapi import APIRouter

from app.services import queue_service

router = APIRouter()


@router.get("/queue")
async def get_queue():
    return queue_service.get_priority_queue()
