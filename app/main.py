import os
from loguru import logger
import sys
import uvicorn
import aiosqlite

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3


logger.remove()
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "DEBUG"),
    enqueue=True,
    format="[<level>{level: <8}</level>] <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# DEBUG: In-memory database
db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
with db:
    cursor = db.cursor()

    cursor.execute("""
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        image_url TEXT

    );
    """
    )

    items = [
        ("Apple", 0.5, "https://plus.unsplash.com/premium_photo-1724249990837-f6dfcb7f3eaa?q=80&w=1287&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D"),
        ("Banana", 0.3, None),
        ("Orange", 0.7, None),
        ("Grapes", 2.0, None),
        ("Watermelon", 3.5, None),
        ("Pineapple", 2.5, None),
        ("Mango", 1.5, None),
        ("Strawberry", 4.0, None),
        ("Blueberry", 5.0, None),
        ("Kiwi", 1.2, None),
        ("Peach", 1.8, None),
        ("Cherry", 6.0, None),
        ("Papaya", 2.2, None),
        ("Plum", 1.6, None),
        ("Coconut", 3.0, None)
    ]
    cursor.executemany("INSERT INTO items (name, price, image_url) VALUES (?, ?, ?)", items)

db.commit()
# DEBUG: End in-memory database



@app.get("/")
def home_page(request: Request):
    context = {}
    return templates.TemplateResponse(request, 'home/index.html', context=context)

@app.get("/search")
async def search(request: Request, q: str):
    print(f"Searching for: {q}")
    with db:
        cursor = db.cursor()
        cursor.execute("SELECT id, name, image_url FROM items WHERE name LIKE ?", (f"%{q}%",))
        results = cursor.fetchall()
        logger.debug(f"Search results: {results}")
    for row in results:
        logger.debug(f"Row: {row['id']}, {row['name']}")
    if not results  :
        response = '<sl-menu-item disabled>No results found</sl-menu-item>'

    else:
        response = ''.join(
            f"""
            <div
            hx-get="/items/{row['id']}"
            hx-target="#cart-container"
            hx-swap="beforeend"
            class="search-result-item"
            >
                <img
                    src="{row['image_url'] or 'https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930'}"
                    alt="{row['name']}" />
                {row['name']}
            </div>
            """
            for row in results
        )

    return HTMLResponse(content=response)

@app.get("/items/{item_id}")
async def read_item(item_id: int):
    with db:
        cursor = db.cursor()
        cursor.execute("SELECT id, name, price, image_url FROM items WHERE id = ?", (item_id,))
        row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Item not found")
        response = f"""
            <div class="cart-item">
                    <img
                        src="{row['image_url'] or 'https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930'}"
                        alt="{row['name']}" />
                <h2>{row['name']}</h2>
                <span class="price">${row['price']:.2f}</span>
            </div>
        """
    return HTMLResponse(content=response)

if __name__ == "__main__":



    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "9021")),
        workers=int(os.getenv("WORKERS", "1")),
        reload=bool(int(os.getenv("RELOAD", "1"))),
    )
