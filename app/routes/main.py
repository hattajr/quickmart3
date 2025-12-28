"""
Checkout and session management route handlers.
"""
import secrets
from fastapi import APIRouter, Request, Form
from fastapi.responses import Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
import httpx
from xml.etree import ElementTree

from app.db.database import insert_transaction, insert_sold_session, get_pg_db
from app.utils import get_client_ip, parse_user_agent, get_country_from_ip
from app.config import MINIO_URL, MINIO_CAROUSEL_IMAGE_URL, CAROUSEL_URL


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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


@router.post("/finish")
async def finish_checkout(request: Request) -> Response:
    """
    Complete checkout and save transaction data.
    """
    logger.debug("Checkout process started")
    
    if request.session and request.session.get("cart"):
        session_id = request.session['session_id']
        
        for item in request.session["cart"].values():
            insert_transaction(
                session_id=session_id,
                item_id=item['id'],
                item_name=item['name'],
                price_at_purchase=item['price'],
                quantity=item['qty'],
                total_price=item['price'] * item['qty']
            )

        ip_address = get_client_ip(request)
        ua_info = parse_user_agent(request)
        country = await get_country_from_ip(ip_address)
        
        insert_sold_session(
            session_id=session_id,
            ip_address=ip_address,
            user_agent=ua_info['user_agent'],
            device_type=ua_info['device_type'],
            browser=ua_info['browser'],
            os=ua_info['os'],
            country=country
        )
        
        logger.info(f"Checkout completed for session {session_id}")
    
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/"
    return response


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
    Fetch and display carousel images from MinIO.
    """
    # url = f"{MINIO_URL}?list-type=2&prefix={MINIO_CAROUSEL_IMAGE_URL.split('/')[-1]}/"
    # logger.debug(f"Fetching carousel images from: {url}")
    
    # async with httpx.AsyncClient() as client:
    #     response = await client.get(url)
    #     xml = response.text
    
    # root = ElementTree.fromstring(xml)
    # ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

    # images = []
    # for content in root.findall("s3:Contents", ns):
    #     key = content.find("s3:Key", ns).text
    #     images.append(f"{MINIO_URL}/{key}")
    
    params = {"ref": "tailwind"}
    async with httpx.AsyncClient() as client:
        response = await client.get(url=CAROUSEL_URL, params=params)
        response.raise_for_status()
        items = response.json()

    images_url = [
        i["download_url"]
        for i in items
        if i["type"] == "file"
    ]
    logger.debug(f"Found {len(images_url)} carousel images")
    return templates.TemplateResponse("home/carousel.html", {
        "request": request,
        "images": images_url
    })
