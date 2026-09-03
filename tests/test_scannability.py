from photoar.scannability import STAR_FLOORS, stars


def test_star_floors_match_the_thresholds_they_come_from():
    # 40 = verify.MIN_INLIERS（识别门槛）；65 ≈ 去重混淆门槛；真实照片中位 ≈100。
    assert dict(STAR_FLOORS) == {130: 5, 90: 4, 65: 3, 40: 2}


def test_stars_is_monotone_and_clamped():
    assert stars(0) == 1
    assert stars(39) == 1
    assert stars(40) == 2
    assert stars(64) == 2
    assert stars(65) == 3
    assert stars(89) == 3
    assert stars(90) == 4
    assert stars(129) == 4
    assert stars(130) == 5
    assert stars(10_000) == 5
    assert stars(-5) == 1
