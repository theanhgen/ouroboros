2026-08-22 | add_feature | Implement index_code on MemoryStore in src/ouroboros/memory. | success | tests: 966 -> 966
2026-08-23 | fix_bug | Fix parameter extraction in extract_code_metadata and get_fu | success | tests: 966 -> 968
2026-08-23 | fix_bug | Fix summary line parsing in _parse_pytest_output within src/ | success | tests: 968 -> 972
2026-08-24 | fix_bug | Fix get_pr_checks_status in src/ouroboros/git_ops.py so empt | failed | Forbidden file modification: config/state.json; Forbidden file modification: src
2026-08-24 | fix_bug | Fix _reset_worktree in src/ouroboros/backends.py to accept d | success | tests: 994 -> 1000
2026-08-25 | fix_bug | Fix _fts_candidates in FactRetriever within src/ouroboros/me | success | tests: 1000 -> 1017
2026-08-25 | fix_bug | Fix similarity in src/ouroboros/holographic.py to validate m | success | tests: 1017 -> 1019
2026-08-26 | fix_bug | Fix bind, unbind, bundle, and snr_estimate in src/ouroboros/ | success | tests: 1019 -> 1025
2026-08-26 | add_feature | Add stopword filtering to encode_text in src/ouroboros/holog | success | tests: 1025 -> 1029
2026-08-27 | add_feature | Add retrieval count tracking via self.store.note_retrieved i | success | tests: 1104 -> 1105
2026-09-11 | add_feature | Implement SNR-driven forgetting in MemoryStore by adding a p | success | tests: 1177 -> 1185
2026-09-12 | fix_bug | Fix test failure parsing in _parse_pytest_output within src/ | success | tests: 1185 -> 1188
2026-09-12 | fix_bug | Fix status badge formatting in generate_changelog_page withi | failed | Forbidden file modification: config/state.json
2026-09-12 | fix_bug | Fix pytest collection error parsing in _parse_pytest_output  | success | tests: 1188 -> 1191
2026-09-12 | fix_bug | Fix HRR similarity calculation in FactRetriever.probe and Fa | success | tests: 1191 -> 1194
2026-09-13 | fix_bug | Make MemoryStore.index_code replace stale code facts for the | success | tests: 1208 -> 1210
2026-09-14 | add_feature | Add deterministic failure triage in `improvement.py`: group  | failed | Forbidden file modification: config/state.json
2026-09-14 | add_feature | Extend evaluation summaries to report attempts, successes, f | failed | no code generated
2026-09-14 | fix_bug | Make record_improvement persist cycle and token metrics to t | failed | no code generated
2026-09-14 | refactor | Consolidate JSON persistence in evaluation.load_history/reco | success | tests: 1210 -> 1212
2026-09-15 | add_feature | Add deletion-aware code-memory indexing: implement `MemorySt | success | tests: 1212 -> 1213
2026-09-15 | fix_bug | Make `agent_generate_changes` isolate runtime state files su | failed | Forbidden file modification: config/state.json
2026-09-15 | fix_bug | Update `MemoryStore.update_fact` so moving a fact between ca | success | tests: 1213 -> 1214
2026-09-15 | fix_bug | Make `wiki._write_page` atomically replace generated Markdow | success | tests: 1214 -> 1216
2026-09-15 | fix_bug | Make `revert_changes` enforce repository and symlink safety  | failed | Forbidden file modification: src/ouroboros/improvement.py
2026-09-16 | fix_bug | Make `MemoryStore.index_code` parse the new source and build | failed | reviewer rejected
2026-09-16 | fix_bug | Harden ToolRunner.execute so read_file_content and read_file | failed | no code generated
2026-09-23 | add_test | Add an end-to-end regression test for a policy-blocked impro | success | tests: 1254 -> 1255
2026-09-23 | add_test | Add a regression test proving that an agent-deleted tracked  | success | tests: 1255 -> 1256
2026-09-23 | fix_bug | Make `test_runner` fail closed when pytest collects only ski | failed | no code generated
2026-09-24 | fix_bug | Fix data-loss bug in MemoryStore.index_code: ensure fact str | failed | no code generated
2026-09-24 | refactor | Consolidate duplicated 'History / prompt context / state pat | failed | no plan generated
2026-09-24 | add_feature | Implement code-aware indexing in MemoryStore using AST to ex | failed | generation failed: TruncatedResponse: hit max_tokens=16000 (finish_reason=lengt
2026-09-25 | add_feature | Implement code-aware indexing in MemoryStore using AST to ex | failed | planning call failed: TruncatedResponse: hit max_tokens=12000 (finish_reason=lengt
2026-09-25 | refactor | Extract common 'History / prompt context / state path / JSON | failed | planning call failed: TruncatedResponse: hit max_tokens=24000 (finish_reason=lengt
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where fact strin | success | tests: 1286 -> 1286
2026-09-25 | refactor | Create a shared storage_helpers module to consolidate duplic | failed | reviewer rejected
2026-09-25 | add_feature | Implement code-aware indexing in MemoryStore using AST to ex | failed | reviewer rejected
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code while properly i | failed | planning call failed: TruncatedResponse: hit max_tokens=24000 (finish_reason=lengt
2026-09-25 | fix_bug | Fix test_runner to fail closed when pytest collects only ski | failed | no plan generated
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | success | tests: 1286 -> 1286
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | no plan generated
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | Too many lines changed: 234 > 200
2026-09-25 | fix_bug | Fix MemoryStore.index_code to properly implement code-aware  | failed | reviewer rejected
2026-09-25 | add_test | Add regression test for code-aware indexing in MemoryStore.i | failed | generation failed: RateLimitError: Error code: 429 - {'error': {'message': 'Rat
2026-09-25 | fix_bug | Improve test validation safety by modifying RunnerOutcome.su | failed | reviewer rejected
2026-09-25 | add_feature | Implement structured code indexing in MemoryStore.index_code | failed | generation failed: EditMismatch: src/ouroboros/memory.py: SEARCH block not foun
2026-09-25 | add_feature | Implement dual-category code indexing in MemoryStore.index_c | failed | reviewer rejected
2026-09-25 | fix_bug | Improve test validation safety by modifying `RunnerOutcome.s | success | tests: 1293 -> 1296
2026-09-25 | add_feature | Implement code-aware indexing in MemoryStore by modifying th | failed | reviewer rejected
2026-09-25 | fix_bug | Rectify the `MemoryStore` schema and indexing logic in `src/ | failed | PR creation failed
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | success | tests: 1296 -> 1296
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | reverted | Test regression detected: 0 failures before, 2 after
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | generation failed: EditMismatch: src/ouroboros/evaluation.py: SEARCH block not 
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | reverted | Test regression detected: 0 failures before, 2 after
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Rectify the `MemoryStore` schema and indexing logic in `src/ | reverted | Test regression detected: 0 failures before, 8 after
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Fix the data-loss bug in MemoryStore.index_code where facts  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | reverted | Test regression detected: 0 failures before, 2 after
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | Unparseable: src/ouroboros/storage_helpers.py (line 119: invalid syntax)
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | generation failed: EditMismatch: src/ouroboros/backlog.py: SEARCH block not fou
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | reverted | Test regression detected: 0 failures before, 1 after
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | Forbidden file modification: src/ouroboros/evaluation.py
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | reverted | Coverage regression: dropped from 79.0% to 77.0%
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | Forbidden file modification: src/ouroboros/evaluation.py
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | reverted | Test regression detected: 0 failures before, 1 after
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | generation failed: EditMismatch: src/ouroboros/evaluation.py: SEARCH block not 
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | Too many files changed: 5 > 3
2026-09-25 | fix_bug | Prevent silent replacement of unreadable backlog files in ad | success | tests: 1338 -> 1338
2026-09-25 | fix_bug | Prevent silent replacement of unreadable backlog files in ad | duplicate | already completed: Prevent silent replacement of unreadable backlog files in ad
2026-09-25 | add_test | Convert the standalone `test_index_code_normalization` funct | success | tests: 1338 -> 1338
2026-09-25 | add_test | Convert the standalone `test_index_code_normalization` funct | duplicate | already completed: Convert the standalone `test_index_code_normalization` funct
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | generation failed: EditMismatch: src/ouroboros/storage_helpers.py: SEARCH block
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | failed | generation failed: EditMismatch: src/ouroboros/memory.py: empty SEARCH on an ex
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | reverted | Coverage regression: dropped from 79.0% to 74.0%
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | success | tests: 1338 -> 1338
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-25 | fix_bug | Prevent silent replacement of unreadable backlog files in ad | duplicate | already completed: Prevent silent replacement of unreadable backlog files in ad
2026-09-25 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | reviewer rejected
2026-09-25 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
2026-09-26 | refactor | Extract duplicated 'History / prompt context / state path /  | failed | Forbidden file modification: src/ouroboros/evaluation.py
2026-09-26 | fix_bug | Fix data-loss bug in MemoryStore.index_code where facts are  | duplicate | already completed: Fix data-loss bug in MemoryStore.index_code where facts are 
