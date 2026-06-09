"""Простая bearer-token авторизация. Токен берём из env API_TOKEN."""
import os
import secrets

from fastapi import Header, HTTPException, status

API_TOKEN = os.getenv("API_TOKEN", "")


def require_token(authorization: str = Header(default="")) -> None:
    if not API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_TOKEN env var is not set on server",
        )
    expected = f"Bearer {API_TOKEN}"
    if not secrets.compare_digest(authorization or "", expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
