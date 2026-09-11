## 2026-09-08 06:00:00Z - Learning 1
Dependency pinning for the ML sandbox environment turned out to be more delicate than expected. The build previously resolved a mismatched CUDA runtime silently, which only surfaced once a training job crashed three hours in with an opaque kernel launch failure that gave no indication of a version mismatch anywhere in its output. All new dependencies should be selected based on their compatibility to torch 2.14.0+cu130, since the sandbox's driver stack is fixed and cannot be upgraded to chase a newer wheel. Pinning the wheel index explicitly instead of relying on the default resolver order avoided the whole class of bug, and the fix has held across three subsequent dependency additions without needing to be revisited.

## 2026-07-14 10:00:00Z - Learning 2
Docker layer caching on the CI runners was invalidating far more often than it should have. Moving the dependency install step above the source copy step recovered most of the lost cache hits, since the source tree changes on every commit but the dependency list rarely does. Build times on the affected pipeline dropped from roughly eleven minutes to under three, which was enough of a difference that reviewers stopped batching commits to avoid waiting on CI. The same reordering was later applied to two other pipelines that showed the identical caching pattern, with comparable improvements in both cases.

## 2026-07-11 09:00:00Z - Learning 3
The structured logging format adopted last quarter omits the request id on a handful of background worker paths, which makes correlating a slow job with its originating request needlessly manual. Every background task should thread the request id through from the point it is enqueued rather than generating a fresh one when the worker eventually picks it up, since regenerating it there discards the very correlation the field exists to provide. Auditing the remaining worker paths for the same gap is still open work, tracked separately from this note.

## 2026-07-08 08:00:00Z - Learning 4
A cluster of flaky integration tests turned out to share a root cause: a shared fixture reused a single temporary directory across parametrized cases without cleaning it between runs. Test order therefore mattered even though nothing about the test names suggested it did, and the flake only reproduced when the suite ran in parallel with more than two workers. Giving each parametrized case its own temporary directory removed the flake entirely, and the fixture now asserts the directory is empty on entry as a guard against the same mistake recurring elsewhere.

## 2026-09-07 20:00:00Z - Learning 5
Onboarding a new internal library exposed a packaging constraint nobody had written down anywhere. library-X==2.4.1 requires Python >=3.11; agents must not attempt installation under Python 3.10. The failure mode on 3.10 is a confusing wheel-tag mismatch error rather than a clear version message, so this is worth checking before, not after, an install attempt is made. Two separate agents lost time to the same confusing error before this was written down, which is the entire reason it is being recorded here rather than left to be rediscovered a third time.

## 2026-07-02 06:00:00Z - Learning 6
The nightly pipeline's retry policy was masking a genuine upstream outage for several days. Each retry succeeded often enough on the fourth or fifth attempt that the dashboard never turned red, even though the true success rate on the first attempt had quietly dropped below sixty percent. Alerting now tracks first-attempt success separately from eventual success, and the gap between the two numbers is itself graphed so a widening gap gets noticed well before either metric alone would cross an alerting threshold.

## 2026-06-29 05:00:00Z - Learning 7
A missing composite index on the events table was responsible for a slow query that only showed up in production once the table passed roughly two million rows. The query planner switched from an index scan to a full table scan right around that threshold, and nothing in staging's much smaller dataset ever exercised that transition. Adding the composite index brought the query back under its latency budget immediately, and a periodic check now runs the same query plan against a sampled production-sized dataset in staging so a similar threshold effect would be caught earlier.

## 2026-06-26 04:00:00Z - Learning 8
Rate limiting on the public API was implemented per process rather than per fleet, so horizontal scaling silently multiplied the effective limit by the number of running instances. Clients that depended on the documented limit for their own backoff logic occasionally got a burst of requests through that the design never intended to allow. Moving the counter into a shared store fixed the discrepancy, at the cost of one extra network round trip per request, which turned out to be well within the existing latency budget for this endpoint.

## 2026-06-23 03:00:00Z - Learning 9
A broad except clause around the payment webhook handler was swallowing a specific, actionable validation error alongside genuine transient network failures. Splitting the handling so only the network-related exceptions are retried, while validation errors are logged and surfaced immediately, cut the average time to notice a malformed webhook payload from days to minutes. The same broad-except pattern was found in two other handlers during the follow-up audit and has since been narrowed in both.

