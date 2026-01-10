"""
Checkout and session management route handlers.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.responses import Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.db.database import insert_transactions_batch, insert_sold_session, get_pg_db
from app.utils import get_client_ip, parse_user_agent
from app.config import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    S3_ENDPOINT_URL,
    S3_BUCKET_NAME,
    S3_CAROUSEL_IMAGES_FOLDER,
    S3_REGION,
)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# Simple in-memory cache for carousel images
class CarouselCache:
    """Modification-time based cache for carousel images."""
    
    def __init__(self):
        self.cache: Optional[list[str]] = None
        self.last_modified: Optional[datetime] = None
    
    def get(self) -> Optional[list[str]]:
        """Get cached images."""
        if self.cache is None:
            return None
        
        logger.debug(f"Carousel cache hit! Last modified: {self.last_modified}")
        return self.cache
    
    def set(self, images: list[str], last_modified: datetime) -> None:
        """Set cached images with last modification timestamp."""
        self.cache = images
        self.last_modified = last_modified
        logger.debug(f"Cached {len(images)} carousel images. Last modified: {last_modified}")
    
    def clear(self) -> None:
        """Manually clear the cache."""
        self.cache = None
        self.last_modified = None
        logger.info("Carousel cache manually cleared")
    
    def get_last_modified(self) -> Optional[datetime]:
        """Get the timestamp of the most recently modified cached file."""
        return self.last_modified


# Global cache instance
carousel_cache = CarouselCache()


def get_s3_client():
    """Create and return configured boto3 S3 client for Supabase."""
    return boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=S3_REGION,
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'}
        )
    )


def get_latest_carousel_modification_time() -> Optional[datetime]:
    """
    Get the latest modification time from carousel folder without fetching all images.
    
    Returns:
        Latest LastModified datetime or None if no files found
    """
    try:
        s3_client = get_s3_client()
        
        # Ensure folder prefix ends with /
        prefix = S3_CAROUSEL_IMAGES_FOLDER if S3_CAROUSEL_IMAGES_FOLDER.endswith('/') else f"{S3_CAROUSEL_IMAGES_FOLDER}/"
        
        # List objects in the carousel folder
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET_NAME,
            Prefix=prefix,
            MaxKeys=100
        )
        
        if 'Contents' not in response:
            return None
        
        # Filter actual image files and find latest modification time
        latest_time = None
        for obj in response['Contents']:
            key = obj['Key']
            if (key != prefix and 
                not key.endswith('/') and
                key.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))):
                
                obj_time = obj['LastModified']
                if latest_time is None or obj_time > latest_time:
                    latest_time = obj_time
        
        return latest_time
        
    except Exception as e:
        logger.error(f"Error checking carousel modification time: {e}")
        return None


def fetch_carousel_images_from_s3() -> tuple[list[str], Optional[datetime]]:
    """
    Fetch carousel images from Supabase S3 storage.
    
    Returns:
        Tuple of (list of presigned URLs sorted alphabetically, latest modification datetime)
    """
    try:
        s3_client = get_s3_client()
        
        # Ensure folder prefix ends with /
        prefix = S3_CAROUSEL_IMAGES_FOLDER if S3_CAROUSEL_IMAGES_FOLDER.endswith('/') else f"{S3_CAROUSEL_IMAGES_FOLDER}/"
        
        # List objects in the carousel folder
        response = s3_client.list_objects_v2(
            Bucket=S3_BUCKET_NAME,
            Prefix=prefix,
            MaxKeys=100
        )
        
        if 'Contents' not in response:
            logger.warning(f"No images found in s3://{S3_BUCKET_NAME}/{prefix}")
            return [], None
        
        # Filter actual image files (skip folder itself and any subdirectories)
        image_objects = [
            obj
            for obj in response['Contents']
            if obj['Key'] != prefix and 
               not obj['Key'].endswith('/') and
               obj['Key'].lower().endswith(('.png', '.jpg', '.jpeg'))
        ]
        
        if not image_objects:
            logger.warning(f"No image files found in s3://{S3_BUCKET_NAME}/{prefix}")
            return [], None
        
        # Find latest modification time
        latest_modified = max(obj['LastModified'] for obj in image_objects)
        
        # Sort by filename (case-insensitive)
        image_objects.sort(key=lambda obj: obj['Key'].split('/')[-1].lower())
        
        # Generate presigned URLs (valid for 24 hours)
        images = []
        for obj in image_objects:
            key = obj['Key']
            try:
                url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET_NAME, 'Key': key},
                    ExpiresIn=86400  # 24 hours
                )
                images.append(url)
                logger.debug(f"Generated presigned URL for: {key.split('/')[-1]}")
            except ClientError as e:
                logger.error(f"Error generating presigned URL for {key}: {e}")
        
        logger.info(f"Fetched {len(images)} carousel images from S3 (latest modified: {latest_modified})")
        return images, latest_modified
        
    except ClientError as e:
        logger.error(f"S3 client error: {e}")
        return [], None
    except Exception as e:
        logger.error(f"Unexpected error fetching carousel images: {e}")
        return [], None


@router.get("/")
def initialize_session(request: Request):
    """
    Initialize a new shopping session.
    """
    request.session.clear()
    request.session['session_id'] = secrets.token_hex(16)
    request.session['cart'] = {}

    logger.debug(f"New session created: {request.session['session_id']}")
    return templates.TemplateResponse(request, 'home/index.html', context={})


def process_checkout(
    session_id: str,
    cart_items: list[dict],
    ip_address: str,
    user_agent: str,
    device_type: str,
    browser: str,
    os: str
) -> None:
    """
    Background task: Process checkout and save to database.
    Logs errors but does not raise them to avoid blocking.
    """
    try:
        # Batch insert all cart items
        insert_transactions_batch(session_id=session_id, items=cart_items)
        
        # Insert session metadata with no country
        insert_sold_session(
            session_id=session_id,
            ip_address=ip_address,
            user_agent=user_agent,
            device_type=device_type,
            browser=browser,
            os=os,
            country=None
        )
        
        logger.info(f"Background checkout completed for session {session_id}")
    except Exception as e:
        logger.error(f"Background checkout failed for session {session_id}: {e}")


@router.post("/finish")
async def finish_checkout(request: Request, background_tasks: BackgroundTasks) -> Response:
    """
    Complete checkout and save transaction data from Alpine.js cart.
    Processes insertions in background for instant user response.
    """
    logger.debug("Checkout process started")
    
    # Parse JSON body from Alpine.js
    try:
        body = await request.json()
        cart_items_dict = body.get('items', {})
    except Exception as e:
        logger.error(f"Failed to parse checkout JSON: {e}")
        return Response(status_code=400, content="Invalid request body")
    
    if cart_items_dict:
        # Get or create session_id
        session_id = request.session.get('session_id')
        if not session_id:
            session_id = secrets.token_hex(16)
            request.session['session_id'] = session_id
        
        # Capture request data before background task
        ip_address = get_client_ip(request)
        ua_info = parse_user_agent(request)
        cart_items_list = list(cart_items_dict.values())
        
        # Schedule background processing
        background_tasks.add_task(
            process_checkout,
            session_id=session_id,
            cart_items=cart_items_list,
            ip_address=ip_address,
            user_agent=ua_info['user_agent'],
            device_type=ua_info['device_type'],
            browser=ua_info['browser'],
            os=ua_info['os']
        )
        
        logger.debug(f"Checkout scheduled for background processing: {session_id}")
    
    # Return immediately - processing happens in background
    return Response(status_code=200)


@router.post("/feedback")
async def submit_feedback(request: Request, feedback_text: str = Form(...)) -> HTMLResponse:
    """
    Submit user feedback.
    """
    client_ip = get_client_ip(request)
    user_agent = parse_user_agent(request)['user_agent']
    
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO feedback_messages 
            (session_id, message, ip_address, user_agent) 
            VALUES (%s, %s, %s, %s)
            """,
            (request.session.get('session_id', 'unknown'), feedback_text, client_ip, user_agent)
        )
        conn.commit()
    
    logger.info(f"Feedback received from session {request.session.get('session_id')}")
    return HTMLResponse(content="<span>Terima kasih atas masukan Anda!</span>")


