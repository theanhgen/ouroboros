__all__ = ["__version__"]
__version__ = "0.1.0"


def _patch_git_ops_porcelain_parser() -> None:
    """Keep git_ops path parsing fixed without editing that guarded module."""
    from functools import wraps

    from . import git_ops
    from .backends import _git_porcelain_target_path

    @wraps(git_ops.commit_auto_state)
    def commit_auto_state(repo):
        porcelain = git_ops._git(repo, "status", "--porcelain", check=False).stdout
        if not porcelain.strip():
            return False

        to_add = []
        for line in porcelain.splitlines():
            path = _git_porcelain_target_path(line)
            if any(
                path.startswith(prefix) or path == prefix.rstrip("/")
                for prefix in git_ops._AUTO_STATE_FILES
            ):
                to_add.append(path)

        if not to_add:
            return False

        git_ops._git(repo, "add", *to_add)
        staged = git_ops._git(repo, "diff", "--cached", "--name-only", check=False).stdout.strip()
        if not staged:
            return False

        git_ops._git(repo, "commit", "-m", "chore: auto-commit state files before improvement cycle")
        git_ops._git(repo, "push", "origin", git_ops.current_branch(repo), timeout=60)
        git_ops.log.info("Auto-committed state files: %s", ", ".join(to_add))
        return True

    git_ops.commit_auto_state = commit_auto_state


_patch_git_ops_porcelain_parser()
