"""Pure, Telegram-independent catalog UI domain."""

from app.catalog_ui.callbacks import decode_callback, encode_callback, resolve_callback
from app.catalog_ui.models import (
    CatalogAction,
    CatalogActionKind,
    CatalogScreen,
    CatalogUiState,
    NavigationContext,
    PendingRender,
    RenderedButton,
    RenderedScreen,
)
from app.catalog_ui.navigation import navigate, normalize_offset
from app.catalog_ui.rendering import acknowledge_render, prepare_render, render_screen

__all__ = [
    "CatalogAction",
    "CatalogActionKind",
    "CatalogScreen",
    "CatalogUiState",
    "NavigationContext",
    "PendingRender",
    "RenderedButton",
    "RenderedScreen",
    "acknowledge_render",
    "decode_callback",
    "encode_callback",
    "navigate",
    "normalize_offset",
    "prepare_render",
    "render_screen",
    "resolve_callback",
]
