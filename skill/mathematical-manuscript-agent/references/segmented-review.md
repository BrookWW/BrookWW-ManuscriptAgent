# Segmented review

Use this mode for an organized manuscript that benefits from focused audit rounds.
The model chooses the decomposition; the runner executes one fresh, isolated
session per block. Aim to reduce repeated reading and context growth without
treating a shorter context as a guarantee of mathematical correctness.

## Planning stage

Read the complete manuscript and choose relatively self-contained blocks with
manageable review workloads. Group closely connected arguments, split large
proof branches where useful, and order the blocks to support later work. Cover
the paper's substantive content, including unnamed claims and appendices.
Arrange a closing consistency check within the last block or as a small separate
block when useful; it need not repeat the full audit.

Save a short English Markdown plan at `.audit-work/review-plan.md`. Use one
level-two heading per block, numbered consecutively from one: `## 1. Block title`,
`## 2. Block title`, and so on. Under each heading, use a few lines to identify
the scope and review focus; add a related-section pointer only when helpful.
Section names, theorem labels, or clear prose are sufficient. An optional short
overview can precede the blocks. Do not wrap the plan in a code fence.

Choose the number of blocks yourself. No dependency ledger, formal graph,
token estimate, separate approval, or trial decomposition is needed. This stage
produces the plan only; manuscript edits are discarded. The plan sets the order
and round count for this run.

## Audit rounds

Read the supplied plan for orientation, then deeply audit and revise the current
block against the latest manuscript. Other sections remain available: consult
their definitions, statements, proofs, or references when needed to resolve a
question. Do not routinely reread or reaudit the whole paper in every round.
The plan is a navigation aid, not evidence that a mathematical claim is true.

Check dependencies as the mathematics requires, without recording every link.
Make justified repairs and update directly affected passages, including those
outside the block. Use the latest text to interpret the planned scope when
earlier revisions have moved or renamed material. Report unresolved obstacles
and unverified dependencies honestly; do not start another runner or expand
the schedule. Optional collaborators should receive focused tasks and relevant
material for this block.

Keep the complete manuscript and required assets intact for source delivery.
Finish with a brief account of changes, remaining issues, and checks actually
performed. Do not produce an additional handoff report or put review history
into the manuscript. The next round receives the latest sources and the same
short plan, not previous session logs or review reports.
