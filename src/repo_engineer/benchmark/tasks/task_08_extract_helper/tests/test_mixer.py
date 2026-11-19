from mixer import color, util, volume


def test_one_shared_helper():
    assert color.clamp is util.clamp
    assert volume.clamp is util.clamp


def test_clamp():
    assert util.clamp(5, 0, 3) == 3
    assert util.clamp(-1, 0, 3) == 0
    assert util.clamp(2, 0, 3) == 2


def test_behaviour_is_unchanged():
    assert color.brighten(250, 10) == 255
    assert color.brighten(10, 5) == 15
    assert volume.louder(0.95) == 1.0
    assert abs(volume.louder(0.2) - 0.3) < 1e-9
