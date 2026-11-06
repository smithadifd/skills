# IC-ZOD-BUMP: Bump zod past the memory-exhaustion advisory in example-app's frontend

Status: closed

## Plan
Revision 1, presented by pass 5a1c0de5a1c0de01
- Raise zod in the frontend to at least 4.6.6.
- npm audit in frontend shows no high-severity finding for zod.
- npm run build, npm run type-check, npm run lint and npm test stay green.
- No deploy.
- open question: The ticket text calls zod a direct dependency; it is transitive and dev-only (via eslint-plugin-react-hooks), so the fix is lockfile-only and package.json is not touched.

## Decisions
- plan go, plan revision 1, the person: Go
- outward go, plan revision 1, the person: Go

## Review rounds
- round 1: pass on plan revision 1 (work/ic-zod-bump/review/receipts/diff-claude-empirical-r1/review-run.json)

## Tried
- [orient] npm audit on origin/main 49d3aef in a fresh worktree (npm ci): 0 critical, 1 high, 8 moderate; high = zod &lt;4.6.6, isDirect false, via eslint-config-next &gt; eslint-plugin-react-hooks@7.1.1 &gt; zod@4.6.5
- [build] builder-core dispatch: refused: agent worktree isolation needs a git cwd; built on the main thread in the same worktree
- [build] npm update zod with local Node 26 / npm 11.16 (no Node 22 installed): lockfile diff limited to the zod entry; gates green
- [review] Sol round 1 (gpt-5.6-sol high): APPROVE, no issues; receipt unparseable on its Checks section, adjudicated approved
- [review] owner merge go typed in pane: Go

## Stamps
- stop [plan]: Waiting at plan go for plan revision 1.
  next skills: ticket
  refs: .ticket/IC-ZOD-BUMP.md
- stop [review]: Waiting at outward go after review round 1.
  next skills: ticket
  refs: .ticket/IC-ZOD-BUMP.md, work/ic-zod-bump/review/receipts/diff-claude-empirical-r1/review-run.json
- close [close]: Merged example/example-app#423 (squash 92b1b49, head ecbd289) on the owner's typed go; zod 4.6.5 -&gt; 4.6.7 lockfile-only; npm audit high 1 -&gt; 0; no deploy within this pass. The owner then asked for a deploy, run as a separate action after close.
  next skills: deploy
  refs: example/example-app#423, 92b1b49, ecbd2892e092bc6b4a8fff8b04e85d438a08d57c, branch:fix/zod-advisory, work/ic-zod-bump/review-pr/receipts

