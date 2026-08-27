"""
Focused runner for the webhook-auth HMAC suite.
Run:  bench --site <site> run-tests --module saathimart.tests.test_hmac_auth
(Kept separate from the main suite so auth tests can be exercised without
pulling in the full catalog/cart fixtures.)
"""
from saathimart.tests.test_saathimart import TestVerifyHubSecret  # noqa
