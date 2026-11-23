"""Tests for toolquota."""

import pytest

from toolquota import Quota, QuotaExceeded, ToolQuota


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(rate=1.0, burst=1, max_calls=None, **quotas):
    clock = FakeClock()
    quota = ToolQuota(
        default=Quota(rate=rate, burst=burst, max_calls=max_calls),
        quotas={name: value for name, value in quotas.items()},
        clock=clock,
    )
    return quota, clock


class TestQuota:
    def test_a_zero_rate_is_rejected(self):
        with pytest.raises(ValueError, match="rate"):
            Quota(rate=0)

    def test_a_zero_burst_is_rejected(self):
        with pytest.raises(ValueError, match="burst"):
            Quota(rate=1, burst=0)

    def test_a_zero_max_calls_is_rejected(self):
        with pytest.raises(ValueError, match="max_calls"):
            Quota(rate=1, max_calls=0)

    def test_the_defaults(self):
        quota = Quota(rate=2)
        assert quota.burst == 1
        assert quota.max_calls is None


class TestAcquire:
    def test_a_fresh_bucket_holds_a_burst(self):
        quota, _ = make(rate=1, burst=3)
        assert [quota.try_acquire("t") for _ in range(3)] == [True, True, True]
        assert quota.try_acquire("t") is False

    def test_the_default_quota_applies_to_every_tool(self):
        quota, _ = make(rate=1, burst=1)
        assert quota.try_acquire("a") is True
        assert quota.try_acquire("b") is True
        assert quota.try_acquire("a") is False

    def test_buckets_are_independent(self):
        quota, _ = make(rate=1, burst=1, search=Quota(rate=1, burst=5))
        assert quota.remaining("search") == 5
        assert quota.remaining("other") == 1

    def test_count_greater_than_one(self):
        quota, _ = make(rate=1, burst=4)
        assert quota.try_acquire("t", count=3) is True
        assert quota.remaining("t") == 1
        assert quota.try_acquire("t", count=2) is False

    def test_a_zero_count_is_rejected(self):
        quota, _ = make()
        with pytest.raises(ValueError, match="count"):
            quota.try_acquire("t", count=0)

    def test_calls_are_counted(self):
        quota, _ = make(rate=1, burst=5)
        quota.try_acquire("t")
        quota.try_acquire("t")
        assert quota.calls("t") == 2

    def test_an_unused_tool_has_no_calls(self):
        quota, _ = make()
        assert quota.calls("never-used") == 0


class TestRefill:
    def test_a_token_comes_back_after_the_interval(self):
        quota, clock = make(rate=1, burst=1)
        quota.try_acquire("t")
        clock.advance(1.0)
        assert quota.try_acquire("t") is True

    def test_a_partial_refill_does_not_allow_a_call(self):
        quota, clock = make(rate=1, burst=1)
        quota.try_acquire("t")
        clock.advance(0.5)
        assert quota.try_acquire("t") is False

    def test_the_bucket_never_exceeds_the_burst(self):
        quota, clock = make(rate=1, burst=2)
        clock.advance(100)
        assert quota.remaining("t") == 2

    def test_a_faster_rate_refills_sooner(self):
        quota, clock = make(rate=4, burst=1)
        quota.try_acquire("t")
        clock.advance(0.25)
        assert quota.try_acquire("t") is True

    def test_remaining_is_fractional(self):
        quota, clock = make(rate=2, burst=2)
        quota.try_acquire("t")
        quota.try_acquire("t")
        clock.advance(0.25)
        assert quota.remaining("t") == 0.5

    def test_time_never_moves_backwards(self):
        quota, clock = make(rate=1, burst=2)
        quota.try_acquire("t")
        quota.try_acquire("t")
        clock.now = -5
        assert quota.remaining("t") == 0


