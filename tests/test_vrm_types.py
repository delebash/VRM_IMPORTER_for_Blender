import sys
from typing import Sequence, cast
from unittest import TestCase

from io_scene_vrm import vrm_types


class TestVrmTypes(TestCase):
    def test_normalize_weights_compatible_with_gl_float(self) -> None:
        for arg, expected in [
            ([1, 0, 0, 0], [1, 0, 0, 0]),
            ([2, 0, 0, 0], [1, 0, 0, 0]),
            ([1, 3, 0, 0], [0.25, 0.75, 0, 0]),
            ([2, 2, 2, 2], [0.25, 0.25, 0.25, 0.25]),
            ([0, 0, 0, sys.float_info.epsilon], [0, 0, 0, 1]),
            ([0, sys.float_info.epsilon, 0, sys.float_info.epsilon], [0, 0.5, 0, 0.5]),
        ]:
            with self.subTest(arg):
                actual = vrm_types.normalize_weights_compatible_with_gl_float(
                    cast(Sequence[float], arg)
                )
                self.assertEqual(
                    expected, actual, f"Expected: {expected}, Actual: {actual}"
                )
