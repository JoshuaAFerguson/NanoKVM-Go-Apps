#!/usr/bin/env python3
"""Smoke-test App: proves this repo installs and runs from the Store."""

from appbase import CYAN, GRAY, WHITE, AppContext, Rect, app

HIDDEN_EDGE_PX = 14  # panel cannot display these rows/columns


def visible_rect(ctx: AppContext) -> Rect:
    """Area the panel actually shows for the current host rotation."""
    fb = ctx.fb
    if fb.rotate == 90:
        return Rect(0, 0, fb.width, fb.height - HIDDEN_EDGE_PX)
    if fb.rotate == 270:
        return Rect(0, HIDDEN_EDGE_PX, fb.width, fb.height - HIDDEN_EDGE_PX)
    if fb.rotate == 180:
        return Rect(0, 0, fb.width - HIDDEN_EDGE_PX, fb.height)
    return Rect(HIDDEN_EDGE_PX, 0, fb.width - HIDDEN_EDGE_PX, fb.height)


@app()
def main(ctx: AppContext) -> None:
    view = visible_rect(ctx)
    version = main.__app_info__.version
    state = {"taps": 0}

    def on_tap(x: int, y: int) -> None:
        state["taps"] += 1

    def tick(dt: float) -> None:
        ctx.fb.clear(0)
        ctx.fb.text_center(view.cx, view.cy - 40, "HELLO", CYAN, 3)
        ctx.fb.text_center(view.cx, view.cy - 8, "NANOKVM", WHITE, 2)
        ctx.fb.text_center(view.cx, view.cy + 24, f"taps: {state['taps']}", WHITE, 2)
        ctx.fb.text_center(view.cx, view.y + view.h - 16, f"v{version}", GRAY)

    ctx.run(tick, fps=10, on_tap=on_tap)


if __name__ == "__main__":
    main()
