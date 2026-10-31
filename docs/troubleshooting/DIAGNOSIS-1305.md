# ZCode model overload and gateway cooldown

The upstream GLM-5.3 response was HTTP 529 with code 1305 and a message meaning
"This model currently has too much traffic; try again later." Requests across
several pool accounts returned this response; successful requests occurred
between the failures. This is distinct from quota exhaustion (402/code 11113)
and the previously fixed request bootstrap error (405/code 3012).

The native application capture also contains three GLM-5.3 overload responses
followed by a successful fourth request. Its installed JavaScript client resolves
model retry defaults to 11 total attempts, a 2,000 ms initial delay, multiplier 2,
60,000 ms maximum and enabled jitter. Its delay implementation multiplies the
capped exponential delay by a random factor between 0.5 and 1.

The inherited gateway policy instead tried up to three different accounts with
almost no wait, then put every rejected account into a 30-second global cooldown.
This exposed errors earlier and blocked Flash on those same accounts. Changing
accounts does not guarantee relief when the model itself is busy.

The gateway now retries overloads with the native delay policy, releases every
response and pool reservation before waiting, and leaves accounts available.
The router can select another account on the next attempt, or reuse the only
account. Quota and authentication failover remain separate. No request is
replayed after response output begins. Cancellation during backoff stops further
requests. Persistent overload preserves the real upstream error after the retry
budget is exhausted.

The three overload controls are configurable in the dashboard's Upstream settings
and YAML: overload_max_attempts, overload_base_delay_seconds and
overload_max_delay_seconds. Existing configurations inherit the defaults.

Validation: 153 offline tests, including single-account recovery, rotation with
delay, bounded failure, model independence, initial SSE errors, no replay after
output, Retry-After headers, cancellation and both HTTP API dialects.
The rebuilt executable on port 8788 also returned HTTP 200 with complete streamed
replies for both GLM-5.3 and GLM-5.3-Flash during concurrent live checks. Both
requests succeeded on their first attempt, so live backoff itself was not exercised
in that check; deterministic offline tests cover that behavior.
