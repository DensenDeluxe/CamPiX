"""Motion score that tolerates light changes.

The detector compares each analysis frame with a slow background, after removing
one global luminance offset. A sudden exposure change or an IR cut therefore
does not look like motion. Pixels that are actually changing are held back from
the background so a moving subject is not absorbed immediately. A fixed 16×9
ignore mask drops trees, clocks, and other permanently busy cells.
"""

from __future__ import annotations

import numpy as np

MASK_COLUMNS = 16
MASK_ROWS = 9
MASK_CELLS = MASK_COLUMNS * MASK_ROWS


def analysis_mask(shape, regions, cells: str = ""):
    """Boolean mask of pixels that contribute to the score."""
    height, width = shape
    if regions:
        active = np.zeros(shape, dtype=bool)
        for region in regions:
            x0, y0 = int(region.x * width), int(region.y * height)
            x1 = min(width, int((region.x + region.w) * width))
            y1 = min(height, int((region.y + region.h) * height))
            if x1 > x0 and y1 > y0:
                active[y0:y1, x0:x1] = True
        if not active.any():
            active = np.ones(shape, dtype=bool)
    else:
        active = np.ones(shape, dtype=bool)
    if cells:
        ignored = np.zeros(shape, dtype=bool)
        for index, bit in enumerate(cells):
            if bit != "1":
                continue
            row, column = divmod(index, MASK_COLUMNS)
            y0 = row * height // MASK_ROWS
            y1 = (row + 1) * height // MASK_ROWS
            x0 = column * width // MASK_COLUMNS
            x1 = (column + 1) * width // MASK_COLUMNS
            if y1 > y0 and x1 > x0:
                ignored[y0:y1, x0:x1] = True
        active &= ~ignored
    return active


def activity_cells(changed, active) -> str:
    """16×9 map of cells whose changed samples cover at least two percent of the cell."""
    height, width = changed.shape
    bits = []
    for index in range(MASK_CELLS):
        row, column = divmod(index, MASK_COLUMNS)
        y0, y1 = row * height // MASK_ROWS, (row + 1) * height // MASK_ROWS
        x0, x1 = column * width // MASK_COLUMNS, (column + 1) * width // MASK_COLUMNS
        cell = changed[y0:y1, x0:x1] & active[y0:y1, x0:x1]
        bits.append("1" if cell.size and float(np.mean(cell)) >= 0.02 else "0")
    return "".join(bits)


def motion_score(previous, gray, pixel_delta: int, regions, cells: str = "") -> float:
    """Fraction of changed samples. Regions and the ignore mask stay inside the frame."""
    changed = np.abs(gray.astype(np.int16) - previous.astype(np.int16)) > pixel_delta
    active = analysis_mask(gray.shape, regions, cells)
    if not active.any():
        return 0.0
    return float(np.mean(changed[active]))


class BackgroundModel:
    """Slow background with a single-frame global light correction."""

    def __init__(self, alpha: float = 0.05, hold_alpha: float = 0.002):
        if not 0 <= hold_alpha <= alpha <= 1:
            raise ValueError("Motion background rates must be ordered between 0 and 1")
        self.alpha = float(alpha)
        self.hold_alpha = float(hold_alpha)
        self.background = None
        self.last_cells = "0" * MASK_CELLS

    def reset(self):
        self.background = None
        self.last_cells = "0" * MASK_CELLS

    def observe(self, gray, pixel_delta: int, regions, cells: str = "") -> float:
        """Return the changed fraction. The first frame only establishes the background."""
        sample = np.asarray(gray)
        if sample.dtype != np.uint8 or sample.ndim != 2:
            raise ValueError("Motion analysis expects one grayscale frame")
        if self.background is None or self.background.shape != sample.shape:
            self.background = sample.astype(np.float32)
            return 0.0
        active = analysis_mask(sample.shape, regions, cells)
        if not active.any():
            return 0.0
        current = sample.astype(np.float32)
        residual = current - self.background
        offset = float(np.median(residual[active]))
        changed = np.abs(residual - offset) > pixel_delta
        score = float(np.mean(changed[active]))
        self.last_cells = activity_cells(changed, active)
        # Apply the measured light shift everywhere, then let stable pixels ease
        # toward the new frame. Moving pixels stay until they have been still.
        adapted = self.background + np.float32(offset)
        moving = changed & active
        step = np.where(moving, np.float32(self.hold_alpha), np.float32(self.alpha))
        self.background = (1.0 - step) * adapted + step * current
        return score
