"""Tests for watchers/__init__.py's shared telemetry/heartbeat helpers.

``sleep_with_heartbeat`` was originally a private method on ``VulnWatcher``
(fixed for the liveness-probe crash loop described in its own module's
history), then pulled up here so ``skill_learner.py`` -- which has the exact
same "long tick interval vs. a short liveness-probe staleness window"
shape -- could reuse it instead of re-deriving the fix. See
docs/postgres-migration-plan.md for the full incident history.
"""
from __future__ import annotations

from unittest.mock import patch

from agentit.watchers import HEARTBEAT_REFRESH_SECONDS, sleep_with_heartbeat


class TestSleepWithHeartbeat:
    async def test_touches_heartbeat_multiple_times_for_long_interval(self):
        interval = HEARTBEAT_REFRESH_SECONDS * 2 + 100

        sleep_calls: list[int] = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        touch_count = 0

        def fake_touch(self):
            nonlocal touch_count
            touch_count += 1

        with patch("agentit.watchers.asyncio.sleep", side_effect=fake_sleep), \
             patch("agentit.watchers.Path.touch", fake_touch):
            await sleep_with_heartbeat(interval)

        # Chunked into 300s + 300s + 100s, heartbeat touched after each chunk.
        assert sleep_calls == [HEARTBEAT_REFRESH_SECONDS, HEARTBEAT_REFRESH_SECONDS, 100]
        assert touch_count == 3

    async def test_short_interval_is_a_single_chunk(self):
        sleep_calls: list[int] = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("agentit.watchers.asyncio.sleep", side_effect=fake_sleep), \
             patch("agentit.watchers.Path.touch") as mock_touch:
            await sleep_with_heartbeat(1)

        assert sleep_calls == [1]
        mock_touch.assert_called_once()

    @patch("agentit.watchers.asyncio.sleep", side_effect=KeyboardInterrupt)
    async def test_keyboard_interrupt_propagates(self, mock_sleep):
        try:
            await sleep_with_heartbeat(1)
            assert False, "expected KeyboardInterrupt to propagate"
        except KeyboardInterrupt:
            pass

        mock_sleep.assert_called_once_with(1)

    async def test_custom_refresh_seconds_and_heartbeat_path(self, tmp_path):
        heartbeat_path = tmp_path / "heartbeat"

        async def fake_sleep(seconds):
            return None

        with patch("agentit.watchers.asyncio.sleep", side_effect=fake_sleep):
            await sleep_with_heartbeat(5, refresh_seconds=2, heartbeat_path=heartbeat_path)

        assert heartbeat_path.exists()

    async def test_zero_seconds_is_a_noop(self):
        with patch("agentit.watchers.asyncio.sleep") as mock_sleep, \
             patch("agentit.watchers.Path.touch") as mock_touch:
            await sleep_with_heartbeat(0)

        mock_sleep.assert_not_called()
        mock_touch.assert_not_called()
