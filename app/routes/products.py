"""
Product search and catalog route handlers.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.config import PRODUCT_IMAGE_BASE_URL
from app.db.database import get_pg_db


router: APIRouter = APIRouter()
templates: Jinja2Templates = Jinja2Templates(directory="app/templates")


@router.get("/search")
async def search_products(request: Request, q: str):
    """
    Search for products by name.
    """
    logger.debug(f"Search query: {q}")
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, barcode, name, image_url FROM products WHERE name ILIKE %s",
            (f"%{q}%",)
        )
        results = cursor.fetchall()

        if not results:
            response = '<div disabled>No results found</div>'
        else:
            items = []
            for row in results:
                # Construct image URL with fallback
                img_url = row['image_url'] if row['image_url'] else f"{PRODUCT_IMAGE_BASE_URL}/{row['barcode']}.png"
                logger.debug(f"Product ID {row['id']} image URL: {img_url}")
                barcode = row['barcode']
                base_url = PRODUCT_IMAGE_BASE_URL
                fallback_url = "https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930"
                
                item_html = f"""
            <div id="search-item-{row['id']}"
            hx-get="/items/{row['id']}" hx-target="#cart-container" hx-swap="beforeend"
                class="flex border-2 rounded-md border-gray-900 h-16 bg-white"
                _ = "
                on load
                    if #cart-item-{row['id']} is in document
                    add .pointer-events-none to me
                    add .opacity-50 to me
                    add .cursor-not-allowed to me
                    end
                on click
                add .hidden to #search-result-container
                set #search-input's value to ''
                focus() to #search-input
                "
                >
                <div class="aspect-[3/4] w-16 overflow-hidden p-1 flex-shrink-0">
                <img src="{img_url}"
                    onerror="if(!this.dataset.r){{this.dataset.r='1';this.src='{base_url}/{barcode}.jpg'}}else if(this.dataset.r=='1'){{this.dataset.r='2';this.src='{base_url}/{barcode}.jpeg'}}else{{this.onerror=null;this.src='{fallback_url}'}}"
                     class="object-cover w-full h-full rounded-md" />
                </div>
                <div class="flex-1 p-2 flex items-center min-w-0">
                <div class="truncate w-full">
                    {row['name']}
                </div>
                </div>
            </div>
                """
                items.append(item_html)
            
            response = ''.join(items)
        return HTMLResponse(content=response)


@router.get("/catalog")
async def show_catalog(request: Request):
    """
    Display the full product catalog.
    """
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute("SELECT id, barcode, name, image_url, price FROM products")
        results = cursor.fetchall()

        if not results:
            results = []

    logger.debug(f"Catalog loaded with {len(results)} products")

    return templates.TemplateResponse("home/catalog.html", {
        "request": request,
        "items": results,
        "assets_url": f"{PRODUCT_IMAGE_BASE_URL}/"
    })


@router.get("/favorites")
async def show_favorite_items(request: Request):
    """
    Display most frequently purchased items.
    """
    for db in get_pg_db():
        cursor = db.cursor()
        cursor.execute("""
            SELECT item_id, item_name, SUM(quantity) as total_quantity
            FROM sold_items si
            GROUP BY item_id, item_name
            ORDER BY total_quantity DESC
            LIMIT 15
        """)
        items = cursor.fetchall()

    return templates.TemplateResponse("home/_favorites.html", {
        "request": request,
        "items": items
    })
