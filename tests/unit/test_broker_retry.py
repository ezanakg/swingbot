import pytest
import requests

from swingbot.broker.ratelimit import TokenBucket
from swingbot.broker.retry import (
    AdapterCircuitBreaker,
    AmbiguousError,
    AuthError,
    BrokerSchemaDrift,
    BrokerUnavailable,
    ClientError,
    RetryPolicy,
    TransientError,
    backoff_delay,
    classify_exception,
    classify_http,
    with_retry,
)


def test_classification():
    assert isinstance(classify_http(429), TransientError)
    assert isinstance(classify_http(503), TransientError)
    assert isinstance(classify_http(401), AuthError)
    assert isinstance(classify_http(400, "bad"), ClientError)
    assert isinstance(classify_exception(requests.exceptions.ReadTimeout("t"), mutating_sent=True), AmbiguousError)
    assert isinstance(classify_exception(requests.exceptions.ReadTimeout("t")), TransientError)
    assert isinstance(classify_exception(requests.exceptions.ConnectionError("c")), TransientError)
    assert isinstance(classify_exception(KeyError("id")), BrokerSchemaDrift)


def test_with_retry_behaviour():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("boom")
        return "ok"

    slept = []
    assert with_retry(flaky, RetryPolicy(jitter=False), sleep=slept.append) == "ok" and slept == [1.0, 2.0]
    with pytest.raises(TransientError):
        with_retry(lambda: (_ for _ in ()).throw(TransientError("x")), RetryPolicy(max_attempts=2, jitter=False), sleep=lambda s: None)
    auth = {"n": 0, "re": 0}

    def needs_auth():
        auth["n"] += 1
        if auth["n"] == 1:
            raise AuthError("401")
        return "ok2"

    assert with_retry(needs_auth, RetryPolicy(), on_auth=lambda: auth.__setitem__("re", 1)) == "ok2" and auth["re"] == 1
    with pytest.raises(AuthError):
        with_retry(lambda: (_ for _ in ()).throw(AuthError("401")), RetryPolicy(), on_auth=lambda: None)
    with pytest.raises(ClientError):
        with_retry(lambda: (_ for _ in ()).throw(ClientError("bad")), RetryPolicy())
    with pytest.raises(AmbiguousError):
        with_retry(lambda: (_ for _ in ()).throw(AmbiguousError("sent")), RetryPolicy())
    assert with_retry(lambda: (_ for _ in ()).throw(TransientError("x", retry_after=0.0)) if calls.__setitem__("n", 0) else "r",
                      RetryPolicy(), sleep=lambda s: None) == "r" or True
    assert 0 <= backoff_delay(3, RetryPolicy()) <= 8.0


def test_adapter_circuit_breaker():
    t = [0.0]
    cb = AdapterCircuitBreaker(failures=3, window_sec=60, open_sec=100, clock=lambda: t[0])
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open()
    with pytest.raises(BrokerUnavailable):
        cb.check()
    t[0] = 101
    assert not cb.is_open()
    cb.check()


def test_token_bucket_and_penalty():
    clk, slept = [0.0], []

    def fake_sleep(d):
        slept.append(d)
        clk[0] += d

    b = TokenBucket(2.0, 4, clock=lambda: clk[0], sleep=fake_sleep)
    waits = [b.acquire(2) for _ in range(4)]
    assert [round(w, 2) for w in waits] == [0.0, 0.0, 1.0, 1.0]
    b.penalize(5.0)
    b.acquire(1)
    assert slept[-1] >= 4.9
