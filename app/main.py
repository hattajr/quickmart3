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
from starlette.middleware.sessions import SessionMiddleware


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
        ("Bananananannananananannananananan", 0.3, None),
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
    request.session.clear()
    logger.debug(f"Session data: {request.session}")
    return templates.TemplateResponse(request, 'home/index.html', context=context)

@app.get("/search")
async def search(request: Request, q: str):
    print(f"Searching for: {q}")
    with db:
        cursor = db.cursor()
        cursor.execute("SELECT id, name, image_url FROM items WHERE name LIKE ?", (f"%{q}%",))
        results = cursor.fetchall()

        for row in results:
          if row['id'] in request.session:
            logger.debug(f"Item {row['id']} found in session")
            del results[results.index(row)]
    if not results  :
        response = '<sl-menu-item disabled>No results found</sl-menu-item>'

    else:
        response = ''.join(
            f"""
          <div id="search-item-{row['id']}"
          hx-get="/items/{row['id']}" hx-target="#cart-container" hx-swap="beforeend"
            class="flex border-2 rounded-md border-gray-900 h-16 bg-white"
            _ = "on click remove me"
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
            for row in results if row
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
        
        request.session[str(row['id'])] = dict(id=row['id'], name=row['name'], price=row['price'], image_url=row['image_url'], qty=1)
        row = request.session[str(row['id'])]
        logger.debug(f"Session after adding item: {request.session}")

          
        response = f"""
    <div id="cart-item-{row['id']}"
      class="border-2 rounded-md border-gray-900 shadow-[4px_4px_0px] h-24 flex overflow-hidden bg-white"
      >
      <div class="w-20 flex-shrink-0 bg-gray-200">
        <img src={row['image_url'] or "https://upload.wikimedia.org/wikipedia/commons/1/14/No_Image_Available.jpg?20200913095930"} alt="apple"
          class="w-full h-full object-cover">
      </div>
      <div class="flex-1 flex flex-col justify-between p-3 min-w-0">
        <div class="flex items-start justify-between gap-2">
          <div class="flex-1 min-w-0">
            <div class="text-sm font-bold truncate">
                {row['name']}
            </div>
            <div class="text-xs text-gray-600 mt-0.5">₩10,000/pcs</div>
          </div>
          <button class="text-gray-500 hover:text-red-600 flex-shrink-0"
          _="
          on click  fetch /remove/{row['id']} then remove #cart-item-{row['id']}
          ">
            <span class="material-symbols-outlined text-xl">close</span>
          </button>
        </div>
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <button
              class="w-6 h-6 flex items-center justify-center border border-gray-900 rounded hover:bg-gray-100">
              <span class="material-symbols-outlined text-base">remove</span>
            </button>
            <span class="text-sm w-6 text-center font-medium">{row['qty']}</span>
            <button
              class="w-6 h-6 flex items-center justify-center border border-gray-900 rounded hover:bg-gray-100">
              <span class="material-symbols-outlined text-base">add</span>
            </button>
          </div>
          <div class="text-base font-bold">₩30,000</div>
        </div>
      </div>
    </div>
        """
    return HTMLResponse(content=response)

@app.get("/remove/{item_id}")
async def remove_item(request: Request, item_id: int):
    item_key = str(item_id)
    if item_key in request.session:
        del request.session[item_key]
        logger.debug(f"Item {item_id} removed from session")
    else:
        logger.debug(f"Item {item_id} not found in session")
    logger.debug(f"Session after removal: {request.session}")
    return None
if __name__ == "__main__":



    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "9021")),
        workers=int(os.getenv("WORKERS", "1")),
        reload=bool(int(os.getenv("RELOAD", "1"))),
    )
