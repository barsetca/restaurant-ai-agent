import re

from app.catalog_ui.models import CatalogAction, CatalogActionKind, CatalogUiState

PROTOCOL_PREFIX = "cat"
PROTOCOL_VERSION = 1
MAX_CALLBACK_BYTES = 64
_CALLBACK_RE = re.compile(r"^cat:([0-9]+):([A-Za-z0-9_-]{8,16}):([0-9a-z]{1,2})$")


def _base36(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number < 0:
        raise ValueError("negative callback index")
    result = "0"
    if number:
        chars: list[str] = []
        while number:
            number, remainder = divmod(number, 36)
            chars.append(alphabet[remainder])
        result = "".join(reversed(chars))
    return result


def encode_callback(token: str, index: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,16}", token):
        raise ValueError("invalid UI token")
    payload = f"{PROTOCOL_PREFIX}:{PROTOCOL_VERSION}:{token}:{_base36(index)}"
    if len(payload.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("callback payload exceeds Telegram limit")
    return payload


def decode_callback(payload: object) -> tuple[str, int] | None:
    if (
        not isinstance(payload, str)
        or len(payload.encode("utf-8")) > MAX_CALLBACK_BYTES
    ):
        return None
    match = _CALLBACK_RE.fullmatch(payload)
    if match is None or int(match.group(1)) != PROTOCOL_VERSION:
        return None
    try:
        index = int(match.group(3), 36)
    except ValueError:
        return None
    return match.group(2), index


def resolve_callback(
    state: CatalogUiState,
    payload: object,
    *,
    message_id: int | None = None,
    draft_revision: int | None = None,
) -> CatalogAction | None:
    """Resolve only the acknowledged active registry; pending actions have no authority."""
    decoded = decode_callback(payload)
    if decoded is None or state.active_token is None:
        return None
    token, index = decoded
    if token != state.active_token or index >= len(state.active_actions):
        return None
    action = state.active_actions[index]
    if state.pending_render is not None and action.kind in {
        CatalogActionKind.INCREMENT,
        CatalogActionKind.DECREMENT,
    }:
        return None
    if action.requires_acknowledged_screen and (
        state.active_message_id is None
        or message_id != state.active_message_id
        or action.draft_revision is None
        or draft_revision != action.draft_revision
    ):
        return None
    return action
