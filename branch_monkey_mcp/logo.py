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
    Uses 2D noise so the glow moves organically in both axes,
    like light from a shifting orb behind the text.

    The glow has a focal point that drifts slowly across the logo,
    and characters near the focal point are brighter.
    """
    t = frame * 0.06  # time in ~seconds at ~7fps

    # Drifting focal point (orb position)
    orb_x = (math.sin(t * 0.3) * 0.5 + 0.5) * width
    orb_y = (math.sin(t * 0.2 + 1.0) * 0.5 + 0.5) * LOGO_HEIGHT

    attrs = []
    for x in range(width):
        # Distance from orb center (normalized)
        dx = (x - orb_x) / max(width, 1)
        dy = (row - orb_y) / max(LOGO_HEIGHT, 1)
        dist = math.sqrt(dx * dx + dy * dy)

        # Orb glow falloff — brighter near center
        glow = max(0.0, 1.0 - dist * 2.5)
        glow = glow * glow  # quadratic falloff for soft edge

        # Add noise turbulence for organic feel
        noise = _noise2d(x * 0.15 + t * 0.5, row * 0.5 + t * 0.3)

        # Combine: orb glow modulated by noise
        brightness = glow * 0.7 + noise * 0.3

        # Pulse the orb core gently
        pulse = (math.sin(t * 2.0) * 0.5 + 0.5) * 0.15
        brightness += pulse * glow

        attrs.append(max(0.0, min(1.0, brightness)))

    return attrs


if __name__ == "__main__":
    for line in LOGO:
        print(line)
    print(f"\n({LOGO_WIDTH} x {LOGO_HEIGHT})")
