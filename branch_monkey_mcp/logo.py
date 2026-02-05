"""
Kompany ASCII art logo.

Block-pixel font rendered with full-block characters (U+2588).
Each letter is 4-5 chars wide, 5 lines tall, with 2-space gaps.

To preview:
    python -m branch_monkey_mcp.logo
"""

B = "\u2588"  # █ full block

# fmt: off
LOGO = [
    f"{B}  {B}  {B}{B}{B}{B}  {B}   {B}  {B}{B}{B}{B}   {B}{B}   {B}   {B}  {B}   {B}",
    f"{B} {B}   {B}  {B}  {B}{B} {B}{B}  {B}  {B}  {B}  {B}  {B}{B}  {B}   {B} {B} ",
    f"{B}{B}    {B}  {B}  {B} {B} {B}  {B}{B}{B}{B}  {B}{B}{B}{B}  {B} {B} {B}    {B}  ",
    f"{B} {B}   {B}  {B}  {B}   {B}  {B}     {B}  {B}  {B}  {B}{B}    {B}  ",
    f"{B}  {B}  {B}{B}{B}{B}  {B}   {B}  {B}     {B}  {B}  {B}   {B}    {B}  ",
]
# fmt: on

LOGO_WIDTH = max(len(line) for line in LOGO)
LOGO_HEIGHT = len(LOGO)


if __name__ == "__main__":
    for line in LOGO:
        print(line)
    print(f"\n({LOGO_WIDTH} x {LOGO_HEIGHT})")
