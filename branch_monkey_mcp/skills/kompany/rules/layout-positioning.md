# Layout & Positioning

Constants and math for placing elements on the canvas.

## Size constants
| Constant | Value | Description |
|----------|-------|-------------|
| M_W | 240 | System width (px) |
| M_H | 160 | System height (px) |
| PAD | 40 | Domain padding (sides + bottom) |
| PAD_TOP | 70 | Domain top padding (title area) |
| GAP | 50 | Gap between domains |
| M_GAP | 40 | Gap between systems |
| NOTE_W | 220 | Note width |
| NOTE_H | 180 | Note height |
| NOTE_GAP | 40 | Gap between notes and domains |

## Domain sizing formulas

**Height** for N vertically-stacked systems:
```
domain_height(n) = M_H * n + PAD_TOP + PAD + M_GAP * (n - 1)
```
- 1 system: 160 + 70 + 40 = 270
- 2 systems: 320 + 70 + 40 + 40 = 470
- 3 systems: 480 + 70 + 40 + 80 = 670

**Width** for N side-by-side systems:
```
domain_width(n) = M_W * n + PAD * 2 + M_GAP * (n - 1)
```
- 1 system: 240 + 80 = 320
- 2 systems: 480 + 80 + 40 = 600

## System positioning inside a domain

Systems stack vertically inside their domain:
```
machine_x = domain_x + PAD          (= domain_x + 40)
machine_y = domain_y + PAD_TOP + idx * (M_H + M_GAP)
          = domain_y + 70 + idx * 200
```

Where `idx` is 0-based index of the system within the domain.

## Domain layout (horizontal flow)

Place domains left-to-right with GAP spacing:
```
domain_0_x = 0
domain_1_x = domain_0_width + GAP
domain_2_x = domain_1_x + domain_1_width + GAP
```

All domains typically start at y=0.

## Notes positioning

Place notes to the right of the last domain:
```
notes_x = last_domain_x + last_domain_width + NOTE_GAP
note_y  = idx * (NOTE_H + 20)
```

## Color palette
| Color | Hex | Common use |
|-------|-----|------------|
| Purple | #8b5cf6 | Marketing, Awareness |
| Blue | #3b82f6 | Sales, Acquisition |
| Green | #22c55e | Delivery, Activation |
| Amber | #f59e0b | Revenue, Operations |
| Red | #ef4444 | Retention, Alerts |
| Indigo | #6366f1 | Default accent |

### Note background colors
| Color | Hex |
|-------|-----|
| White | #f8fafc |
| Light purple | #ddd6fe |
| Light blue | #bfdbfe |
| Light green | #bbf7d0 |
| Light pink | #fecdd3 |
| Light orange | #fed7aa |
| Yellow | #fef08a |
