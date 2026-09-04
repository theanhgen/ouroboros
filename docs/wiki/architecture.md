# Architecture

Auto-generated overview of the Ouroboros codebase.
Last updated: 2026-06-27 07:21 UTC

## Modules

### `backlog.py` (170 lines)
- `_backlog_path(repo_root)`
- `load_backlog(repo_root)`
- `save_backlog(repo_root, items)`
- `add_item(repo_root, task_type, description, priority, source)`
- `mark_done(repo_root, item_id)`
- `mark_failed(repo_root, item_id)`
- `get_pending(repo_root)`
- `format_backlog_for_llm(items)`
- `organize_backlog(repo_root, client, model)`

### `cli.py` (495 lines)
- `cmd_plan(_args)`
- `cmd_propose(_args)`
- `cmd_apply(_args)`
- `cmd_moltbook_run(_args)`
- `cmd_moltbook_status(_args)`
- `cmd_moltbook_feed(args)`
- `cmd_config_show(_args)`
- `cmd_config_modify(args)`
- `cmd_improve_run(args)`
- `cmd_improve_status(_args)`
- `cmd_improve_history(_args)`
- `cmd_improve_community(args)`
- `cmd_improve_github(args)`
- `cmd_improve_scout(args)`
- `cmd_improve_identify(args)`
- `cmd_backlog_list(_args)`
- `cmd_backlog_clean(_args)`
- `build_parser()`
- `main()`

### `codebase.py` (230 lines)
- `extract_code_metadata(content, path)`
- `get_repo_root()`
- `list_source_files(repo_root)`
- `get_test_files(repo_root)`
- `read_file(path)`
- `read_file_raw(path)`
- `get_function_signatures(path)`
- `get_codebase_summary(repo_root)`

### `community_improvement.py` (603 lines)
- `step_community_improvement(client, state, creds, cfg, safety_config)`
- `_step_identify(client, state, cfg, safety_config)`
- `_step_post(client, state, creds, cfg)`
- `_step_wait(state, creds, cfg)`
- `_step_analyze(client, state, cfg)`
- `_step_implement(client, state, creds, cfg, safety_config)`
- `_build_community_pr_body(task, changes, result, ci)`
- `clear_community_improvement(state)`

### `config.py` (61 lines)

### `evaluation.py` (196 lines)
- `_history_path(repo_root)`
- `record_improvement(result, repo_root, model)`
- `load_history(repo_root)`
- `check_pr_outcomes(repo_root)`
- `improvements_today(repo_root)`
- `summarize_history(history)`
- `to_dict(self)`
- `from_dict(cls, data)`

### `git_ops.py` (353 lines)
- `_safe_git_env()`
- `_git(repo)`
- `is_clean(repo)`
- `commit_auto_state(repo)`
- `current_branch(repo)`
- `create_branch(repo, name)`
- `checkout_branch(repo, name)`
- `checkout_main(repo)`
- `delete_branch(repo, name)`
- `commit_changes(repo, message, files)`
- `pull_latest(repo)`
- `push_branch(repo, branch)`
- `create_pr(repo, title, body, base, head)`
- `create_issue(repo, title, body)`
- `find_open_issue_by_marker(repo, marker)`
- `make_auto_issue_marker(task_type, description, target_files)`
- `has_open_improvement_prs(repo)`
- `make_branch_name(task_type)`
- `get_pr_status(repo, branch)`
- `auto_merge_pr(repo, pr_url, strategy)`
- `get_pr_checks_status(repo, pr_url)`
- `get_pr_feedback(repo, pr_url, max_chars)`

### `github_improvement.py` (247 lines)
- `get_open_issues(repo_root)`
- `analyze_issue(client, issue, repo_root, model)`
- `apply_github_fix(client, issue, analysis, repo_root, model, dry_run)`
- `run_github_improvement_cycle(client, repo_root, model, dry_run, enable_auto_merge)`

### `holographic.py` (121 lines)
- `_require_numpy()`
- `encode_atom(word, dim)`
- `bind(a, b)`
- `unbind(memory, key)`
- `bundle()`
- `similarity(a, b)`
- `encode_text(text, dim)`
- `encode_fact(content, entities, dim)`
- `phases_to_bytes(phases)`
- `bytes_to_phases(data)`
- `snr_estimate(dim, n_items)`

