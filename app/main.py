from httpcore import request
import os
from loguru import logger
import sys
import uvicorn
import aiosqlite

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3
from starlette.middleware.sessions import SessionMiddleware
from fastapi.templating import Jinja2Templates
from pprint import pprint
import secrets
import psycopg2
from psycopg2.extras import RealDictCursor
from user_agents import parse


MINIO_URL = os.getenv("MINIO_URL")

logger.remove()
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "DEBUG"),
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="your-super-secret-key") 
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def get_pg_db():
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        database=os.getenv("PG_DATABASE", "quickmart"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASSWORD", "password"),
        cursor_factory=RealDictCursor
    )
    try:
        yield conn
    finally:
        conn.close()

def get_client_ip(request: Request) -> str:
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host

def get_user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "")

def parse_user_agent(request: Request) -> dict:
    """Parse user agent to extract device, browser, OS"""
    user_agent_string = get_user_agent(request)
    user_agent = parse(user_agent_string)
    
    return {
        "user_agent": user_agent_string,
        "device_type": "mobile" if user_agent.is_mobile else 
                      "tablet" if user_agent.is_tablet else 
                      "desktop",
        "browser": f"{user_agent.browser.family} {user_agent.browser.version_string}",
        "os": f"{user_agent.os.family} {user_agent.os.version_string}"
    }

async def get_country_from_ip(ip_address: str) -> str:
    """Get country code from IP address using free API"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://ip-api.com/json/{ip_address}")
            data = response.json()
            return data.get("countryCode", "")  # Returns 2-letter code like "KR", "US"
    except:
        return ""

def get_total(session):
    total_qty = sum(item['qty'] for item in session['cart'].values())
    total_price = sum(item['price'] * item['qty'] for item in session['cart'].values())
    return total_qty, total_price

def insert_transaction(session_id: str, item_id: int, item_name: str, price_at_purchase: float, quantity: int, total_price: float):
    for db in get_pg_db():
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO sold_items (session_id, item_id, item_name, price_at_purchase, quantity, total_price) VALUES (%s, %s, %s, %s, %s, %s)",
            (session_id, item_id, item_name, price_at_purchase, quantity, total_price)
        )
        db.commit()
        logger.debug(f"Inserted transaction for item {item_name} (ID: {item_id}) into database.")

async def log_sold_session(request: Request):
    for db in get_pg_db():
        cur = db.cursor()
        session_id = request.session.get('session_id', 'unknown')
        ip_address = get_client_ip(request)
        ua_info = parse_user_agent(request)
        country= await get_country_from_ip(ip_address)

        cur.execute(
            "INSERT INTO sold_sessions (session_id, ip_address, user_agent, device_type, browser, os, country) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
            (session_id, get_client_ip(request), ua_info['user_agent'], ua_info['device_type'], ua_info['browser'], ua_info['os'], country)
        )
        db.commit()
        logger.debug(f"Logged sold session {session_id} into database.")



@app.get("/")
def home_page(request: Request):
    context = {}
    request.session.clear()
    # set session id
    request.session['session_id'] = secrets.token_hex(16)
    request.session['cart'] = {}

    logger.debug(f"Session data: {request.session}")
    return templates.TemplateResponse(request, 'home/index.html', context=context)

@app.get("/search")
async def search(request: Request, q: str):
    logger.debug(f"Search query: {q}")
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute("SELECT id, barcode, name, image_url FROM products WHERE name ILIKE %s", (f"%{q}%",))
        results = cursor.fetchall()

        if not results:
            response = '<div disabled>No results found</div>'
        
        else:
            response = ''.join(
                f"""
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
                <img src={row['image_url'] or f"{MINIO_URL}/{row['barcode']}.png"}
                    onerror="this.onerror=null; this.src='https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930';"
                     class="object-cover w-full h-full rounded-md" />
                </div>
                <div class="flex-1 p-2 flex items-center min-w-0">
                <div class="truncate w-full">
                    {row['name']}
                </div>
                </div>
            </div>
                """
                for row in results
            )
        return HTMLResponse(content=response)

@app.get("/items/{item_id}")
async def read_item(request:Request, item_id: int):
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute("SELECT id, barcode, name, price, image_url FROM products WHERE id = %s", (item_id,))
        row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Item not found")
        
        if row['image_url'] is None:
            row['image_url'] = f"{MINIO_URL}/{row['barcode']}.png"

        request.session["cart"][str(row['id'])] = dict(id=row['id'], name=row['name'], price=row['price'], image_url=row['image_url'], qty=1)
        row = request.session["cart"][str(row['id'])]
        logger.debug(f"Session after adding item: {request.session}")

        total_qty, total_price = get_total(request.session)
        return templates.TemplateResponse("home/_cart_item.html", {
            "request": request,
            "row": row,
            "price": f"{int(row['price']):,}",
            "sub_total": f"{int(row['price'] * row['qty']):,}",
            "total_qty": total_qty,
            "total_price": f"{int(total_price):,}"
        })

          

@app.delete("/remove/{item_id}")
async def remove_item(request: Request, item_id: int):
    item_key = str(item_id)
    if item_key in request.session["cart"]:
        del request.session["cart"][item_key]
        logger.debug(f"Item {item_id} removed from session")
    else:
        logger.debug(f"Item {item_id} not found in session")
    logger.debug(f"Session after removal: {request.session}")
    total_qty, total_price = get_total(request.session)
    return templates.TemplateResponse("home/_total.html", {
        "request": request,
        "total_qty": total_qty,
        "total_price": f"{int(total_price):,}"
    })


@app.get("/decrease/{item_id}")
async def decrease_item(request: Request, item_id: int):
    # minumum quantity is 1
    item_key = str(item_id)
    if item_key in request.session["cart"]:
        if request.session['cart'][item_key]['qty'] > 1:
            request.session["cart"][item_key]['qty'] -= 1
            logger.debug(f"Decreased quantity of item {item_id} to {request.session['cart'][item_key]['qty']}")
    else:
        logger.debug(f"Item {item_id} not found in session")
    logger.debug(f"Session after decrease: {request.session}")

    row = request.session["cart"][item_key]
    total_qty, total_price = get_total(request.session)
    return templates.TemplateResponse("home/_sub_total.html", {
        "request": request,
        "row": row,
        "qty": row['qty'],
        "sub_total": f"{int(row['price'] * row['qty']):,}",
        "total_qty": total_qty,
        "total_price": f"{int(total_price):,}"
    })

@app.get("/increase/{item_id}")
async def increase_item(request: Request, item_id: int):
    item_key = str(item_id)
    if item_key in request.session["cart"]:
        request.session["cart"][item_key]['qty'] += 1
        logger.debug(f"Increased quantity of item {item_id} to {request.session['cart'][item_key]['qty']}")
    logger.debug(f"Session after increase: {request.session}")
    row = request.session["cart"][item_key]
    total_qty, total_price = get_total(request.session)
    return templates.TemplateResponse("home/_sub_total.html", {
        "request": request,
        "row": row,
        "qty": row['qty'],
        "sub_total": f"{int(row['price'] * row['qty']):,}",
        "total_qty": total_qty,
        "total_price": f"{int(total_price):,}"
    })
      
@app.get("/favorites")
async def favorite_items(request: Request):
    for db in get_pg_db():
        cursor = db.cursor()
        cursor.execute("""
