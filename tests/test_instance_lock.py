from __future__ import annotations

import pytest

from app.instance_lock import single_instance_lock


def test_single_instance_lock_blocks_second_lock(tmp_path):
    lock_file = tmp_path / "bot.lock"

    with single_instance_lock(lock_file):
        with pytest.raises(RuntimeError, match="Bot da dang chay"):
            with single_instance_lock(lock_file):
                pass


def test_single_instance_lock_releases_after_exit(tmp_path):
    lock_file = tmp_path / "bot.lock"

    with single_instance_lock(lock_file):
        pass

    with single_instance_lock(lock_file):
        pass
