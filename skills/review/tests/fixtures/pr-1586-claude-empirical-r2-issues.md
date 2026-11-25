### Issues
- `skills/orchestrate/design_tier.py:1222` — Line 1222 is the guard that lets only a verified settlement replace the adjudication. Replacing it with `if True:` leaves every suite green:
  - the design and settle subset of `test_review_run.py`: 45 passed;
  - `test_design_tier.py` plus `test_owed_design_passes.py`: 299 passed.

  The new test, `test_design_settle_adjudicated_round_passes_only_with_verified_sibling`, never covers the "only" part. It builds no `-settled` directory that the reader declines to select.

  The guard does change behaviour. I put an empty `diff-R1-sol-r2-settled` directory beside a valid adjudication:
  - at head, the gate refuses `design_pass_superseded: R1 (the store holds diff-R1-sol-r2-adjudicated)`;
  - under the mutation, it refuses `design_pass_not_approved: R1 (verdict 'unparseable', …)`.

  Both still refuse, so nothing gets through today. But a test asserting the `design_pass_superseded` name would go red under the mutation, and none does. [reach: lane (the untested guard and its test are both in this change's own files); breaks: evidence; evidence: probed]

