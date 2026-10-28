def test_item_coverage_calculation():
    """Test that item_coverage compares item vs task, not item vs item."""
    from ouroboros.backlog import item_coverage
    item = "implement code-aware indexing in MemoryStore using AST to extract functions, classes, and docstrings"
    task = "implement code-aware indexing in MemoryStore"
    result = item_coverage(item, task)
    # Should be > 0 because task contains some words from item
    assert result > 0, "item_coverage should compare item vs task, not item vs item"
    
    # Test case where item and task are different
    task2 = "completely different task"
    result2 = item_coverage(item, task2)
    # Should be 0 or very low since no overlap
    assert result2 < 0.5, "item_coverage should detect lack of overlap"
