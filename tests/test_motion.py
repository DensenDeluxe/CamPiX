import numpy as np
import pytest

from campix.models import CameraConfig, MotionRegion
from campix.motion import MASK_CELLS, BackgroundModel, motion_score


def test_background_ignores_a_global_light_change_and_keeps_local_motion():
    model = BackgroundModel()
    base = np.full((90, 160), 30, dtype=np.uint8)
    assert model.observe(base, 25, []) == 0
    bright = np.full_like(base, 180)
    assert model.observe(bright, 25, []) == 0
    moving = bright.copy()
    moving[:30, :40] = 20
    score = model.observe(moving, 25, [])
    assert score > 0.08
    assert model.observe(moving, 25, []) == pytest.approx(score)
    fast = BackgroundModel(alpha=1, hold_alpha=1)
    assert fast.observe(bright, 25, []) == 0
    assert fast.observe(moving, 25, []) > 0.08
    assert fast.observe(moving, 25, []) == 0


def test_background_absorbs_a_gradual_light_ramp():
    model = BackgroundModel()
    frame = np.full((20, 20), 10, dtype=np.uint8)
    assert model.observe(frame, 25, []) == 0
    for level in range(12, 80, 2):
        frame = np.full((20, 20), level, dtype=np.uint8)
        assert model.observe(frame, 25, []) == 0


def test_ignore_mask_drops_only_the_selected_cells():
    model = BackgroundModel()
    base = np.zeros((90, 160), dtype=np.uint8)
    model.observe(base, 25, [])
    masked = base.copy()
    masked[:10, :10] = 255
    cells = "1" + "0" * (MASK_CELLS - 1)
    assert model.observe(masked, 25, [], cells) == 0
    model.reset()
    model.observe(base, 25, [])
    visible = base.copy()
    visible[:10, 10:20] = 255
    assert model.observe(visible, 25, [], cells) > 0


def test_regions_and_mask_compose_and_direct_score_stays_compatible():
    previous = np.zeros((10, 10), dtype=np.uint8)
    gray = np.zeros((10, 10), dtype=np.uint8)
    gray[:, 5:] = 255
    assert motion_score(previous, gray, 25, []) == pytest.approx(0.5)
    assert motion_score(previous, gray, 25, [MotionRegion(x=0, y=0, w=0.4, h=1)]) == 0
    model = BackgroundModel()
    wide = np.zeros((90, 160), dtype=np.uint8)
    model.observe(wide, 25, [])
    changed = wide.copy()
    changed[:, 80:] = 255
    region = [MotionRegion(x=0, y=0, w=0.4, h=1)]
    assert model.observe(changed, 25, region) == 0


def test_motion_mask_and_audio_names_are_rejected_without_echoing_input():
    with pytest.raises(ValueError, match="144 characters"):
        CameraConfig(id="cam", name="Cam", backend="synthetic", motion_mask="01")
    with pytest.raises(ValueError, match="at least one cell"):
        CameraConfig(id="cam", name="Cam", backend="synthetic", motion_mask="1" * MASK_CELLS)
    with pytest.raises(ValueError, match="without quotes, colons"):
        CameraConfig(
            id="cam",
            name="Cam",
            backend="dshow",
            source="Webcam",
            audio_enabled=True,
            audio_source='Mic":audio=Other',
        )
    with pytest.raises(ValueError, match="accepts no device"):
        CameraConfig(
            id="cam",
            name="Cam",
            backend="synthetic",
            audio_enabled=True,
            audio_source="hw:1",
        )
    saved = CameraConfig(
        id="cam",
        name="Cam",
        backend="synthetic",
        motion_mask="0" * MASK_CELLS,
        audio_enabled=True,
    )
    assert saved.motion_mask == ""
    assert saved.audio_enabled is True
    assert saved.audio_source == ""
