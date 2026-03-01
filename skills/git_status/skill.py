"""Git status skill - check repository status and recent commits using subprocess."""

import subprocess


def repo_status(path: str = ".") -> dict:
    """Get git repo status. Returns branch, clean, modified, untracked, staged."""
    try:
        status_result = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status_result.returncode != 0:
            return {"error": "not a git repository or path invalid"}

        branch_result = subprocess.run(
            ["git", "-C", path, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

        modified = []
        untracked = []
        staged = []
        for line in status_result.stdout.strip().splitlines():
            if not line:
                continue
            x, y = line[0], line[1]
            name = line[3:].strip()
            if x in "MADRC":
                staged.append(name)
            if y in "MD":
                modified.append(name)
            if x == "?" and y == "?":
                untracked.append(name)

        return {
            "branch": branch,
            "clean": len(modified) == 0 and len(untracked) == 0 and len(staged) == 0,
            "modified": modified,
            "untracked": untracked,
            "staged": staged,
        }
    except subprocess.TimeoutExpired:
        return {"error": "command timed out"}
    except Exception as e:
        return {"error": str(e)}


def recent_commits(path: str = ".", count: int = 5) -> dict:
    """Get recent commits. Returns commits list with hash, author, message, date."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "log", "--oneline", "-n", str(count), "--format=%H|%an|%s|%ci"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"error": "not a git repository or path invalid"}

        commits = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) >= 4:
                commits.append({
                    "hash": parts[0],
                    "author": parts[1],
                    "message": parts[2],
                    "date": parts[3],
                })
        return {"commits": commits}
    except subprocess.TimeoutExpired:
        return {"error": "command timed out"}
    except Exception as e:
        return {"error": str(e)}