<!-- ticket-card:v2
{"decisions":[{"criteria":["Raise zod in the frontend to at least 4.6.6.","npm audit in frontend shows no high-severity finding for zod.","npm run build, npm run type-check, npm run lint and npm test stay green.","No deploy."],"effect":"The plan gate is approved for plan revision 1.","evidence":"/home/user/.claude/projects/home/11111111-2222-4333-8444-555555555555.jsonl","gate":"plan","pass":"5a1c0de5a1c0de01","phase":"plan","plan_revision":1,"source":{"ref":"/home/user/.claude/projects/home/11111111-2222-4333-8444-555555555555.jsonl","what":"ratification"},"standing":"settled","tags":[{"id":"IC-ZOD-BUMP","type":"ticket"}],"verbatim":"Go","who":"the person","who_kind":"person"},{"criteria":["Raise zod in the frontend to at least 4.6.6.","npm audit in frontend shows no high-severity finding for zod.","npm run build, npm run type-check, npm run lint and npm test stay green.","No deploy."],"effect":"The outward gate is approved for plan revision 1.","evidence":"/home/user/.claude/projects/home/11111111-2222-4333-8444-555555555555.jsonl","gate":"outward","phase":"review","plan_revision":1,"source":{"ref":"/home/user/.claude/projects/home/11111111-2222-4333-8444-555555555555.jsonl","what":"ratification"},"standing":"settled","tags":[{"id":"IC-ZOD-BUMP","type":"ticket"}],"verbatim":"Go","who":"the person","who_kind":"person"}],"key":"IC-ZOD-BUMP","pass":null,"plan":{"criteria":["Raise zod in the frontend to at least 4.6.6.","npm audit in frontend shows no high-severity finding for zod.","npm run build, npm run type-check, npm run lint and npm test stay green.","No deploy."],"diff_statement":"Update frontend/package-lock.json so the transitive dev dependency zod resolves to 4.6.7 instead of 4.6.5, with no package.json change.","key":"IC-ZOD-BUMP","open_questions":["The ticket text calls zod a direct dependency; it is transitive and dev-only (via eslint-plugin-react-hooks), so the fix is lockfile-only and package.json is not touched."],"pass":"5a1c0de5a1c0de01","revision":1,"schema":"ticket-plan-card/v2","steps":[{"name":"orient","status":"complete"},{"name":"research","status":"complete"},{"name":"plan","skip":"a one-package lockfile-only bump with no design choice","status":"skipped"},{"name":"build","status":"pending"},{"name":"review","status":"pending"},{"name":"outward","status":"pending"}],"ticket_status":"open","ticket_updated_at":"2026-11-05","title":"Bump zod past the memory-exhaustion advisory in example-app's frontend"},"reviews":[{"criteria":["Raise zod in the frontend to at least 4.6.6.","npm audit in frontend shows no high-severity finding for zod.","npm run build, npm run type-check, npm run lint and npm test stay green.","No deploy."],"evidence":"work/ic-zod-bump/review/receipts/diff-claude-empirical-r1/review-run.json","findings":[],"passed":[0,1,2,3],"plan_revision":1,"reviewer":"fresh-context reviewer","round":1,"verdict":"pass"}],"schema":"ticket-card/v2","stamps":[{"close":false,"next_skills":["ticket"],"phase":"plan","point":"Waiting at plan go for plan revision 1.","refs":[".ticket/IC-ZOD-BUMP.md"]},{"close":false,"next_skills":["ticket"],"phase":"review","point":"Waiting at outward go after review round 1.","refs":[".ticket/IC-ZOD-BUMP.md","work/ic-zod-bump/review/receipts/diff-claude-empirical-r1/review-run.json"]},{"close":true,"next_skills":["deploy"],"phase":"close","point":"Merged example/example-app#423 (squash 92b1b49, head ecbd289) on the owner's typed go; zod 4.6.5 -\u003e 4.6.7 lockfile-only; npm audit high 1 -\u003e 0; no deploy within this pass. The owner then asked for a deploy, run as a separate action after close.","refs":["example/example-app#423","92b1b49","ecbd2892e092bc6b4a8fff8b04e85d438a08d57c","branch:fix/zod-advisory","work/ic-zod-bump/review-pr/receipts"]}],"status":"closed","title":"Bump zod past the memory-exhaustion advisory in example-app's frontend","tried":[{"evidence":"work/ic-zod-bump/audit-main.json","outcome":"0 critical, 1 high, 8 moderate; high = zod \u003c4.6.6, isDirect false, via eslint-config-next \u003e eslint-plugin-react-hooks@7.1.1 \u003e zod@4.6.5","phase":"orient","what":"npm audit on origin/main 49d3aef in a fresh worktree (npm ci)"},{"evidence":"branch:fix/zod-advisory","outcome":"refused: agent worktree isolation needs a git cwd; built on the main thread in the same worktree","phase":"build","what":"builder-core dispatch"},{"evidence":"ecbd2892e092bc6b4a8fff8b04e85d438a08d57c","outcome":"lockfile diff limited to the zod entry; gates green","phase":"build","what":"npm update zod with local Node 26 / npm 11.16 (no Node 22 installed)"},{"evidence":"work/ic-zod-bump/review/receipts/diff-sol-r1-adjudicated/review-run.json","outcome":"APPROVE, no issues; receipt unparseable on its Checks section, adjudicated approved","phase":"review","what":"Sol round 1 (gpt-5.6-sol high)"},{"evidence":"/home/user/.claude/projects/home/11111111-2222-4333-8444-555555555555.jsonl","outcome":"Go","phase":"review","what":"owner merge go typed in pane"}]}
-->
