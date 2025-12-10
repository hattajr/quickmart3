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



logger.remove()
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "DEBUG"),
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="[removed]") 
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# DEBUG: In-memory database
db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
with db:
    cursor = db.cursor()
    query = ("""
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        image_url TEXT
    );

    CREATE TABLE sold_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        item_id INTEGER NOT NULL REFERENCES items(id),
        price_at_purchase REAL NOT NULL,
        quantity INTEGER NOT NULL,
        total_price REAL NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX idx_sold_items_item_id ON sold_items (item_id);
    CREATE INDEX idx_sold_items_session_id ON sold_items (session_id);

    """
    )
    cursor.executescript(query)

    items = [
        ("Apple", 5000, "https://plus.unsplash.com/premium_photo-1724249990837-f6dfcb7f3eaa?q=80&w=1287&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D"),
        ("Bananananannananananannananananan", 3000, None),
        ("Banana", 3000, None),
        ("Orange", 7000, None),
        ("Grapes", 2000, None),
        ("Watermelon", 3500, None),
        ("Pineapple", 2500, None),
        ("Mango", 1500, None),
        ("Strawberry", 4000, None),
        ("Blueberry", 5000, None),
        ("Kiwi", 1200, None),
        ("Peach", 1800, None),
        ("Cherry", 6000, None),
        ("Papaya", 2200, None),
        ("Plum", 1600, None),
        ("Coconut", 3000, None)
    ]
    cursor.executemany("INSERT INTO items (name, price, image_url) VALUES (?, ?, ?)", items)

db.commit()
# DEBUG: End in-memory database


def get_total(session):
    total_qty = sum(item['qty'] for item in session['cart'].values())
    total_price = sum(item['price'] * item['qty'] for item in session['cart'].values())
    return total_qty, total_price

def insert_transaction(session_id: str, item_id: int, price_at_purchase: float, quantity: int, total_price: float):
    with db:
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO sold_items (session_id, item_id, price_at_purchase, quantity, total_price) VALUES (?, ?, ?, ?, ?)",
            (session_id, item_id, price_at_purchase, quantity, total_price)
        )
        db.commit()

def _debug_transactions():
    logger.debug(f"Transactions in database:")
    with db:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM sold_items")
        transactions = cursor.fetchall()
        pprint([dict(tx) for tx in transactions])


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
    with db:
        cursor = db.cursor()
        cursor.execute("SELECT id, name, image_url FROM items WHERE name LIKE ?", (f"%{q}%",))
        results = cursor.fetchall()
    if not results  :
        response = '<div disabled>No results found</div>'
    
    else:
        response = ''.join(
            f"""
          <div id="search-item-{row['id']}"
          hx-get="/items/{row['id']}" hx-target="#cart-container" hx-swap="beforeend"
            class="flex border-2 rounded-md border-gray-900 h-16 bg-white {'pointer-events-none opacity-50 cursor-not-allowed' if str(row['id']) in request.session['cart'] else ''}"
            _ = "
            on click
              add .hidden to #search-result-container
              set #search-input's value to ''
              focus() to #search-input
            "
            >
            <div class="aspect-[3/4] w-16 overflow-hidden p-1 flex-shrink-0">
              <img src={row['image_url'] or "https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930"}
                alt="Apple" class="object-cover w-full h-full rounded-md" />
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
    with db:
        cursor = db.cursor()
        cursor.execute("SELECT id, name, price, image_url FROM items WHERE id = ?", (item_id,))
        row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Item not found")
        
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
    with db:
        cursor = db.cursor()
        cursor.execute("""
            SELECT item_id, name, quantity FROM sold_items si
            JOIN items i ON si.item_id = i.id
            GROUP BY item_id
            ORDER BY SUM(quantity) DESC
            LIMIT 15
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
                session_id=request.session['session_id'],
                price_at_purchase=item['price'],
                quantity=item['qty'],
                total_price=item['price'] * item['qty']
            )
        
    logger.info("Transactions inserted into database.")

    _debug_transactions()


    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/"
    return response

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "9982")),
        workers=int(os.getenv("WORKERS", "1")),
        reload=bool(int(os.getenv("RELOAD", "1"))),
    )