### `improvement.py` (1051 lines)
- `_is_path_allowed(file_path, config)`
- `_validate_changes(changes, config)`
- `_count_changed_lines(original, new)`
- `identify_improvements(client, codebase_summary, test_results, history, model, additional_context)`
- `plan_improvement(client, task, relevant_code, model)`
- `generate_changes(client, task, plan, file_contents, config, model)`
- `apply_changes(changes, repo_root)`
- `revert_changes(changes, repo_root)`
- `_format_failure_details(test_result)`
- `_retry_with_root_cause(client, task, original_changes, test_before, test_after, config, repo_root, model, on_event)`
- `validate_improvement(task, changes, repo_root)`
- `_build_failed_attempts_context(history, max_entries)`
- `_build_success_rate_context(history)`
- `_assemble_feed_context(client, state)`
- `run_improvement_cycle(client, state, config, model, dry_run, on_event)`
- `_today()`
- `_append_learning(repo_root, entry)`
- `_read_recent_learnings(repo_root, n)`
- `_build_pr_body(task, changes, result)`
- `from_llm_response(cls, data)`
- `__init__(self, repo_root)`
- `execute(self, name, args)`
- `_fire(event_type, message, data)`

### `improvement_runner.py` (338 lines)
- `_send_notification(cfg, message)`
- `_scheduler_state_path()`
- `load_scheduler_state()`
- `save_scheduler_state(state)`
- `_load_feed_context_state()`
- `_normal_delay_seconds(cfg)`
- `_retry_delay_seconds(cfg, failure_count)`
- `_next_due_ts(state, cfg)`
- `_set_idle_state(state, now, cfg, status)`
- `_set_deferred_state(state, now, status, message, delay_seconds)`
- `_set_failure_state(state, now, cfg, status, error)`
- `_task_issue_marker(task)`
- `_build_followup_issue_body(task, result, marker)`
- `_maybe_create_followup_issue(repo_root, cfg, result)`
- `run_scheduled_self_improvement()`
- `_on_event(event_type, message, data)`

### `issue_scouting.py` (129 lines)
- `_task_issue_marker(task)`
- `_build_issue_title(task)`
- `_build_issue_body(task, test_summary, marker)`
- `run_issue_scouting_cycle(client, repo_root, model, dry_run)`

### `knowledge_base.py` (103 lines)
- `load_kb(path)`
- `save_kb(kb, path)`
- `add_entries(entries, path)`
- `get_summary(client, kb, force_refresh, path)`

### `llm.py` (563 lines)
- `load_openai_key()`
- `load_anthropic_key()`
- `make_client(api_key, provider)`
- `_get_provider(model)`
- `load_local_model_url()`
- `make_local_client(base_url)`
- `_is_local_model(model)`
- `_maybe_strip_response_format(kwargs, model)`
- `_completion_token_kwargs(model, max_tokens)`
- `chat_completion(client, system_prompt, user_prompt, model, response_format, max_tokens)`
- `identify_improvements(client, summary, test_results, history, model, additional_context)`
- `plan_code_change(client, task, code, model)`
- `generate_code(client, plan, files, constraints, model)`
- `review_code_changes(client, task, changes, model)`
- `generate_question_post(client, task_data, code_context, test_failures, model)`
- `analyze_code_suggestions(client, problem, code_context, comments, model)`
- `generate_code_from_suggestion(client, suggestion, code_context, plan, constraints, model)`
- `analyze_comments_for_upgrades(client, post_title, post_content, comments, model)`
- `mine_insight_for_codebase(client, post_title, post_content, bot_comment, model)`
- `extract_topic_signal(client, post_title, bot_comment, replies, model)`
- `extract_insights_batch(client, posts, model)`
- `generate_kb_summary(client, entries, model)`
- `pick_oddities(client, posts, model)`
- `get_tools_definition()`
- `generate_post(client, recent_answer, question_area, model, extra_context)`
- `generate_comment(client, post_title, post_content, model, codebase_context)`
- `answer_question(client, question, codebase_summary, model)`

### `memory.py` (710 lines)
- `_clamp_trust(value)`
- `__init__(self, db_path, hrr_dim)`
- `_init_db(self)`
- `add_fact(self, content, category, tags)`
- `search_facts(self, query, category, min_trust, limit)`
- `update_fact(self, fact_id, content, trust_delta, tags, category)`
- `remove_fact(self, fact_id)`
- `list_facts(self, category, min_trust, limit)`
- `record_feedback(self, fact_id, helpful)`
- `fact_count(self)`
- `close(self)`
- `_extract_entities(self, text)`
- `_resolve_entity(self, name)`
- `_link_fact_entity(self, fact_id, entity_id)`
- `_compute_hrr_vector(self, fact_id, content)`
- `_rebuild_bank(self, category)`
- `__init__(self, store, fts_weight, jaccard_weight, hrr_weight)`
- `search(self, query, category, min_trust, limit)`
- `probe(self, entity, category, limit)`
- `reason(self, entities, category, limit)`
- `contradict(self, category, threshold, limit)`
- `_fts_candidates(self, query, category, min_trust, limit)`
- `_tokenize(text)`
- `_jaccard(a, b)`
- `__init__(self, client, storage)`
- `index_file(self, file_path, content)`
- `index_failure(self, task_id, description, failure_msg)`
- `index_success(self, task_id, description, details)`
- `retrieve_relevant_context(self, query, limit)`
- `record_outcome_feedback(self, description, success)`
- `run_hygiene(self)`
- `_add(name)`

