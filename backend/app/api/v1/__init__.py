"""Versioned public API. Feature routers are added here as each phase lands."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")

__all__ = ["router"]