@router.get("/about")
async def show_about_modal(request: Request) -> Response:
    """
    Display the about modal.
    """
    return templates.TemplateResponse("home/about.html", {
        "request": request
    })


@router.get("/carousel-images")
async def list_carousel_images(request: Request) -> Response:
    """
    Fetch and display carousel images from Supabase S3 with modification-time based caching.
    Only refetches if new files have been added/modified in the carousel folder.
    """
    # Check if there are any new or modified files
    latest_s3_modified = get_latest_carousel_modification_time()
    cached_last_modified = carousel_cache.get_last_modified()
    
    # Determine if we need to refresh the cache
    should_refresh = False
    
    if carousel_cache.get() is None:
        # No cache exists
        logger.debug("No carousel cache found, fetching from S3...")
        should_refresh = True
    elif latest_s3_modified is None:
        # No files in S3 (empty folder)
        logger.debug("No files in S3 carousel folder")
        images_url = []
    elif cached_last_modified is None:
        # Cache exists but no timestamp (shouldn't happen, but handle it)
        logger.debug("Cache exists but no timestamp, refreshing...")
        should_refresh = True
    elif latest_s3_modified > cached_last_modified:
        # New or modified files detected
        logger.info(f"New carousel files detected! S3: {latest_s3_modified}, Cache: {cached_last_modified}")
        should_refresh = True
    else:
        # Cache is up to date
        logger.debug(f"Carousel cache is up to date (last modified: {cached_last_modified})")
        images_url = carousel_cache.get()
    
    # Refresh cache if needed
    if should_refresh and latest_s3_modified is not None:
        images_url, last_modified = fetch_carousel_images_from_s3()
        
        # Update cache with modification timestamp
        if images_url and last_modified:
            carousel_cache.set(images_url, last_modified)
    elif should_refresh and latest_s3_modified is None:
        # No files to cache
        images_url = []
    
    return templates.TemplateResponse("home/carousel.html", {
        "request": request,
        "images": images_url if 'images_url' in locals() else []
    })


@router.post("/admin/clear-carousel-cache")
async def clear_carousel_cache(request: Request, password: str = Form(...)) -> HTMLResponse:
    """
    Manually clear the carousel image cache (admin only).
    """
    from app.config import ADMIN_PASSWORD
    
    if password != ADMIN_PASSWORD:
        return HTMLResponse(content="<span class='text-red-500'>Unauthorized</span>", status_code=401)
    
    carousel_cache.clear()
    return HTMLResponse(content="<span class='text-green-500'>Carousel cache cleared successfully!</span>")