class TestMaxCalls:
    def test_the_absolute_cap_never_refills(self):
        quota, clock = make(rate=1, burst=1, max_calls=2)
        assert quota.try_acquire("t") is True
        clock.advance(100)
        assert quota.try_acquire("t") is True
        clock.advance(100)
        assert quota.try_acquire("t") is False
        assert quota.wait_time("t") == float("inf")

    def test_a_permanent_exhaustion_is_reported(self):
        quota, _ = make(rate=1, burst=1, max_calls=1)
        quota.acquire("t")
        with pytest.raises(QuotaExceeded) as caught:
            quota.acquire("t")
        assert caught.value.permanent is True
        assert "whole quota" in str(caught.value)


class TestWaitTime:
    def test_now_means_zero(self):
        quota, _ = make(rate=1, burst=1)
        assert quota.wait_time("t") == 0.0

    def test_a_full_bucket_and_an_empty_one(self):
        quota, _ = make(rate=0.5, burst=1)
        quota.try_acquire("t")
        assert quota.wait_time("t") == 2.0

    def test_it_accounts_for_a_partial_refill(self):
        quota, clock = make(rate=1, burst=1)
        quota.try_acquire("t")
        clock.advance(0.25)
        assert quota.wait_time("t") == 0.75

    def test_count_scales_the_wait(self):
        quota, _ = make(rate=1, burst=2)
        assert quota.wait_time("t", count=4) == 2.0

    def test_clamping_at_zero(self):
        quota, clock = make(rate=1, burst=2)
        quota.try_acquire("t")
        clock.advance(10)
        assert quota.wait_time("t") == 0.0


class TestAcquireRaises:
    def test_a_temporary_limit(self):
        quota, _ = make(rate=1, burst=1)
        quota.acquire("search")
        with pytest.raises(QuotaExceeded) as caught:
            quota.acquire("search")
        error = caught.value
        assert error.tool == "search"
        assert error.wait_time == 1.0
        assert error.permanent is False
        assert "retry in 1.00s" in str(error)

    def test_acquire_succeeds_when_there_is_room(self):
        quota, _ = make(rate=1, burst=2)
        quota.acquire("t")
        quota.acquire("t")

    def test_it_is_a_runtime_error(self):
        assert issubclass(QuotaExceeded, RuntimeError)


class TestResetAndReport:
    def test_reset_refills_one_tool(self):
        quota, _ = make(rate=1, burst=1)
        quota.try_acquire("a")
        quota.reset("a")
        assert quota.try_acquire("a") is True

    def test_reset_refills_everything(self):
        quota, _ = make(rate=1, burst=1)
        quota.try_acquire("a")
        quota.try_acquire("b")
        quota.reset()
        assert quota.try_acquire("a") is True
        assert quota.try_acquire("b") is True

    def test_reset_clears_the_call_count(self):
        quota, _ = make(rate=1, burst=5)
        quota.try_acquire("t")
        quota.reset()
        assert quota.calls("t") == 0

    def test_report(self):
        quota, _ = make(rate=1, burst=3)
        quota.try_acquire("t")
        assert quota.report() == {"t": {"calls": 1, "available": 2.0}}

    def test_the_report_of_an_empty_quota(self):
        quota, _ = make()
        assert quota.report() == {}


class TestMiddlewareStyleUse:
    def test_a_loop_that_waits_instead_of_failing(self):
        clock = FakeClock()
        quota = ToolQuota(default=Quota(rate=1, burst=1), clock=clock)
        waits = []
        for _ in range(3):
            while not quota.try_acquire("search"):
                waits.append(quota.wait_time("search"))
                clock.advance(quota.wait_time("search"))
        assert waits == [1.0, 1.0]
        assert quota.calls("search") == 3

    def test_a_per_tool_policy_for_a_paid_api(self):
        quota, clock = make(rate=1, burst=1, search=Quota(rate=10 / 60, burst=2, max_calls=3))
        assert quota.try_acquire("search") is True
        assert quota.try_acquire("search") is True
        clock.advance(6)  # 10/min means one token every 6 s
        assert quota.try_acquire("search") is True
        clock.advance(600)
        assert quota.try_acquire("search") is False
        assert quota.try_acquire("other") is True  # the default still applies elsewhere
