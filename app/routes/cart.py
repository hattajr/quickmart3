"""
Shopping cart related route handlers.
"""

from fastapi import APIRouter

router: APIRouter = APIRouter()

# All cart operations moved to Alpine.js frontend
# Only /api/items/{id} endpoint remains in products.py
# Checkout endpoint moved to main.py as /finish
