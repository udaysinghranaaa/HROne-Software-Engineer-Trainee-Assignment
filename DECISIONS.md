# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.

## Phase 4 decisions

- Punch-out selects the latest BSON punch-in at or before the requested instant, regardless of whether it is already closed. A missing candidate is 404; an already closed candidate is 409. Equal times and durations beyond 24 hours are 422. The user clarified that a timestamp before all punch-ins is 404.
- PATCH rejects derived and unsupported fields with 422, as explicitly clarified by the user. The global ignore-derived-fields convention continues to apply to the Phase 3 request models and punch-out. PATCH employee codes remain plain strings: the path contract specifies no creation-code regex.
- Both writes compare the natural key and the stored attendance snapshot, including history and the distinction between missing and present legacy fields. One `find_one_and_update` commits the change. A stale snapshot returns 409 without retrying automatically. This also protects a correction racing a punch-out.
- PATCH uses `$set` only for changed fields and `$push` for exactly one audit entry in the same operation. Audit changes include computed fields that actually changed. Prior history and unrelated fields survive; no-op corrections return 422. No schema revision field, transaction, process lock or new infrastructure is needed.
- Existing UTC/IST and R2-R5 helpers remain authoritative. Whole-second UTC BSON punches and audit instants serialize as epoch milliseconds. Presence requires punch-in; absence clears both times and resets calculations. The attendance date cannot move.
- The existing four indexes remain. `attendance_latest_punch` on `(emp_code ASC, punch_in DESC, date DESC)` supports the employee-scoped latest-candidate query. Snapshot updates use the unique natural-key index. Large-data query plans and performance still need live measurement.
- The opt-in Phase 4 verifier shares Phase 3's collision, ownership, Windows interpreter, child PID/database/token, bound-port and cleanup protections. Its 35-byte `hrone_p4v_` names and separate result file distinguish Phase 4 runs. It is prepared and isolated-tested, not executed against Atlas.

The five original submission questions above are retained. Analytics-specific answers and implementations remain for later phases; Phase 4 does not claim to have implemented them.