### `metrics.py` (133 lines)
- `_metrics_path(repo_root)`
- `load_metrics(repo_root)`
- `save_metrics(repo_root, snapshots)`
- `record_snapshot(repo_root, improvement_result)`
- `get_summary(repo_root)`

### `model_defaults.py` (3 lines)

### `moltbook.py` (1443 lines)
- `_handle_shutdown(signum, _frame)`
- `_read_json_file(path)`
- `load_credentials()`
- `_request(method, path, api_key, body)`
- `get_status(api_key)`
- `get_feed(api_key, sort, limit)`
- `get_posts(api_key, sort, limit)`
- `create_post(api_key, submolt, title, content, url)`
- `create_comment(api_key, post_id, content, parent_id)`
- `_post_url(post_id)`
- `_comment_url(post_id, comment_id)`
- `get_post_comments(api_key, post_id)`
- `get_my_posts(api_key, agent_name, limit)`
- `load_runner_config()`
- `_state_path()`
- `load_state()`
- `save_state(state)`
- `_send_telegram_message(token, chat_id, text)`
- `_notify(cfg, state, message)`
- `_shorten(text, limit)`
- `_trim_self_question_log(state)`
- `_trim_comment_history(state, limit)`
- `_trim_feed_suggestions(state, limit)`
- `_trim_engagement_scores(state, limit)`
- `_trim_state(state)`
- `_check_engagement(cfg, creds, state, openai_client)`
- `_interruptible_sleep(seconds)`
- `_auto_git_push(state, dry_run)`
- `run_loop()`
- `_pick_client(model)`
- `_is_under_repo(path, repo_root)`
- `_on_improve_event(event_type, message, data)`

### `policies.py` (77 lines)
- `require_pr_only(is_pr_only)`
- `validate_modification_scope(file_paths, config)`
- `validate_change_size(num_files, num_lines, config)`

### `prompts.py` (335 lines)
- `_prompts_path()`
- `load_comment_system_prompt()`
- `load_post_generation_prompt()`
- `load_comment_analysis_prompt()`
- `load_question_post_prompt()`
- `load_code_suggestion_prompt()`
- `load_comment_mining_prompt()`
- `load_topic_signal_prompt()`
- `load_insight_extraction_prompt()`
- `load_kb_summary_prompt()`
- `load_github_issue_analysis_prompt()`
- `load_github_issue_fix_prompt()`
- `load_suggestion_implementation_prompt()`

### `self_modify.py` (136 lines)
- `can_self_modify()`
- `modify_config(updates, config_type)`
- `modify_runner_config(updates)`
- `get_current_config()`

### `self_question.py` (122 lines)
- `generate_codebase_questions(repo_root)`
- `get_questions_with_codebase(repo_root)`
- `choose_question(state, questions)`
- `record_question(state, question, answer)`

### `storage.py` (124 lines)
- `__init__(self, db_path)`
- `_init_db(self)`
- `record_cycle(self, record)`
- `record_metrics(self, metrics)`
- `get_recent_cycles(self, limit)`
- `get_total_cost(self)`
- `add_embedding(self, content_type, ref_id, content, embedding)`
- `search_embeddings(self, content_type, limit)`

### `system.py` (95 lines)
- `get_system_stats()`
- `get_service_logs(lines)`
- `get_system_summary()`

### `test_runner.py` (168 lines)
- `_parse_pytest_output(output)`
- `_run_tests_sandboxed(repo_root, config, timeout)`
- `run_tests(repo_root, timeout)`
- `success(self)`
- `total(self)`
- `summary(self)`

### `wiki.py` (399 lines)
- `_wiki_path(repo_root)`
- `_write_page(repo_root, filename, content)`
- `generate_architecture_page(repo_root)`
- `generate_metrics_page(repo_root)`
- `generate_changelog_page(repo_root)`
- `generate_config_page(repo_root)`
- `generate_failures_page(repo_root)`
- `update_wiki(repo_root)`
- `_generate_index(repo_root)`