SELECT item_id, item_name, SUM(quantity) as total_quantity
FROM sold_items si
GROUP BY item_id, item_name
ORDER BY total_quantity DESC
LIMIT 15;
        """)
        items = cursor.fetchall()

    return templates.TemplateResponse("home/_favorites.html", {
        "request": request,
        "items": items
    })

@app.post("/finish")
async def finish_checkout(request: Request):
    logger.debug("Checkout finished, session cleared.")
    logger.debug(f"Final session data: {request.session}")
    if request.session:
        for item in request.session["cart"].values():
            insert_transaction(
                item_id=item['id'],
                item_name=item['name'],
                session_id=request.session['session_id'],
                price_at_purchase=item['price'],
                quantity=item['qty'],
                total_price=item['price'] * item['qty']
            )

        await log_sold_session(request)
        
    logger.info("Transactions inserted into database.")
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/"
    return response

@app.get("/about")
async def about_modal(request: Request):
    return templates.TemplateResponse("home/about.html", {
        "request": request
    })

@app.get("/catalog")
async def catalog_modal(request: Request):

    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute("SELECT id, barcode, name, image_url, price FROM products")
        results = cursor.fetchall()

        if not results:
            response = '<div disabled>No results found</div>'

    return templates.TemplateResponse("home/catalog.html", {
        "request": request,
        "items": results,
        "assets_url": f"{MINIO_URL}"
    })


@app.post("/feedback")
async def feedback_submission(request: Request, feedback_text: str = Form(...)):
    client_ip = get_client_ip(request)
    user_agent = get_user_agent(request)
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO feedback_messages (session_id, message, ip_address, user_agent) VALUES (%s, %s, %s, %s)",
            (request.session.get('session_id', 'unknown'), feedback_text, client_ip, user_agent)
        )
        conn.commit()
    logger.info(f"Feedback received: {feedback_text}")
    return HTMLResponse(content="<span>Terima kasih atas masukan Anda!</span>")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "9982")),
        workers=int(os.getenv("WORKERS", "1")),
        reload=bool(int(os.getenv("RELOAD", "1"))),
    )
