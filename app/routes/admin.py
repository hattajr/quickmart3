"""
Admin routes for product management with stateless password authentication.
"""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from psycopg2 import IntegrityError

from app.config import ADMIN_PASSWORD
from app.db.database import get_pg_db


router: APIRouter = APIRouter()
templates: Jinja2Templates = Jinja2Templates(directory="app/templates")


def verify_password(password: str) -> bool:
    """
    Verify admin password.
    """
    return password == ADMIN_PASSWORD


@router.get("/admin/products", response_class=HTMLResponse)
async def admin_products_page(request: Request):
    """
    Render admin products page with password prompt.
    """
    return templates.TemplateResponse("admin/products.html", {"request": request})


@router.post("/admin/verify", response_class=HTMLResponse)
async def verify_admin_password(request: Request, password: str = Form(...)):
    """
    Verify password and return product list.
    """
    if not verify_password(password):
        return templates.TemplateResponse(
            "admin/_toast.html",
            {"request": request, "message": "Invalid password!", "type": "error"}
        )
    
    for conn in get_pg_db():
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, barcode, name, brand, price, unit, stock, description, keyword, image_url FROM products ORDER BY id ASC"
        )
        products = cursor.fetchall()
        
        return templates.TemplateResponse(
            "admin/_product_list.html",
            {"request": request, "products": products, "password": password, "sort_by": "id", "sort_order": "asc"}
        )


@router.post("/admin/products/search", response_class=HTMLResponse)
async def search_products(
    request: Request,
    password: str = Form(...),
    q: str = Form(""),
    sort_by: str = Form("id"),
    sort_order: str = Form("asc")
):
    """
    Search products by name with sorting.
    """
    if not verify_password(password):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    valid_columns = ["id", "name", "price", "stock"]
    if sort_by not in valid_columns:
        sort_by = "id"
    
    if sort_order not in ["asc", "desc"]:
        sort_order = "asc"
    
    for conn in get_pg_db():
        cursor = conn.cursor()
        query = f"""
            SELECT id, barcode, name, brand, price, unit, stock, description, keyword, image_url 
            FROM products 
            WHERE name ILIKE %s 
            ORDER BY {sort_by} {sort_order}
        """
        cursor.execute(query, (f"%{q}%",))
        products = cursor.fetchall()
        
        return templates.TemplateResponse(
            "admin/_product_table.html",
            {"request": request, "products": products, "password": password}
        )


@router.post("/admin/products/add", response_class=HTMLResponse)
async def add_product(
    request: Request,
    password: str = Form(...),
    barcode: str = Form(None),
    name: str = Form(...),
    brand: str = Form(None),
    price: int = Form(...),
    unit: str = Form(None),
    stock: int = Form(0),
    description: str = Form(None),
    keyword: str = Form(None),
    image_url: str = Form(None),
    sort_by: str = Form("id"),
    sort_order: str = Form("asc")
):
    """
    Add a new product.
    """
    if not verify_password(password):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Validate price and stock
    if price <= 0:
        return templates.TemplateResponse(
            "admin/_toast.html",
            {"request": request, "message": "Price must be greater than 0!", "type": "error"}
        )
    
    if stock < 0:
        return templates.TemplateResponse(
            "admin/_toast.html",
            {"request": request, "message": "Stock cannot be negative!", "type": "error"}
        )
    
    for conn in get_pg_db():
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO products (barcode, name, brand, price, unit, stock, description, keyword, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (barcode, name, brand, price, unit, stock, description, keyword, image_url)
            )
            conn.commit()
            logger.info(f"Product added: {name}")
            
            # Return updated table with success toast
            cursor.execute(
                f"SELECT id, barcode, name, brand, price, unit, stock, description, keyword, image_url FROM products ORDER BY {sort_by} {sort_order}"
            )
            products = cursor.fetchall()
            
            return templates.TemplateResponse(
                "admin/_product_list.html",
                {
                    "request": request,
                    "products": products,
                    "password": password,
                    "sort_by": sort_by,
                    "sort_order": sort_order,
                    "toast_message": "Product added successfully!",
                    "toast_type": "success"
                }
            )
        except IntegrityError as e:
            conn.rollback()
            if "products_name_key" in str(e):
                return templates.TemplateResponse(
                    "admin/_toast.html",
                    {"request": request, "message": "Product name already exists!", "type": "error"}
                )
            logger.error(f"Error adding product: {e}")
            return templates.TemplateResponse(
                "admin/_toast.html",
                {"request": request, "message": f"Database error: {str(e)}", "type": "error"}
            )


@router.post("/admin/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product(
    request: Request,
    product_id: int,
    password: str = Form(...),
    barcode: str = Form(None),
    name: str = Form(...),
    brand: str = Form(None),
    price: int = Form(...),
    unit: str = Form(None),
    stock: int = Form(0),
    description: str = Form(None),
    keyword: str = Form(None),
    image_url: str = Form(None),
    sort_by: str = Form("id"),
    sort_order: str = Form("asc")
):
    """
    Edit an existing product.
    """
    if not verify_password(password):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Validate price and stock
    if price <= 0:
        return templates.TemplateResponse(
            "admin/_toast.html",
            {"request": request, "message": "Price must be greater than 0!", "type": "error"}
        )
    
    if stock < 0:
        return templates.TemplateResponse(
            "admin/_toast.html",
            {"request": request, "message": "Stock cannot be negative!", "type": "error"}
        )
    
    for conn in get_pg_db():
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE products 
                SET barcode = %s, name = %s, brand = %s, price = %s, unit = %s, 
                    stock = %s, description = %s, keyword = %s, image_url = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (barcode, name, brand, price, unit, stock, description, keyword, image_url, product_id)
            )
            conn.commit()
            logger.info(f"Product updated: {name} (ID: {product_id})")
            
            # Return updated table with success toast
            cursor.execute(
                f"SELECT id, barcode, name, brand, price, unit, stock, description, keyword, image_url FROM products ORDER BY {sort_by} {sort_order}"
            )
            products = cursor.fetchall()
            
            return templates.TemplateResponse(
                "admin/_product_list.html",
                {
                    "request": request,
                    "products": products,
                    "password": password,
                    "sort_by": sort_by,
                    "sort_order": sort_order,
                    "toast_message": "Product updated successfully!",
                    "toast_type": "success"
                }
            )
        except IntegrityError as e:
            conn.rollback()
            if "products_name_key" in str(e):
                return templates.TemplateResponse(
                    "admin/_toast.html",
                    {"request": request, "message": "Product name already exists!", "type": "error"}
                )
            logger.error(f"Error updating product: {e}")
            return templates.TemplateResponse(
                "admin/_toast.html",
                {"request": request, "message": f"Database error: {str(e)}", "type": "error"}
            )


@router.delete("/admin/products/{product_id}", response_class=HTMLResponse)
async def delete_product(
    request: Request,
    product_id: int,
    password: str
):
    """
    Delete a product.
    """
    if not verify_password(password):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    for conn in get_pg_db():
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
            conn.commit()
            logger.info(f"Product deleted: ID {product_id}")
            
            # Return empty string to remove the row
            return ""
        except Exception as e:
            conn.rollback()
            logger.error(f"Error deleting product: {e}")
            return templates.TemplateResponse(
                "admin/_toast.html",
                {"request": request, "message": f"Error deleting product: {str(e)}", "type": "error"}
            )
