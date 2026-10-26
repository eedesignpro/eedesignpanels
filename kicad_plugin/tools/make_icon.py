"""Generate the toolbar icon: plugins/icon.png.

A 2x3 array of boards with a routed gap between them - what the plugin does,
at 64x64. Matched to the working example (hdi_setup ships a 64x64 icon.png and
points icon_file_name at it).

Run only when the icon needs to change; the PNG is committed.
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 64
SCALE = 8  # draw large, downsample - cheap antialiasing
OUT = Path(__file__).resolve().parent.parent / "plugins" / "icon.png"

BOARD = (57, 213, 44, 255)      # green, #39d52cff
EDGE = (240, 240, 235, 255)     # routed outline
RAIL = (120, 128, 140, 255)     # panel frame


def main():
    s = SIZE * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = 4 * SCALE
    rail = 3 * SCALE
    d.rounded_rectangle(
        [margin, margin, s - margin, s - margin],
        radius=2 * SCALE, outline=RAIL, width=rail,
    )

    inner = margin + rail + 2 * SCALE
    span = s - 2 * inner
    cols, rows, gap = 2, 3, 3 * SCALE
    bw = (span - gap * (cols - 1)) / cols
    bh = (span - gap * (rows - 1)) / rows

    for c in range(cols):
        for r in range(rows):
            x = inner + c * (bw + gap)
            y = inner + r * (bh + gap)
            d.rounded_rectangle(
                [x, y, x + bw, y + bh],
                radius=SCALE, fill=BOARD, outline=EDGE, width=SCALE,
            )

    img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
