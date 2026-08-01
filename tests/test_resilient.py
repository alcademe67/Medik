import unittest

import requests

from kucoin.client import KuCoinAPIError
from kucoin.resilient import backoff_delays, retry_call


class BackoffTests(unittest.TestCase):
    def test_one_attempt_has_no_waits(self):
        self.assertEqual(backoff_delays(1), [])

    def test_delays_double_and_cap(self):
        self.assertEqual(backoff_delays(5, base=2.0, factor=2.0, cap=10.0), [2.0, 4.0, 8.0, 10.0])


class RetryCallTests(unittest.TestCase):
    def setUp(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def test_returns_immediately_on_success(self):
        result = retry_call(lambda: 42, sleep=self.sleep)
        self.assertEqual(result, 42)
        self.assertEqual(self.slept, [])

    def test_retries_transient_failures_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise requests.exceptions.ConnectionError("wifi asleep")
            return "ok"

        self.assertEqual(retry_call(flaky, sleep=self.sleep), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.slept, [2.0, 4.0])

    def test_reraises_after_budget_exhausted(self):
        def always_fails():
            raise KuCoinAPIError("429000", "too many requests")

        with self.assertRaises(KuCoinAPIError):
            retry_call(always_fails, attempts=3, sleep=self.sleep)
        self.assertEqual(self.slept, [2.0, 4.0])

    def test_unlisted_exception_is_not_retried(self):
        def bad_value():
            raise ValueError("bad symbol")

        with self.assertRaises(ValueError):
            retry_call(bad_value, sleep=self.sleep)
        self.assertEqual(self.slept, [])

    def test_on_error_receives_attempt_exception_and_delay(self):
        seen = []
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise OSError("network unreachable")
            return "ok"

        retry_call(
            flaky,
            on_error=lambda attempt, exc, delay: seen.append((attempt, str(exc), delay)),
            sleep=self.sleep,
        )
        self.assertEqual(seen, [(1, "network unreachable", 2.0)])

    def test_zero_attempts_rejected(self):
        with self.assertRaises(ValueError):
            retry_call(lambda: None, attempts=0, sleep=self.sleep)


if __name__ == "__main__":
    unittest.main()
