"""
Kompany ASCII art logo.

Tall shadow-style font using box-drawing + block characters.
Each letter is 6-7 chars wide, 6 lines tall, with 1-space gaps.
Lowercase style with descenders on some letters.

To preview:
    python -m branch_monkey_mcp.logo
"""

import math

# ── letter definitions (6 lines tall) ────────────────────────

# fmt: off
_k = [
    "██     ",
    "██  ██ ",
    "█████  ",
    "██  ██ ",
    "██   ██",
    "       ",
]
_o = [
    "       ",
    " █████ ",
    "██   ██",
    "██   ██",
    " █████ ",
    "       ",
]
_m = [
    "         ",
    "███ ████ ",
    "██ ██ ██ ",
    "██ ██ ██ ",
    "██    ██ ",
    "         ",
]
_p = [
    "         ",
    "██████   ",
    "██   ██  ",
    "██████   ",
    "██       ",
    "██       ",
]
_a = [
    "       ",
    " █████ ",
    "    ██ ",
    " █████ ",
    "██████ ",
    "       ",
]
_n = [
    "        ",
    "██████  ",
    "██   ██ ",
    "██   ██ ",
    "██   ██ ",
    "        ",
]
_y = [
    "        ",
    "██   ██ ",
    " ██ ██  ",
    "  ███   ",
    " ██     ",
    "██      ",
]
# fmt: on

_LETTERS = [_k, _o, _m, _p, _a, _n, _y]
_GAP = 1  # space between letters

# Build static logo lines
LOGO = []
for row in range(6):
    LOGO.append((" " * _GAP).join(letter[row] for letter in _LETTERS))

LOGO_WIDTH = max(len(line) for line in LOGO)
LOGO_HEIGHT = len(LOGO)


# ── animation helpers ─────────────────────────────────────────

def get_animated_attrs(frame: int, width: int) -> list:
    """
    Return per-character attribute indices for one logo row.

    Returns a list of ints (0-2) indicating brightness level:
      0 = dim, 1 = normal, 2 = bright

    A sine wave sweeps left-to-right, creating a shimmering effect.
    """
    attrs = []
    for x in range(width):
        # Sine wave: period ~20 chars, moves right over time
        phase = (x / 10.0) - (frame / 3.0)
        val = (math.sin(phase) + 1.0) / 2.0  # 0.0 – 1.0
        if val > 0.7:
            attrs.append(2)  # bright
        elif val > 0.3:
            attrs.append(1)  # normal
        else:
            attrs.append(0)  # dim
    return attrs


if __name__ == "__main__":
    for line in LOGO:
        print(line)
    print(f"\n({LOGO_WIDTH} x {LOGO_HEIGHT})")
