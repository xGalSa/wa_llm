from typing import Annotated, AsyncGenerator

from fastapi import Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from src.handler import MessageHandler
from src.whatsapp.client import WhatsAppClient
from voyageai.client_async import AsyncClient


async def get_db_async_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    assert request.app.state.async_session, "AsyncSession generator not initialized"
    async with request.app.state.async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_whatsapp(request: Request) -> WhatsAppClient:
    assert request.app.state.whatsapp, "WhatsApp client not initialized"
    return request.app.state.whatsapp


def get_text_embebedding(request: Request) -> AsyncClient:
    assert request.app.state.embedding_client, "text embedding not initialized"
    return request.app.state.embedding_client


async def get_handler(
    session: Annotated[AsyncSession, Depends(get_db_async_session)],
    whatsapp: Annotated[WhatsAppClient, Depends(get_whatsapp)],
    embedding_client: Annotated[AsyncClient, Depends(get_text_embebedding)],
) -> MessageHandler:
    # Create a new handler instance for each request to avoid session management issues
    # The global cache in the handler will still prevent duplicate processing
    return MessageHandler(session, whatsapp, embedding_client)
