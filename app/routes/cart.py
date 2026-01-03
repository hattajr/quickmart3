"""
Shopping cart related route handlers.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.config import PRODUCT_IMAGE_BASE_URL
from app.db.database import get_pg_db
from app.utils.cart import get_cart_total


router: APIRouter = APIRouter()
templates: Jinja2Templates = Jinja2Templates(directory="app/templates")


@router.get("/items/{item_id}")
async def add_item_to_cart(request: Request, item_id: int):
    """
    Add an item to the shopping cart.
    """
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, barcode, name, price, image_url FROM products WHERE id = %s",
            (item_id,)
        )
        row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Item not found")
        
        logger.debug(f"item image_url: {row['image_url']}")
        logger.debug(f"image url is None: {row['image_url'] is None}")
        if row['image_url'] is None:

            row['image_url'] = f"{PRODUCT_IMAGE_BASE_URL}/{row['barcode']}.png"

        request.session["cart"][str(row['id'])] = dict(
            id=row['id'],
            name=row['name'],
            price=row['price'],
            image_url=row['image_url'],
            qty=1
        )
        row = request.session["cart"][str(row['id'])]
        logger.debug(f"Added item {item_id} to cart")
        logger.debug(f"item image_url: {row['image_url']}")

        total_qty, total_price = get_cart_total(request.session["cart"])
        return templates.TemplateResponse("home/_cart_item.html", {
            "request": request,
            "row": row,
            "price": f"{int(row['price']):,}",
            "sub_total": f"{int(row['price'] * row['qty']):,}",
            "total_qty": total_qty,
            "total_price": f"{int(total_price):,}"
        })


@router.delete("/remove/{item_id}")
async def remove_item_from_cart(request: Request, item_id: int):
    """
    Remove an item from the shopping cart.
    """
    item_key = str(item_id)
    if item_key in request.session["cart"]:
        del request.session["cart"][item_key]
        logger.debug(f"Item {item_id} removed from cart")
    else:
        logger.debug(f"Item {item_id} not found in cart")
    
    total_qty, total_price = get_cart_total(request.session["cart"])
    return templates.TemplateResponse("home/_total.html", {
        "request": request,
        "total_qty": total_qty,
        "total_price": f"{int(total_price):,}"
    })


@router.get("/decrease/{item_id}")
async def decrease_item_quantity(request: Request, item_id: int):
    """
    Decrease the quantity of an item in the cart (minimum 1).
    """
    item_key = str(item_id)
    if item_key in request.session["cart"]:
        if request.session['cart'][item_key]['qty'] > 1:
            request.session["cart"][item_key]['qty'] -= 1
            logger.debug(f"Decreased quantity of item {item_id}")
    else:
        logger.debug(f"Item {item_id} not found in cart")

    row = request.session["cart"][item_key]
    total_qty, total_price = get_cart_total(request.session["cart"])
    return templates.TemplateResponse("home/_sub_total.html", {
        "request": request,
        "row": row,
        "qty": row['qty'],
        "sub_total": f"{int(row['price'] * row['qty']):,}",
        "total_qty": total_qty,
        "total_price": f"{int(total_price):,}"
    })


@router.get("/increase/{item_id}")
async def increase_item_quantity(request: Request, item_id: int):
    """
    Increase the quantity of an item in the cart.
    """
    item_key = str(item_id)
    if item_key in request.session["cart"]:
        request.session["cart"][item_key]['qty'] += 1
        logger.debug(f"Increased quantity of item {item_id}")
    
    row = request.session["cart"][item_key]
    total_qty, total_price = get_cart_total(request.session["cart"])
    return templates.TemplateResponse("home/_sub_total.html", {
        "request": request,
        "row": row,
        "qty": row['qty'],
        "sub_total": f"{int(row['price'] * row['qty']):,}",
        "total_qty": total_qty,
        "total_price": f"{int(total_price):,}"
    })
