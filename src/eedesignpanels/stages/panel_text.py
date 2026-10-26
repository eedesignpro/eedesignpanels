"""Place native KiCad part-number text on the panel rails."""

import math
import string
import uuid

from eedesigncore import PcbText, ProjectFile, TextAttributes, items_bbox
from eedesigncore.common.font import default_font

from eedesignpanels.toolpath import padded_bbox

RAIL_CHOICES = ("automatic", "left", "right", "top", "bottom",
                "vertical", "horizontal", "all", "none")
PART_NUMBER_FIELDS = ("PART_ID", "ARRAY_TABULATION", "PCB_REVISION")
TEXT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "eedesignpro/eedesignpanels/rail-text")
STROKE_RATIO = 1.0 / 8.0
MAX_PART_NUMBER_FIELD_LENGTH = 256


def read_sspn(path):
    """Read the source project's own text variables through eedesigncore."""
    if not path.is_file():
        return "", "Panel text is disabled because the source project file is missing."
    variables = ProjectFile.from_file(path).root.get_object("text_variables")
    values = [variables.get(key) if variables is not None else None
              for key in PART_NUMBER_FIELDS]
    missing = [key for key, value in zip(PART_NUMBER_FIELDS, values)
               if not isinstance(value, str) or not value.strip()]
    if missing:
        return "", "Panel text is disabled. Missing project values: " + ", ".join(missing) + "."
    if any(len(value) > MAX_PART_NUMBER_FIELD_LENGTH or not value.isprintable() for value in values):
        return "", "Panel text is disabled because a part number value is too long or contains a nonprinting character."
    return "-".join(value.strip() for value in values), ""


def selected_rails(settings, rails):
    """Resolve the requested rails without inventing material."""
    available = tuple(side for side in ("left", "right", "top", "bottom")
                      if getattr(rails, side) > 0.0)
    choice = settings.rails
    if choice == "automatic":
        return available[:1]
    choices = {
        "vertical": ("left", "right"), "horizontal": ("top", "bottom"),
        "all": ("left", "right", "top", "bottom"), "none": (),
    }.get(choice, (choice,))
    return tuple(side for side in choices if side in available)


def envelope_factors(content):
    """Treat every character as the widest part-number glyph, including its stroke."""
    font = default_font()
    alphabet = set(string.ascii_letters + string.digits + "-?" + content)
    advance = max(font.glyph(char).advance for char in alphabet)
    # Native font metrics include descenders and KiCad's text-box allowance.
    height = max(font.text_box(char, (1_000_000, 1_000_000), 125_000).height
                 for char in alphabet) / 1_000_000.0
    return len(content) * advance + STROKE_RATIO, height + STROKE_RATIO


def rail_coordinates(box, side, blank):
    """Return inward and along-rail bounds measured from the panel edge."""
    x0, y0, x1, y1 = box
    if side == "left":
        return x0 - blank.min_x, x1 - blank.min_x, y0, y1
    if side == "right":
        return blank.max_x - x1, blank.max_x - x0, y0, y1
    if side == "top":
        return y0 - blank.min_y, y1 - blank.min_y, x0, x1
    return blank.max_y - y1, blank.max_y - y0, x0, x1


def native_center_offset(content, height, vertical, mirror):
    """Locate the native glyph center relative to KiCad's text anchor."""
    size = round(height * 1_000_000)
    runs = default_font().text_strokes(
        content, (size, size), (0, 0), round(size * STROKE_RATIO),
        angle=90.0 if vertical else 0.0, mirror=mirror,
    )
    points = [point for run in runs for point in run]
    if not points:
        return 0.0, 0.0
    return tuple((min(point[axis] for point in points)
                  + max(point[axis] for point in points)) / 2_000_000.0
                 for axis in (0, 1))


def obstacle_boxes(base, footprints, layer):
    """Use known routed widths and carrier footprint extents, without collision tests."""
    boxes = [padded_bbox(piece, channel.radius)
             for channel in base.cut_channels for piece in channel.path]
    front = layer == "F.SilkS"
    layers = {"F.Cu", "F.Mask", "F.SilkS", "F.CrtYd"} if front else {
        "B.Cu", "B.Mask", "B.SilkS", "B.CrtYd"}
    for footprint in footprints:
        box = items_bbox([footprint], layers=layers)
        if box is not None:
            boxes.append((box.min_x, box.min_y, box.max_x, box.max_y))
    return boxes


def apply(board, base, settings, content, footprints=(), warnings=None):
    """Add centered, rail-aligned gr_text items to the panel in memory."""
    warnings = warnings if warnings is not None else []
    if not settings.enabled or settings.rails == "none":
        return ()
    sides = selected_rails(settings, base.rails)
    if not sides:
        warnings.append("Panel text is disabled because no selected rail exists.")
        return ()
    if not content:
        return ()
    length_factor, depth_factor = envelope_factors(content)
    blank = base.blank
    obstacles = obstacle_boxes(base, footprints, settings.layer)
    made = []
    for side in sides:
        vertical = side in ("left", "right")
        low, high = ((blank.min_y, blank.max_y) if vertical
                     else (blank.min_x, blank.max_x))
        center = (low + high) / 2.0
        clearance = settings.edge_clearance_mm
        maximum = min((getattr(base.rails, side) - clearance) / depth_factor,
                      (high - low - 2.0 * clearance) / length_factor)
        for box in obstacles:
            inward0, inward1, along0, along1 = rail_coordinates(box, side, blank)
            if inward1 < clearance:
                continue
            along_gap = max(along0 - center, center - along1, 0.0)
            # Either a transverse gap or a longitudinal gap avoids this box.
            limit = max((inward0 - clearance) / depth_factor,
                        2.0 * along_gap / length_factor)
            maximum = min(maximum, limit)
        maximum = math.floor(maximum * 1_000_000.0) / 1_000_000.0
        height = settings.text_height_mm or maximum
        if settings.text_height_mm is not None and height > maximum:
            warnings.append(
                f"Panel text on the {side} rail exceeds the available space. The height override remains visible."
            )
        elif height <= 0.0:
            warnings.append(
                f"Panel text is disabled on the {side} rail because the lettering does not fit."
            )
            continue
        inward = clearance + height * depth_factor / 2.0
        position = {
            "left": (blank.min_x + inward, center),
            "right": (blank.max_x - inward, center),
            "top": (center, blank.min_y + inward),
            "bottom": (center, blank.max_y - inward),
        }[side]
        offset = native_center_offset(content, height, vertical, settings.layer == "B.SilkS")
        position = (position[0] - offset[0], position[1] - offset[1])
        item = PcbText(
            content=content, position=position, angle=90.0 if vertical else 0.0,
            layer=settings.layer,
            attributes=TextAttributes(
                font_size=(height, height), font_thickness=height * STROKE_RATIO,
                mirror=settings.layer == "B.SilkS",
            ),
            id=str(uuid.uuid5(TEXT_NAMESPACE, side)),
        )
        board.drawings.append(item)
        made.append(item)
    return tuple(made)
