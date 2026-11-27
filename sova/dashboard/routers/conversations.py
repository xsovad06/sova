"""Conversations API router: the feed panel's conversation tabs (issue #1544). Translates HTTP only.

Every route resolves its project from the request (``/p/{slug}/api`` on a multi-project server) and the service
checks the conversation row's own project binding, so a conversation is only ever served from the project it was
opened in. Turn progress is pushed as a ``conversation`` message on the shared status socket.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from sova.config.context import get_project_dir, resolve_project_dir
from sova.config.loader import load_config
from sova.dashboard.services import conversation_service as svc
from sova.utils.formatting import normalize_issue_key
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.conversations")

router = APIRouter(prefix="/conversations", tags=["conversations"])

NOT_FOUND_DETAIL = "Conversation not found"
_NOT_FOUND_DOC = {"description": "No conversation with that id in this project"}
_BAD_MESSAGE_DOC = {"description": "The message is empty or too long"}
_NOT_FOUND = {404: _NOT_FOUND_DOC}


class CreateRequest(BaseModel):
    issue: str | None = Field(default=None, max_length=51)
    message: str | None = Field(default=None, max_length=svc.MAX_MESSAGE_CHARS)

    @field_validator("issue")
    @classmethod
    def validate_issue(cls, v: str | None) -> str | None:
        """An issue number or tracker key ("#42" and "PROJ-123" too); a blank value means no issue."""
        if v is None or not v.strip():
            return None
        return normalize_issue_key(v)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=svc.MAX_MESSAGE_CHARS)


def _project() -> Path:
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return resolve_project_dir(get_project_dir(), default_project_dir=get_default_project_dir())


def _publisher(project_dir: Path) -> svc.Publish:
    """Send a turn's progress as a ``conversation`` message to the sockets of *project_dir* only."""

    async def publish(payload: dict) -> None:
        from sova.dashboard.routers.agents import _ws_manager

        await _ws_manager.broadcast_event("conversation", project_dir, payload)

    return publish


async def _start(project_dir: Path, conversation_id: int, text: str) -> dict:
    cfg = await asyncio.to_thread(load_config, project_dir)
    try:
        return await svc.start_turn(
            project_dir, cfg.github_user, conversation_id, text, publish=_publisher(project_dir)
        )
    except svc.ConversationLimitError as e:
        raise HTTPException(status_code=409, detail=e.user_message) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail="The message is empty or too long") from e


@router.get("")
async def list_conversations() -> dict:
    """Open conversations (the tabs) and past ones (closed), plus how many turns run now."""
    return await svc.list_conversations(_project())


@router.post(
    "",
    responses={
        400: _BAD_MESSAGE_DOC,
        409: {"description": "The first message was refused: too many turns run"},
    },
)
async def create_conversation(body: CreateRequest) -> dict:
    """Open a conversation, optionally bound to an issue, and start its first turn when a message is given.

    A first turn that fails to start, for any reason, leaves no conversation behind: the client got an error, so
    it keeps the text in its input, and no empty tab appears on the next list.
    """
    project_dir = _project()
    if body.message is not None and not body.message.strip():
        raise HTTPException(status_code=400, detail="The message is empty")
    cfg = await asyncio.to_thread(load_config, project_dir)
    if body.message and svc.running_turns(project_dir) >= cfg.conversation.max_concurrent:
        raise HTTPException(
            status_code=409, detail="Too many conversation turns are running (conversation.max_concurrent)"
        )
    conversation = await svc.create_conversation(project_dir, issue_number=body.issue, title=body.message or "")
    if not body.message:
        return {"conversation": conversation, "turn": None}
    try:
        turn = await _start(project_dir, conversation["id"], body.message)
    except BaseException:
        await asyncio.shield(svc.delete_conversation(project_dir, conversation["id"]))
        raise
    return {"conversation": conversation, "turn": turn}


@router.get("/{conversation_id}", responses=_NOT_FOUND)
async def get_conversation(conversation_id: int) -> dict:
    """The conversation and every stored message (the catch-up fetch)."""
    try:
        return await svc.get_conversation(_project(), conversation_id)
    except svc.ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL) from e


@router.post(
    "/{conversation_id}/messages",
    responses={
        400: _BAD_MESSAGE_DOC,
        404: _NOT_FOUND_DOC,
        409: {"description": "Too many turns run, or this conversation already runs one or is closing"},
    },
)
async def post_message(conversation_id: int, body: MessageRequest) -> dict:
    """Store the operator's message and start the turn answering it."""
    try:
        return await _start(_project(), conversation_id, body.text)
    except svc.ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL) from e


@router.post("/{conversation_id}/stop")
async def stop_turn(conversation_id: int) -> dict:
    """Stop the running turn; its partial answer is stored as stopped."""
    return {"stopped": await svc.stop_turn(_project(), conversation_id)}


@router.post("/{conversation_id}/close", responses=_NOT_FOUND)
async def close_conversation(conversation_id: int) -> dict:
    try:
        return await svc.close_conversation(_project(), conversation_id)
    except svc.ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL) from e


@router.post("/{conversation_id}/reopen", responses=_NOT_FOUND)
async def reopen_conversation(conversation_id: int) -> dict:
    try:
        return await svc.reopen_conversation(_project(), conversation_id)
    except svc.ConversationNotFoundError as e:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL) from e


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: int) -> dict:
    """Delete the conversation and its messages. Deleting one already gone answers ``deleted: false``."""
    return {"deleted": await svc.delete_conversation(_project(), conversation_id)}
