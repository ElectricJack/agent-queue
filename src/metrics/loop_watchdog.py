"""Sample an overdue asyncio loop from a separate thread, without ptrace.

Only code locations are retained: never frame locals, source text or request
payloads. A sampled stack may also indicate blocking I/O or host starvation;
it is evidence of where the loop was stopped, not a CPU attribution claim.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable

logger = logging.getLogger(__name__)

_STACK_LIMIT = 64
_DEPTH_LIMIT = 16
_LOG_INTERVAL = 30.0


class LoopWatchdog:
    """Bounded stack sampler owned by the metrics loop-lag probe."""

    def __init__(
        self, thread_id: int, *, clock: Callable[[], float] = time.monotonic,
        threshold: float = 1.0,
    ) -> None:
        self._thread_id = thread_id
        self._clock = clock
        self._threshold = threshold
        self._deadline = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stacks: Counter[tuple[str, ...]] = Counter()
        self._samples = 0
        self._last_log = 0.0
        self._max_late = 0.0

    def touch(self, interval: float) -> None:
        # One atomic reference assignment; the loop never acquires a sampler
        # lock or waits for stack formatting/logging in the watchdog thread.
        self._deadline = self._clock() + interval

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="aq-loop-watchdog", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _stack(self) -> tuple[str, ...]:
        frame = sys._current_frames().get(self._thread_id)
        locations = []
        try:
            while frame is not None and len(locations) < _DEPTH_LIMIT:
                code = frame.f_code
                locations.append(f"{code.co_filename}:{frame.f_lineno} ({code.co_name})")
                frame = frame.f_back
        finally:
            # Do not retain the daemon's frame graph past this observation.
            del frame
        return tuple(locations)

    def poll(self) -> None:
        """One sampling decision; called only by the watchdog thread."""
        now = self._clock()
        late = now - self._deadline
        if late < self._threshold:
            if self._samples:
                stack, count = self._stacks.most_common(1)[0]
                logger.warning(
                    "Event loop recovered after %.2fs overdue; samples=%d; "
                    "most observed stack (%d samples): %s",
                    self._max_late, self._samples, count, " <- ".join(stack),
                )
                self._stacks.clear()
                self._samples = 0
                self._max_late = 0.0
            return
        stack = self._stack()
        if not stack:
            return
        if stack not in self._stacks and len(self._stacks) >= _STACK_LIMIT - 1:
            stack = ("other (stack limit reached)",)
        self._stacks[stack] += 1
        self._samples += 1
        self._max_late = max(self._max_late, late)
        if self._samples == 1 or now - self._last_log >= _LOG_INTERVAL:
            hottest, count = self._stacks.most_common(1)[0]
            logger.warning(
                "Event loop heartbeat %.2fs overdue; samples=%d; "
                "most observed stack (%d samples): %s",
                late, self._samples, count, " <- ".join(hottest),
            )
            self._last_log = now

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            try:
                self.poll()
            except Exception:
                logger.debug("loop watchdog sample failed", exc_info=True)
