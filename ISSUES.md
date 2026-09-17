# Ouroboros Code Sweep Issues

## Multireview Verified Defects

### GitHub #127: Bare except in apply_fix_and_create_pr swallows KeyboardInterrupt and SystemExit
**File:** `src/ouroboros/github_improvement.py`
**Line:** 291
**Description:**
The error recovery block in `apply_fix_and_create_pr` attempts to restore the main branch using a bare `except: pass`. In Python, bare `except:` catches `BaseException`, intercepting `KeyboardInterrupt` and `SystemExit`.
**Fix:** Replace `except: pass` with `except Exception: pass`.

### GitHub #128: Eager evaluation in dict.get default causes AttributeError when r.outcome is None
**File:** `src/ouroboros/wiki.py`
**Line:** 190
**Description:**
In `generate_history_page`, `.get(r.outcome, f"[{r.outcome.upper()}]")` evaluates the default value eagerly before calling `.get()`. If `r.outcome` is `None`, evaluating `r.outcome.upper()` raises `AttributeError: 'NoneType' object has no attribute 'upper'`, breaking changelog/wiki generation.
**Fix:** Guard with `fallback_badge = f"[{(r.outcome or 'UNKNOWN').upper()}]"` or check `if r.outcome:`.

## Bugs and Logical Flaws

### 1. Loop variable capture bug in closure (B023)
**File:** `src/ouroboros/moltbook.py`
**Lines:** 1646, 1648
**Description:** 
In `run_loop`, the `_on_improve_event` callback function uses `cfg` which is a local variable from the outer scope that is modified earlier in the loop (`cfg = load_runner_config()`). This can cause unexpected behavior because the callback binds to the variable by reference, not by value at the time the function is defined.
**Fix:** Pass `cfg` as a default argument to the callback: `def _on_improve_event(event_type: str, message: str, data: dict, cfg=cfg) -> None:`.

### 2. Broad exception catching in system stats (Logic Flaw)
**File:** `src/ouroboros/system.py`
**Lines:** 67, 82, 94, 105, 114
**Description:** 
Multiple blocks of `except Exception: pass` are used around `subprocess.check_output` calls. While this prevents the system stats gathering from crashing the app, it also swallows `KeyboardInterrupt` (if not caught specifically) and hides all potential `FileNotFoundError` or permission errors, making debugging system-level issues difficult.
**Fix:** Catch specific exceptions like `subprocess.CalledProcessError` or `OSError`.

### 3. Potential crashes hidden by type erasure / lack of None checks (Mypy)
**Files:**
- `src/ouroboros/test_runner.py:141`: Attempting to `.append()` to an `object` (likely missing proper list initialization).
- `src/ouroboros/wiki.py:62`: Calling `.strip()` on a value that can be an `int` or `list`, which will raise an `AttributeError` at runtime.
- `src/ouroboros/moltbook.py:1454`: Accessing `.api_key` on potentially `None` `Credentials`.
- `src/ouroboros/moltbook.py:1714` & `1748`: Indexing or accessing attributes on types that can be `None` (e.g. `ImprovementTask | None`).
- `src/ouroboros/storage.py:372`: Appending an `int` to a `list[str]`.
**Description:**
Static analysis with `mypy` reveals 32 typing and logic errors across 14 files. Some of these are critical runtime bugs (like `AttributeError` traps) that are currently masked by dynamic typing but will crash when specific code paths are executed.
**Fix:** Resolve type mismatches, add explicit `if x is not None:` guards, and initialize variables with their proper data structures (e.g. `[]` instead of `object()`).

## Code Quality and Linting Issues

### 3. Improper assertions in tests (B011, B017)
**Files:** 
- `tests/test_codebase.py:59`
- `tests/test_improvement.py:172`
- `tests/test_policies.py:22`
- `tests/test_routing.py:55`
**Description:**
Using `assert False` is dangerous because running Python with `-O` removes assertion statements, meaning the tests would pass regardless of the exception being raised. Additionally, `test_routing.py` uses `pytest.raises(Exception)`, which is a blind exception catch that masks unrelated errors.
**Fix:** Use `pytest.raises(ExpectedExceptionType)` instead of try-except blocks with `assert False`.

### 4. Improper property access (B009)
**File:** `src/ouroboros/metrics.py`
**Line:** 76
**Description:**
The code uses `bool(getattr(result, "is_valid"))`. Using `getattr` with a constant string is not any safer than normal property access since there is an explicit `hasattr(result, "is_valid")` check right before it.
**Fix:** Use `bool(result.is_valid)`.

### 5. Unused loop control variable (B007)
**File:** `src/ouroboros/llm.py`
**Line:** 142
**Description:**
`for key, value in message.items():` is used, but `key` is never referenced in the loop body.
**Fix:** Change to `for _key, value in message.items():` or `for value in message.values():`.

### 6. Undefined names in type hints (F821)
**Files:** 
- `src/ouroboros/evaluation.py:62`
- `src/ouroboros/knowledge_base.py:28`
**Description:**
String-based type hints (`"ImprovementResult"`, `"OuroborosStorage"`) are used, but the names are not defined in the module's scope, which trips up static analysis tools like Ruff.
**Fix:** Use `from __future__ import annotations` and type hints without quotes, or ensure the names are properly imported in a `TYPE_CHECKING` block.

### 7. Unused import redefinitions (F811)
**Files:**
- `tests/test_backlog.py` (mark_done, mark_failed)
- `tests/test_codebase.py` (json)
- `tests/test_community_improvement.py` (CodeChange)
**Description:**
Several test files import the same name multiple times, leading to redefinition warnings.

## Note on TODOs
A comprehensive search across the `src/` and `tests/` directories yielded no explicit `TODO`, `FIXME`, or `HACK` comments. A mention of "the bug #67 is about" was found in `moltbook.py` line 1286, which documents a previously addressed issue rather than an outstanding bug. All 1,130 tests pass locally.
