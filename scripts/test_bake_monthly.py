"""The parts of bake-monthly.py that decide (STORAGE_REDESIGN_PLAN.md,
"Monthly bake", decision 22)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "bake_monthly", SCRIPTS / "bake-monthly.py"
)
bm = importlib.util.module_from_spec(spec)
sys.modules["bake_monthly"] = bm
spec.loader.exec_module(bm)

DAY = 86400


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-12-01", True),  # a Tuesday, day 1
        ("2027-01-05", True),  # a Tuesday, day 5
        ("2026-12-07", False),  # a Monday
        ("2026-12-08", False),  # the second Tuesday
        ("2027-06-01", True),
        ("2026-11-24", False),  # the fourth Tuesday
    ],
)
def test_first_tuesday(day, expected):
    assert bm.first_tuesday(dt.date.fromisoformat(day)) is expected


def test_first_tuesdays_are_at_most_35_days_apart_so_36_only_fires_after_a_miss():
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(800)]
    tuesdays = [d for d in days if bm.first_tuesday(d)]
    gaps = {(b - a).days for a, b in zip(tuesdays, tuesdays[1:], strict=False)}
    assert max(gaps) <= 35  # noqa: PLR2004
    assert min(gaps) >= 28  # noqa: PLR2004


def test_overdue():
    now = 1_800_000_000
    assert bm.overdue(None, now)
    assert not bm.overdue(now - 35 * DAY, now)
    assert bm.overdue(now - 37 * DAY, now)


def test_image_name_and_flavors_in_turn():
    assert (
        bm.image_name("vgpu", dt.date(2026, 12, 1)) == "morphocloud-share-vgpu-20261201"
    )
    assert [bm.flavor_for("vgpu", i) for i in range(3)] == [
        "g3.large",
        "g3.medium",
        "g3.large",
    ]
    assert [bm.flavor_for("regular", i) for i in range(3)] == [
        "g3.xl",
        "g4.xl",
        "g3.xl",
    ]


def test_lock_states(tmp_path):
    lock = tmp_path / "lock"
    assert bm.lock_state(lock) == ("free", {})
    lock.write_text(json.dumps({"pid": 4242, "started": "2026-12-01T08:00:00Z"}))
    assert bm.lock_state(lock, alive=lambda _p: True)[0] == "alive"
    state, info = bm.lock_state(lock, alive=lambda _p: False)
    assert state == "dead"
    assert info["started"] == "2026-12-01T08:00:00Z"
    lock.write_text("garbage")
    assert bm.lock_state(lock)[0] == "dead"


def _names(kind, *days):
    return [f"morphocloud-share-{kind}-{d}" for d in days]


def test_prune_keeps_the_image_in_use_the_one_before_and_newer_ones():
    images = _names("vgpu", "20260926", "20261116", "20261123", "20261201") + _names(
        "regular", "20260926", "20261123", "20261201"
    )
    in_use = {
        "vgpu": "morphocloud-share-vgpu-20261123",
        "regular": "morphocloud-share-regular-20261201",
    }
    assert bm.prune_choice(images, in_use, set()) == [
        "morphocloud-share-vgpu-20260926",  # older than the one before the one in use
        "morphocloud-share-regular-20260926",
    ]


def test_prune_never_deletes_an_image_a_server_still_uses():
    images = _names("vgpu", "20260926", "20261116", "20261123")
    in_use = {"vgpu": "morphocloud-share-vgpu-20261123", "regular": None}
    used = {"morphocloud-share-vgpu-20260926"}  # a shelved instance boots from it
    assert bm.prune_choice(images, in_use, used) == []


def test_prune_keeps_everything_when_the_variable_is_unknown():
    images = _names("vgpu", "20260926", "20261116", "20261123")
    assert bm.prune_choice(images, {"vgpu": None, "regular": None}, set()) == []
    assert bm.prune_choice(images, {"vgpu": "Featured-Ubuntu24"}, set()) == []


def test_prune_ignores_images_that_are_not_ours():
    images = [
        *_names("vgpu", "20261116", "20261123", "20261201"),
        "morphocloud-slicer-5.12.3-gpu",
    ]
    in_use = {"vgpu": "morphocloud-share-vgpu-20261201", "regular": None}
    assert bm.prune_choice(images, in_use, set()) == ["morphocloud-share-vgpu-20261116"]


def test_leaked_build_and_check_instances_older_than_two_days():
    now = dt.datetime(2026, 12, 1, 8, tzinfo=dt.UTC).timestamp()
    servers = [
        {"name": "mc-bake-20261126-080000", "created": "2026-11-26T08:00:05Z"},
        {"name": "mc-bakecheck-20261130-090000", "created": "2026-11-30T09:00:05Z"},
        {"name": "instance-284", "created": "2026-11-01T00:00:00Z"},
        {"name": "mc-bake-notes", "created": "2026-11-01T00:00:00Z"},
    ]
    assert bm.leaked(servers, now) == ["mc-bake-20261126-080000"]


def test_promote_command_names_both_variables():
    cmd = bm.promote_command(
        {
            "vgpu": "morphocloud-share-vgpu-20261201",
            "regular": "morphocloud-share-regular-20261201",
        }
    )
    assert (
        "gh variable set MORPHOCLOUD_IMAGE_VGPU -R MorphoCloud/Instances -b morphocloud-share-vgpu-20261201"
        in cmd
    )
    assert "MORPHOCLOUD_IMAGE_REGULAR" in cmd
