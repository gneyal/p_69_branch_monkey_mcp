"""
Kompany ASCII art logo.

Lowercase block-pixel font, 6 lines tall.
Animated with organic Perlin-like noise glow (inspired by Amp Code's orb effect).

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
_GAP = 1

# Build static logo lines
LOGO = []
for row in range(6):
    LOGO.append((" " * _GAP).join(letter[row] for letter in _LETTERS))

LOGO_WIDTH = max(len(line) for line in LOGO)
LOGO_HEIGHT = len(LOGO)


# ── Perlin-like noise for organic animation ──────────────────

def _noise2d(x: float, y: float) -> float:
    """
    Cheap 2D value noise using layered sines.
    Returns ~0.0–1.0 with organic-looking variation.
    """
    # Three octaves of offset sine waves for pseudo-noise
    v = 0.0
    v += math.sin(x * 0.7 + y * 1.3) * 0.5
    v += math.sin(x * 1.9 - y * 0.8 + 2.1) * 0.25
    v += math.sin(x * 0.4 + y * 2.7 + 5.3) * 0.25
    return (v + 1.0) / 2.0  # normalize to 0–1


def get_animated_attrs(frame: int, width: int, row: int = 0) -> list:
    """
    Return per-character glow intensity for one logo row.

    Returns a list of floats (0.0–1.0) representing brightness.

    A soft glow drifts slowly across the text. Most of the logo stays
    at a comfortable mid-brightness, with a gentle highlight region
    that moves organically. The effect is subtle — more "alive" than flashy.
    """
    t = frame * 0.04  # slower pace

    # Soft drifting highlight (wide, gentle)
    center = (math.sin(t * 0.25) * 0.5 + 0.5) * width

    attrs = []
    for x in range(width):
        # Wide gaussian-like glow around center
        dx = (x - center) / max(width * 0.4, 1)
        glow = math.exp(-dx * dx)  # 0–1, very smooth falloff

        # Subtle noise adds organic variation
        noise = _noise2d(x * 0.08 + t * 0.3, row * 0.4 + t * 0.2)

        # Base brightness is high (0.5), glow lifts it to ~0.9
        brightness = 0.45 + glow * 0.35 + noise * 0.1

        # Gentle global breathe
        breathe = math.sin(t * 1.2) * 0.05
        brightness += breathe

        attrs.append(max(0.0, min(1.0, brightness)))

    return attrs


if __name__ == "__main__":
    for line in LOGO:
        print(line)
    print(f"\n({LOGO_WIDTH} x {LOGO_HEIGHT})")
