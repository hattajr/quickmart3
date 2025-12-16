"""
Shopping cart related utility functions.
"""
from typing import Dict, Any, Tuple


def get_cart_total(cart: Dict[str, Any]) -> Tuple[int, float]:
    """
    Calculate total quantity and total price from cart items.
    """
    total_qty = sum(item['qty'] for item in cart.values())
    total_price = sum(item['price'] * item['qty'] for item in cart.values())
    return total_qty, total_price
