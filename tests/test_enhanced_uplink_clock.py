"""Exercise the production continuity mode with two independent capture clocks."""
import ctypes as C

import pytest

from bluehead.uplink import CallRouter
from . import test_uplink_clock as clocks


class EnhancedRouter(CallRouter):
    def configure(self, rate, *, adaptive=False):
        super().configure(rate, adaptive=adaptive)
        self.api.bh_up_continuity.argtypes = [C.c_void_p, C.c_int]
        self.api.bh_up_continuity.restype = C.c_int
        assert self.api.bh_up_continuity(self.handle, 1) == 0


@pytest.mark.parametrize("ppm", [-500, 500])
def test_production_two_capture_clocks_30_virtual_minutes(ppm, tmp_path, monkeypatch):
    monkeypatch.setattr(clocks, "CallRouter", EnhancedRouter)
    clocks.test_dual_capture_clocks_30_minutes_no_bulk_drop_or_underrun(ppm, tmp_path)
