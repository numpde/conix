# Recorded indexer stress evidence

Run directories use UTC timestamps, a public source subject and an operation.
These reports summarize retained evidence; raw HTTP/WARC, complete receipts
and process logs remain private under the matching `out/indexer/stress/runs/`
path recorded in each report.

| Recorded operation | Review evidence | Scope |
| --- | --- | --- |
| Fuji PayToPlay capture, 2026-11-16 12:42 UTC | [Capture report](runs/20261116-124233/fuji-pay-to-play/capture/report.json) | Original Conix and independent head observations over 120 seconds |
| Fuji PayToPlay evidence completion, 2026-11-16 15:54 UTC | [Completion report](runs/20261116-155440/fuji-pay-to-play/evidence-completion/report.json) | All 99 blocks and 17 selectors; 1,683 log cells and 101 canonical header rereads |

The capture ran at `08c28f6`. Completion ran at `6a3f2bc`, preserved by
`qualification/stress-evidence-20261116`; the subsequent host reporting
correction is separately recorded in [Note 013](../../../notes/013_recorded_stress_campaign.txt).
These results admit one source-consistent workload. Timed replay and keep-pace
capacity remain unqualified.

The two original private directories were relocated after confirming their
workers were retired. All 42 capture files and nine completion files retained
their bytes, modes and file identities. Reports record the old directory names
for provenance. Embedded paths in the original receipts describe their execution
locations; the reports' `private_directory` fields identify current locations.
The imported capture timestamp is its recorded observation release time;
the completion timestamp is the collector's recorded start time. New runs
allocate their timestamp before effects. Other historical partial runs remain
at their original locations.

Reports are generated from explicit hashes, counts, public source identity and
bounded outcomes. They exclude raw receipts, stderr, URLs, argv, observation
bodies and credentials. Private payloads and logs must not be added here.
