"""Self-contained browser tab icons for the board and panel views."""

from urllib.parse import quote


def _icon(shapes: str) -> str:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">{shapes}</svg>'
    return (
        '<link rel="icon" type="image/svg+xml" sizes="any" '
        f'href="data:image/svg+xml,{quote(svg, safe="")}">'
    )


BOARD_ICON = _icon(
    '<rect x="3" y="3" width="26" height="26" rx="3" fill="#16834b"/>'
    '<path d="M8 9h8v7h8M8 23h8v-7" fill="none" stroke="#f5cf65" '
    'stroke-width="2"/>'
    '<g fill="#f5cf65"><circle cx="8" cy="9" r="2"/>'
    '<circle cx="8" cy="23" r="2"/><circle cx="24" cy="16" r="2"/></g>'
    '<rect x="12" y="12" width="8" height="8" rx="1" fill="#193c30"/>'
)

PANEL_ICON = _icon(
    '<rect x="1" y="3" width="30" height="26" rx="2" fill="#f5cf65"/>'
    '<rect x="3" y="6" width="26" height="20" fill="#193c30"/>'
    '<path d="M5 8h6v16H5zM13 8h6v16h-6zM21 8h6v16h-6z" fill="#22a965"/>'
    '<path d="M8 11v10M16 11v10M24 11v10" stroke="#f5cf65" '
    'stroke-width="2" stroke-dasharray="3 4"/>'
)
