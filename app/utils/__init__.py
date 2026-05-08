"""
Utility functions for request handling and client information.
"""

import httpx
from fastapi import Request
from loguru import logger
from user_agents import parse


def get_client_ip(request: Request) -> str:
    """
    Extract client IP address from request headers or direct connection.
    """
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else ""


def get_user_agent(request: Request) -> str:
    """
    Extract user agent string from request headers.
    """
    return request.headers.get("User-Agent", "")


def parse_user_agent(request: Request) -> dict[str, str]:
    """
    Parse user agent to extract device, browser, OS information.
    """
    user_agent_string: str = get_user_agent(request)
    user_agent = parse(user_agent_string)

    device_type: str = "mobile" if user_agent.is_mobile else "tablet" if user_agent.is_tablet else "desktop"

    return {
        "user_agent": user_agent_string,
        "device_type": device_type,
        "browser": f"{user_agent.browser.family} {user_agent.browser.version_string}",
        "os": f"{user_agent.os.family} {user_agent.os.version_string}",
    }


async def get_country_from_ip(ip_address: str) -> str:
    """
    Get country code from IP address using ip-api.com service.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://ip-api.com/json/{ip_address}")
            data = response.json()
            return data.get("countryCode", "")
    except Exception as e:
        logger.warning(f"Failed to get country from IP {ip_address}: {e}")
        return ""
