# BLOCKED (v1.1)

## The full suite in one process was killed three times (worked around)

`pytest` as one background process died three times on the build machine at random points
(12%, 24%, 61%), with no test failure: exit 127 / -1, killed from outside. At the same time
another agent was running its own pytest suite in a different project on the same machine. A
lone idle Python process survived, and so did every foreground run.

Workaround used for v1.1 M5: the suite in five chunks, each in the foreground, each with its own
JUnit file, merged into `report.xml` for the LAUNCH.md count check. Result: 581 passed, 0 failed,
0 skipped, browser and screenshot tests included. Worth one uninterrupted `pytest` run on a quiet
machine (or CI) before release.