## 2026-06-20 02:00:00Z - Learning 10
Code review turnaround improved noticeably after splitting large, multi-concern pull requests into a sequence of smaller ones, each reviewable in under fifteen minutes. The team's own data showed review comments per line roughly doubled once individual diffs shrank below two hundred changed lines, suggesting reviewers were skimming the larger ones rather than reading closely. This is now a soft guideline rather than an enforced limit, since a handful of genuinely cohesive changes are still better reviewed as a single unit.

## 2026-06-17 01:00:00Z - Learning 11
A deployment rollback attempt failed halfway through because the rollback script assumed the previous release's database migrations were always reversible, which was not true for one column-drop migration earlier in the year. Rollback tooling now checks migration reversibility explicitly before allowing an automatic rollback to proceed unattended, and refuses with a clear message rather than failing partway through when a migration in the chain cannot be reversed.

## 2026-06-14 00:00:00Z - Learning 12
An in-memory cache with no eviction policy grew unbounded under a traffic pattern nobody had load-tested: a long tail of distinct, rarely-repeated keys rather than the small hot set the cache was designed around. Switching to a bounded LRU cache with a modest size cap fixed the memory growth without measurably hurting the hit rate on the actual traffic mix, and the same size cap has since been applied to two other caches with a similar unbounded-key-space shape.

## 2026-06-10 23:00:00Z - Learning 13
Alert thresholds copied from a similar but lower-traffic service produced a steady trickle of pages that nobody acted on, which trained the on-call rotation to treat every page from that service as noise. Re-deriving the thresholds from that service's own historical percentiles, rather than reusing another service's numbers, eliminated most of the false positives, and the on-call rotation reports meaningfully higher trust in pages from that service since the change.

## 2026-06-07 22:00:00Z - Learning 14
Short-lived authentication tokens issued by the internal service mesh were not being refreshed proactively, so a client would only notice expiry on the next outbound call and had to retry after re-authenticating. Refreshing a token once it crosses roughly eighty percent of its lifetime, rather than waiting for a failed call, removed the retry entirely from the common path, and the change required no coordination with the token-issuing service itself.

## 2026-06-04 21:00:00Z - Learning 15
The frontend build pipeline was bundling a large date-formatting library in full even though only a handful of its locales were ever used in production. Switching to a per-locale import path shaved a substantial chunk off the initial bundle size and measurably improved the time-to-interactive metric on slower mobile connections, which was the segment of users most affected by the original bundle size in the first place.

## 2026-06-01 20:00:00Z - Learning 16
Websocket clients reconnecting after a brief network blip were all retrying on the same fixed one-second interval, which produced a synchronized thundering herd against the gateway every time it briefly dropped. Adding jitter to the reconnect backoff spread the reconnection attempts out enough that the gateway no longer saw a load spike on recovery, and the same jitter pattern is now the default in the shared client library rather than something each integration adds itself.

## 2026-05-29 19:00:00Z - Learning 17
A message queue consumer with no backpressure signal kept pulling messages faster than it could process them whenever a downstream dependency slowed down, building an ever-growing in-memory backlog until the process eventually ran out of memory. Bounding the consumer's in-flight message count fixed the failure mode without requiring any change to the downstream dependency, and the bound is now a required parameter rather than an optional one on the shared consumer wrapper.

## 2026-05-26 18:00:00Z - Learning 18
A feature flag left permanently enabled for internal accounts, but never cleaned up after the experiment concluded, ended up gating a code path that diverged further from the default path with every subsequent change. Restoring parity took longer than the original experiment itself had, which is now the standing argument for flag removal being part of the experiment's own exit criteria rather than a separate follow-up task that competes with newer work for priority.

## 2026-09-08 10:30:00Z - Learning 19
Integration work against the partner API surfaced an undocumented constraint the sandbox environment never enforced. The API accepts batches of at most 100 items; all subsequent agents must split larger inputs into chunks of <=100. The production endpoint returns a generic five-hundred error rather than a validation message when the limit is exceeded, which made the root cause far harder to identify than it should have been.

## 2026-05-20 16:00:00Z - Learning 20
Documentation for the internal deployment tool had drifted far enough from its actual behavior that new team members were following steps that no longer matched any current environment. Regenerating the relevant section directly from the tool's own help output, rather than maintaining a hand-written copy, is the only version of this that has stayed accurate so far.

