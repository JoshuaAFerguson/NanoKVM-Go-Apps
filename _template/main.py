#!/usr/bin/env python3
"""Starter App. Copy _template/ to a new kebab-case directory and edit app.json."""

from appbase import WHITE, AppContext, Rect, app

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

    def tick(dt: float) -> None:
        ctx.fb.clear(0)
        ctx.fb.text_center(view.cx, view.cy - 8, "MY APP", WHITE, 2)

    ctx.run(tick, fps=10)


if __name__ == "__main__":
    main()
