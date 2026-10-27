"""Bound memory consumed by discovery and incomplete periodic advertisements."""

from collections import OrderedDict
import types


class DiscoveryCache(OrderedDict):
    maximum = 128

    def get(self, key, default=None):
        value = super().get(key, default)
        if key in self:
            self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.maximum:
            self.popitem(last=False)


def bound_periodic_fragments(pa):
    original = getattr(pa, "on_periodic_advertising_report", None)
    if original is None or getattr(pa, "_bluehead_bounded", False):
        return
    pa._bluehead_bounded = True
    discarding = False

    def report(owner, value):
        nonlocal discarding
        more = int(value.data_status) == 1
        if discarding or len(owner.data_accumulator) + len(value.data) > 1650:
            owner.data_accumulator = b""
            discarding = more
            return
        original(value)

    pa.on_periodic_advertising_report = types.MethodType(report, pa)
