"""Pixel art for Pixel Pet: bodies, faces, icons. Each sprite is rows of palette
keys ('.' = transparent); colours come from per-form palettes."""

from __future__ import annotations

from ui import rgb565

OUTLINE = rgb565(26, 22, 38)
WHITE = rgb565(250, 250, 255)
INK = rgb565(30, 26, 44)

# B body, L highlight, D shade, S star, K outline (fixed), E/s egg shell/spots
PALETTES = {
    "egg": {"E": rgb565(246, 238, 220), "s": rgb565(120, 196, 160), "K": OUTLINE},
    "baby": {"B": rgb565(126, 222, 170), "L": rgb565(200, 250, 220), "D": rgb565(70, 170, 120), "K": OUTLINE},
    "kid": {"B": rgb565(110, 210, 200), "L": rgb565(190, 245, 240), "D": rgb565(60, 150, 150), "K": OUTLINE},
    "sparkle": {"B": rgb565(236, 120, 200), "L": rgb565(255, 200, 236), "D": rgb565(176, 70, 150),
                "S": rgb565(255, 214, 60), "K": OUTLINE},
    "chill": {"B": rgb565(100, 170, 255), "L": rgb565(190, 220, 255), "D": rgb565(60, 110, 200),
              "S": rgb565(60, 60, 80), "K": OUTLINE},
    "grumpy": {"B": rgb565(160, 160, 90), "L": rgb565(210, 210, 150), "D": rgb565(110, 110, 60),
               "S": rgb565(120, 100, 70), "K": OUTLINE},
}

EGG = (
    "....KKKK....",
    "...KEEEEK...",
    "..KEEEEEEK..",
    "..KEEsEEEK..",
    ".KEEEEEEEEK.",
    ".KEEEEEEssK.",
    ".KEEEEEEEsK.",
    "KEEsEEEEEEEK",
    "KEssEEEEEEEK",
    "KEEEEEEEEEEK",
    "KEEEEEEsEEEK",
    ".KEEEEEEEEK.",
    "..KEEEEEEK..",
    "...KKKKKK...",
)

BABY = (
    "....KKKK....",
    "..KKBBBBKK..",
    ".KBBLLBBBBK.",
    ".KBLLBBBBBK.",
    "KBBBBBBBBBBK",
    "KBBBBBBBBBBK",
    "KBBBBBBBBBBK",
    ".KBBBBBBBBK.",
    ".KDBBBBBBDK.",
    "..KKKKKKKK..",
)

KID = (
    "..KK......KK..",
    ".KBBK....KBBK.",
    ".KBBBKKKKBBBK.",
    ".KBBLLBBBBBBK.",
    "KBBLLBBBBBBBBK",
    "KBBBBBBBBBBBBK",
    "KBBBBBBBBBBBBK",
    "KBBBBBBBBBBBBK",
    "KBBBBBBBBBBBBK",
    ".KDBBBBBBBBDK.",
    "..KDDBBBBDDK..",
    "...KKKKKKKK...",
)

ADULT_BODY = (
    "....KKKKKKKK....",
    "..KKBBBBBBBBKK..",
    ".KBBLLBBBBBBBBK.",
    ".KBLLBBBBBBBBBK.",
    "KBBBBBBBBBBBBBBK",
    "KBBBBBBBBBBBBBBK",
    "KBBBBBBBBBBBBBBK",
    "KBBBBBBBBBBBBBBK",
    ".KDBBBBBBBBBBDK.",
    "..KDDBBBBBBDDK..",
    "...KKKDDDDKKK...",
    "...KK......KK...",
)
TOPPERS = {  # 16-wide rows drawn above the adult body
    "sparkle": (".......KK.......", "......KSSK......", ".......KK.......", "........K......."),
    "chill": ("................", "....KKKKKKKK....", "...KSSSSSSSSK...", "................"),
    "grumpy": ("................", "......K..K......", ".....K....K.....", "................"),
}

# Face anchors per body: (eye row, left eye col, right eye col, mouth row, mouth centre col)
FACES = {"baby": (4, 3, 7, 7, 6), "kid": (5, 3, 9, 8, 7), "adult": (4, 4, 10, 7, 8)}

ICONS = {
    "feed": ("..K.....", "...K.K..", ".RRRRR..", "RRRRRRR.", "RRRWRRR.", "RRRRRRR.", ".RRRRR..", "..RRR..."),
    "play": ("..YYYY..", ".YYOOYY.", "YYOOOOYY", "YOOYYOOY", "YOOYYOOY", "YYOOOOYY", ".YYOOYY.", "..YYYY.."),
    "wash": (".......C", "..CC..CW", ".CWWC.C.", ".CWWC...", "..CC.CC.", ".....CWC", "..C..CC.", ".CWC...."),
    "sleep": ("..MMM...", ".MMM....", "MMM.....", "MMM.....", "MMM.....", ".MMM...M", "..MMMMM.", "........"),
    "wake": ("...Y....", ".Y.Y.Y..", "..YYY...", "YYYYYYY.", "..YYY...", ".Y.Y.Y..", "...Y....", "........"),
    "heal": ("...RR...", "...RR...", ".RRRRRR.", ".RRRRRR.", "...RR...", "...RR...", "........", "........"),
    "heart": (".RR.RR..", "RRRRRRR.", "RRRRRRR.", ".RRRRR..", "..RRR...", "...R....", "........", "........"),
    "poop": ("...B....", "..BB....", ".BBBB...", ".BWBWB..", "BBBBBBB.", "BBBBBBB.", "........", "........"),
}
ICON_PALETTE = {"R": rgb565(240, 70, 80), "W": WHITE, "K": rgb565(80, 50, 30), "Y": rgb565(255, 205, 60),
                "O": rgb565(255, 130, 40), "C": rgb565(120, 200, 255), "M": rgb565(200, 190, 255),
                "B": rgb565(140, 90, 50)}


def body(stage: str, form: str) -> tuple[tuple[str, ...], dict, str]:
    """(rows, palette, face key) for a pet."""
    if stage == "egg":
        return EGG, PALETTES["egg"], ""
    if stage == "baby":
        return BABY, PALETTES["baby"], "baby"
    if stage == "kid":
        return KID, PALETTES["kid"], "kid"
    form = form if form in TOPPERS else "chill"
    return TOPPERS[form] + ADULT_BODY, PALETTES[form], "adult"


def face_offset(stage: str, form: str) -> int:
    """Rows added above the body (adult toppers), so face anchors shift down."""
    return len(TOPPERS.get(form if form in TOPPERS else "chill", ())) if stage == "adult" else 0


def runs(rows, palette: dict) -> list[tuple[int, int, int, int]]:
    """(row, col, length, colour) horizontal runs, for fast drawing with fill_rect."""
    out = []
    for r, row in enumerate(rows):
        c = 0
        while c < len(row):
            key = row[c]
            start = c
            while c < len(row) and row[c] == key:
                c += 1
            if key != "." and key in palette:
                out.append((r, start, c - start, palette[key]))
    return out
