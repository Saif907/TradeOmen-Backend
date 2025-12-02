# backend/app/apis/ai_chat.py

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from supabase import Client
from typing import List, Optional
from uuid import UUID

from app.auth.dependency import (
    AuthenticatedUser, 
    DBClient, 
    UserProfile,
    requires_plan, 
    check_ai_quota,
    check_ai_consent
)
from app.libs.data_models import ChatStart, ChatSessionOut, ChatMessageIn, ChatMessageOut
from app.libs.task_queue import enqueue_task
from app.libs.crypto_utils import decrypt_data, encrypt_data

router = APIRouter()

AI_CHAT_QUOTA_DAILY = 50 

# --- CHAT SESSION MANAGEMENT ---

@router.post("/chat/start", response_model=ChatSessionOut, status_code=status.HTTP_201_CREATED)
async def start_chat_session(
    chat_start: ChatStart,
    user: AuthenticatedUser,
    db: DBClient,
    # Correct Usage: Call the factory inside Depends()
    authorized: bool = Depends(requires_plan("AI_CHAT"))
):
    try:
        data_to_insert = {
            'user_id': str(user.user_id),
            'topic': chat_start.topic or f"New Session - {user.user_id}",
            'is_active': True,
        }
        
        response = db.table('chat_sessions').insert(data_to_insert).execute()
        
        if not response.data:
             raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database operation failed.")
        
        logger.success(f"CHAT_START: User {user.user_id} started session {response.data[0]['id']}.")
        
        return ChatSessionOut(**response.data[0])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB_ERROR: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to start chat session.")


@router.get("/chat/sessions", response_model=List[ChatSessionOut])
async def list_chat_sessions(
    user: AuthenticatedUser,
    db: DBClient,
    authorized: bool = Depends(requires_plan("AI_CHAT"))
):
    try:
        response = db.table('chat_sessions').select('*').eq('user_id', str(user.user_id)).order('created_at', desc=True).execute()
        return [ChatSessionOut(**item) for item in response.data]
    except Exception as e:
        logger.error(f"DB_ERROR: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve sessions.")

# --- MESSAGE HANDLING ---

@router.post("/chat/message", response_model=ChatMessageOut)
async def send_chat_message(
    message_in: ChatMessageIn,
    user: AuthenticatedUser,
    profile: UserProfile,
    db: DBClient,
    # Correct Usage: Call factories inside Depends()
    authorized_plan: bool = Depends(requires_plan("AI_CHAT")),
    quota_ok: bool = Depends(check_ai_quota(AI_CHAT_QUOTA_DAILY)),
    consent_ok: bool = Depends(check_ai_consent("AI_CHAT"))
):
    user_id = user.user_id
    
    # Session Validation
    try:
        session_check = db.table('chat_sessions').select('user_id').eq('id', str(message_in.session_id)).single().execute()
        if not session_check.data or UUID(session_check.data['user_id']) != user_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Session access denied.")
    except Exception:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Session validation failed.")


    # Encryption and Save
    try:
        encrypted_content = encrypt_data(message_in.raw_message)
        
        user_message_data = {
            'user_id': str(user_id),
            'session_id': str(message_in.session_id),
            'role': 'user',
            'encrypted_content': encrypted_content,
        }
        
        response = db.table('chat_messages').insert(user_message_data).execute()
        
        if not response.data:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Database write blocked.")
            
        inserted_message = response.data[0]
        logger.info(f"CHAT_MSG_SAVE: Saved message for user {user_id}.")

    except RuntimeError: 
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Encryption failure.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB_ERROR: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save message.")

    # Async Delegation
    try:
        await enqueue_task(
            task_name="llm_response_generate",
            payload={
                "session_id": str(message_in.session_id),
                "user_id": str(user_id),
                "user_message": message_in.raw_message, 
                "current_quota_used": profile.get('ai_chat_quota_used', 0),
            }
        )
    except Exception as e:
        logger.error(f"TASK_FAIL: {e}")
        # Return 503 to indicate partial failure (message saved, but AI processing delayed/failed)
        # In a real app, you might queue a retry.

    # Response
    try:
        content_decrypted = decrypt_data(inserted_message['encrypted_content'])
    except Exception:
        content_decrypted = "[DECRYPTION_FAILED]"
        
    inserted_message['content'] = content_decrypted
    inserted_message.pop('encrypted_content', None)
    
    return ChatMessageOut.model_validate(inserted_message)


@router.get("/chat/{session_id}/history", response_model=List[ChatMessageOut])
async def get_chat_history(
    session_id: UUID,
    user: AuthenticatedUser,
    db: DBClient,
    authorized: bool = Depends(requires_plan("AI_CHAT"))
):
    # Session Owner Check
    try:
        session_check = db.table('chat_sessions').select('user_id').eq('id', str(session_id)).single().execute()
        if not session_check.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found.")
    except Exception:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Session validation failed.")

    try:
        response = db.table('chat_messages').select('*').eq('session_id', str(session_id)).order('created_at', desc=False).execute()
        
        if not response.data:
            return [] 

        decrypted_history = []
        for message in response.data:
            try:
                content_decrypted = decrypt_data(message['encrypted_content'])
            except Exception:
                content_decrypted = "[DECRYPTION_FAILED]"
            
            message['content'] = content_decrypted
            message.pop('encrypted_content', None)
            decrypted_history.append(ChatMessageOut.model_validate(message))

        return decrypted_history
    except Exception as e:
        logger.error(f"DB_ERROR: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve history.")