# backend/app/apis/data_import.py

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from loguru import logger
from supabase import Client
from typing import List
from uuid import UUID
from anyio import to_thread # FIX: Added missing import

from app.auth.dependency import (
    AuthenticatedUser, 
    DBClient, # The Annotated type alias
    requires_plan
)
from app.libs.data_models import ImportJobStart, ImportJobOut, ImportJobType
from app.libs.task_queue import enqueue_task, _invalidate_edge_cache_async # Using the async wrapper for cache

router = APIRouter()

# --- HELPER FUNCTION ---

def _upload_file_to_storage(user_id: UUID, file: UploadFile, db: Client) -> str:
    """
    Simulates securely uploading the raw file to Supabase Storage before processing.
    NOTE: Supabase Python SDK does not support stream uploads directly. This simplifies the process.
    """
    bucket_name = "trade-imports"
    file_content = file.file.read()
    file_path = f"raw/{user_id}/{file.filename}-{UUID().hex}"

    try:
        # In a real app, you would use db.storage.from_(bucket_name).upload(...)
        # We simulate the success of the upload here for the MVP architecture.
        logger.warning(f"SIMULATING: Uploaded {len(file_content)} bytes to Supabase Storage path: {file_path}")
        # Assuming the file is successfully saved. The path is the storage reference.
        return file_path
    except Exception as e:
        logger.error(f"STORAGE_ERROR: Failed to upload file for user {user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to securely upload file.")


# --- ENDPOINTS ---

@router.post("/import/start", response_model=ImportJobOut, status_code=status.HTTP_202_ACCEPTED, 
             summary="Initiate a bulk trade import job (CSV/Broker)")
async def start_bulk_import(
    job_start: ImportJobStart,
    user: AuthenticatedUser,
    db: DBClient,
    # Freemium Gate: Bulk import is a paid feature (Acceptable Use Policy)
    _ = Depends(requires_plan("BULK_IMPORT"))
):
    """
    Initializes a job tracking entry and queues asynchronous processing of a trade data source.
    (Super Fast / Robustness)
    """
    user_id = user.user_id
    
    # 1. Create PENDING job entry in DB (Immediate Response)
    data_to_insert = {
        'user_id': str(user_id),
        'job_type': job_start.job_type,
        'status': 'PENDING',
        'file_path': job_start.storage_path, # Path if pre-uploaded, or placeholder for broker sync
    }

    try:
        response = db.table('import_jobs').insert(data_to_insert).execute()
        
        if not response.data:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create import job record.")
        
        new_job = response.data[0]
        job_id = UUID(new_job['id'])
        
        # 2. Delegate Heavy Processing to Background Thread (Efficiency)
        await enqueue_task(
            task_name="process_import",
            payload={
                "job_id": str(job_id),
                "user_id": str(user_id),
                "storage_path": job_start.storage_path,
                "job_type": job_start.job_type,
            }
        )
        
        # 3. Return accepted status and job details
        logger.success(f"IMPORT_START: User {user_id} started {job_start.job_type} job {job_id}.")
        return ImportJobOut.model_validate(new_job)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB_ERROR: Failed to initialize import job for user {user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error starting import job.")

@router.post("/import/upload", status_code=status.HTTP_201_CREATED, 
             summary="Directly uploads a CSV file for import processing")
async def upload_csv_for_import(
    user: AuthenticatedUser,
    db: DBClient, # FIX: Moved DBClient (required parameter) before default parameters
    csv_file: UploadFile = File(...),
    job_type: ImportJobType = Form(ImportJobType.CSV_IMPORT),
    _ = Depends(requires_plan("BULK_IMPORT"))
):
    """
    Receives an uploaded file, stores it securely, and queues the processing job.
    This combines the file upload and job initialization steps for better UX.
    """
    user_id = user.user_id
    
    if csv_file.content_type not in ["text/csv", "application/vnd.ms-excel"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only CSV files are supported for import.")
    
    # 1. Store the File Securely
    try:
        # NOTE: This uses a simplified sync helper for the file read/upload simulation
        # In production, large files should use async file handlers or chunked upload APIs.
        storage_path = await to_thread(_upload_file_to_storage, user_id, csv_file, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FILE_UPLOAD_FAIL: User {user_id} file upload failed: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to upload file to storage.")


    # 2. Initialize and Queue the Job (Delegates to the logic above)
    job_start = ImportJobStart(job_type=job_type, storage_path=storage_path)
    return await start_bulk_import(job_start, user, db)


@router.get("/import/jobs", response_model=List[ImportJobOut], summary="List all active/past import jobs")
async def list_import_jobs(
    user: AuthenticatedUser,
    db: DBClient,
    _ = Depends(requires_plan("BULK_IMPORT"))
):
    """
    Retrieves the status of all import jobs for the user.
    (RLS Enforced, Non-breakable)
    """
    try:
        # RLS automatically filters results by user_id
        response = db.table('import_jobs').select('*').order('created_at', desc=True).limit(20).execute()
        
        # Invalidate cache for the jobs list (Efficiency)
        await _invalidate_edge_cache_async(payload={"cache_path": f"/v1/data/import/jobs/{user.user_id}"})
        
        return [ImportJobOut.model_validate(item) for item in response.data]
    except Exception as e:
        logger.error(f"DB_ERROR: Failed to list import jobs for user {user.user_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve job list.")