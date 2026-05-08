"""
Product search and catalog route handlers.
"""

import html
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.config import PRODUCT_IMAGE_BASE_URL
from app.db.database import get_db
from app.utils import get_client_ip
from app.utils.product_cache import product_cache

router: APIRouter = APIRouter()
templates: Jinja2Templates = Jinja2Templates(directory="app/templates")


def log_search_selection(
    session_id: str, product_id: int, product_name: str, search_query: str, ip_address: str
) -> None:
    """
    Background task: Log search selection to database.
    """
    try:
        for conn in get_db():
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO search_selections 
                (session_id, product_id, product_name, search_query, ip_address) 
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, product_id, product_name, search_query, ip_address),
            )
            conn.commit()
        logger.info(f"Logged search selection: product_id={product_id}, query='{search_query}'")
    except Exception as e:
        logger.error(f"Failed to log search selection: {e}")


@router.post("/api/log-search-selection")
async def log_selection(request: Request, background_tasks: BackgroundTasks) -> Response:
    """
    Log when a user selects a product from search results.
    """
    try:
        body = await request.json()
        product_id = body.get("product_id")
        product_name = body.get("product_name", "")
        search_query = body.get("search_query", "")

        if not product_id:
            return Response(status_code=400)

        session_id = request.session.get("session_id", "unknown")
        ip_address = get_client_ip(request)

        background_tasks.add_task(
            log_search_selection,
            session_id=session_id,
            product_id=product_id,
            product_name=product_name,
            search_query=search_query,
            ip_address=ip_address,
        )

        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Error in log_selection endpoint: {e}")
        return Response(status_code=500)


@router.get("/api/items/{item_id}")
async def get_item_api(item_id: int):
    """
    Get product data as JSON for Alpine.js cart.
    """
    for conn in get_db():
        cursor = conn.cursor()
        cursor.execute("SELECT id, barcode, name, price, image_url FROM products WHERE id = ?", (item_id,))
        row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Item not found")

        # Server-side image fallback cascade
        img_url = row["image_url"] if row["image_url"] else f"{PRODUCT_IMAGE_BASE_URL}/{row['barcode']}.png"

        return JSONResponse(
            content={"id": row["id"], "name": row["name"], "price": float(row["price"]), "image_url": img_url}
        )


def _render_search_item(row: object) -> str:
    """Render one product card for the search results fragment.

    Isolating this here keeps the route handler free of escaping/rendering
    details and gives a single place to audit output encoding.
    """

    # sqlite3.Row supports dict-style access
    img_url = row["image_url"] if row["image_url"] else f"{PRODUCT_IMAGE_BASE_URL}/{row['barcode']}.png"  # type: ignore[index]
    base_url = PRODUCT_IMAGE_BASE_URL
    fallback_url = "https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930"

    safe_img_url = html.escape(img_url, quote=True)
    safe_barcode = html.escape(row["barcode"] or "", quote=True)  # type: ignore[index]
    safe_base_url = html.escape(base_url, quote=True)
    safe_fallback_url = html.escape(fallback_url, quote=True)
    safe_name = html.escape(row["name"])  # type: ignore[index]
    product_id = row["id"]  # type: ignore[index]

    return f"""
        <div id="search-item-{product_id}"
            class="flex border-2 rounded-md border-gray-900 h-16 bg-white cursor-pointer hover:bg-gray-50"
            @click="
                fetch('/api/items/{product_id}')
                    .then(res => res.json())
                    .then(data => {{
                        $store.cart.addItem(data);
                        $store.search.close();
                        fetch('/api/log-search-selection', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{
                                product_id: {product_id},
                                product_name: data.name,
                                search_query: $store.search.query
                            }})
                        }}).catch(err => console.error('Log failed:', err));
                    }})
                    .catch(err => $store.toasts.show('Failed to add item', 'error'))
            "
            >
            <div class="aspect-[3/4] w-16 overflow-hidden p-1 flex-shrink-0">
            <img src="{safe_img_url}"
                data-base="{safe_base_url}"
                data-barcode="{safe_barcode}"
                data-fallback="{safe_fallback_url}"
                onerror="if(!this.dataset.r){{this.dataset.r='1';this.src=this.dataset.base+'/'+this.dataset.barcode+'.jpg'}}else if(this.dataset.r=='1'){{this.dataset.r='2';this.src=this.dataset.base+'/'+this.dataset.barcode+'.jpeg'}}else{{this.onerror=null;this.src=this.dataset.fallback}}"
                 class="object-cover w-full h-full rounded-md" />
            </div>
            <div class="flex-1 p-2 flex items-center min-w-0">
            <div class="truncate w-full">
                {safe_name}
            </div>
            </div>
        </div>
            """


@router.get("/search")
async def search_products(request: Request, q: str, exclude_ids: Optional[str] = None):
    """
    Search for products using fuzzy matching, excluding items already in cart.
    """
    logger.debug(f"Search query: {q}, exclude_ids: {exclude_ids}")

    # Parse excluded IDs
    excluded_id_list = []
    if exclude_ids and exclude_ids.strip():
        try:
            excluded_id_list = [int(id.strip()) for id in exclude_ids.split(",") if id.strip()]
        except ValueError:
            logger.warning(f"Invalid exclude_ids format: {exclude_ids}")

    # Perform fuzzy search
    ranked_ids = product_cache.fuzzy_search(q, exclude_ids=excluded_id_list if excluded_id_list else None)

    if not ranked_ids:
        response = '<div class="p-4 text-center text-gray-600">No results found</div>'
        return HTMLResponse(content=response)

    # Query database for ranked products
    for conn in get_db():
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(ranked_ids))
        cursor.execute(
            f"SELECT id, barcode, name, image_url FROM products WHERE id IN ({placeholders})",
            ranked_ids,
        )
        results = cursor.fetchall()

        # Create ID->row mapping
        row_map = {row["id"]: row for row in results}

        # Reorder results to match fuzzy ranking
        ordered_results = [row_map[pid] for pid in ranked_ids if pid in row_map]

        items = [_render_search_item(row) for row in ordered_results]
        response = f'<div class="p-1.5 flex flex-col gap-2">{"".join(items)}</div>'
        return HTMLResponse(content=response)


@router.get("/catalog")
async def show_catalog(request: Request):
    """
    Display the full product catalog.
    """
    results: list = []
    for conn in get_db():
        cursor = conn.cursor()
        cursor.execute("SELECT id, barcode, name, image_url, price FROM products")
        results = cursor.fetchall()

        if not results:
            results = []

    logger.debug(f"Catalog loaded with {len(results)} products")

    return templates.TemplateResponse(
        request, "home/catalog.html", {"items": results, "assets_url": f"{PRODUCT_IMAGE_BASE_URL}/"}
    )


@router.get("/favorites")
async def show_favorite_items(request: Request):
    """
    Display most frequently purchased items.
    """
    items: list = []
    for db in get_db():
        cursor = db.cursor()
        cursor.execute("""
            SELECT item_id, item_name, SUM(quantity) as total_quantity
            FROM sold_items si
            GROUP BY item_id, item_name
            ORDER BY total_quantity DESC
            LIMIT 15
        """)
        items = cursor.fetchall()

    return templates.TemplateResponse(request, "home/_favorites.html", {"items": items})
