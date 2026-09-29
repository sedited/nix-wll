"""Git collection and bounded reads from a pull request snapshot."""
import re
import subprocess
from dataclasses import dataclass
from types import MappingProxyType
from pathlib import Path

MAX_REVIEW_BYTES = 200_000
MAX_FILE_BYTES = 1_000_000
MAX_TOOL_BYTES = 12_000
MAX_FOCUSED_DIFF_BYTES = 80_000
MAX_AUDIT_DOC_BYTES = 100_000
SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")

def git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=True, capture_output=True,
        text=True, timeout=180,
    ).stdout

def prepare_checkout(checkout, bot_config):
    if not (checkout / ".git").exists():
        checkout.mkdir(parents=True, exist_ok=True)
        git(checkout, "init", "-q")
        git(checkout, "remote", "add", "origin", bot_config.origin)
    if git(checkout, "remote", "get-url", "origin").strip() != bot_config.origin:
        raise ValueError("checkout origin does not match configured repository")

def collect_review(checkout, number, base_ref, expected_head, title, description, bot_config):
    prepare_checkout(checkout, bot_config)
    git(checkout, "fetch", "--no-tags", "--filter=blob:none", "origin",
        f"+refs/heads/{base_ref}:refs/review-bot/base",
        f"+refs/pull/{number}/head:refs/review-bot/head")
    actual_head = git(checkout, "rev-parse", "refs/review-bot/head").strip()
    base_sha = git(checkout, "rev-parse", "refs/review-bot/base").strip()
    if actual_head != expected_head:
        return base_sha, actual_head, None, "PR head changed before review"
    merge_base = git(checkout, "merge-base", base_sha, actual_head).strip()
    commits = git(checkout, "log", "--reverse", "--format=%H%n%B%n%x00", f"{merge_base}..{actual_head}")
    patch = git(checkout, "diff", "--no-ext-diff", "--binary", f"{merge_base}..{actual_head}")
    prelude = (f"PR title: {title}\nPR description:\n{description}\n"
               f"Commits:\n{commits}\n")
    review = f"{prelude}Patch:\n{patch}"
    if len(review.encode()) > MAX_REVIEW_BYTES:
        changed = git(checkout, "diff", "--no-ext-diff", "--name-status",
                      f"{merge_base}..{actual_head}")
        review = (f"{prelude}Patch exceeds {MAX_REVIEW_BYTES} input bytes. "
                  "Use read_diff to inspect changed files.\nChanged files:\n"
                  f"{changed}")
        if len(review.encode()) > MAX_REVIEW_BYTES:
            return base_sha, actual_head, None, f"Review input exceeds {MAX_REVIEW_BYTES} bytes"
    return base_sha, actual_head, review, None

def tracked_files(checkout, ref="HEAD"):
    """Map tracked regular paths at a Git object to blob IDs."""
    return tracked_files_at(checkout, ref)

def tracked_files_at(checkout, ref):
    entries = git(checkout, "ls-tree", "-r", "-z", "--full-tree", ref).split("\x00")
    files = {}
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if kind == "blob" and mode in {"100644", "100755"}:
            files[path] = blob
    return files

def find_paths(files, query):
    if (not isinstance(query, str) or not 1 <= len(query) <= 100
            or "\n" in query or "\r" in query or "\x00" in query):
        return "Path query must be 1 to 100 characters on one line."
    result = ""
    for path in files:
        if query.casefold() not in path.casefold():
            continue
        line = f"{path}\n"
        if len((result + line).encode()) > MAX_TOOL_BYTES:
            return result + "[Results truncated]"
        result += line
    return result or "No matching tracked files."

def read_file(checkout, files, path, start_line):
    if (not isinstance(path, str) or path not in files
            or not isinstance(start_line, int) or isinstance(start_line, bool)
            or start_line < 1):
        return "Invalid path or line. Only tracked regular files can be read."
    size = int(git(checkout, "cat-file", "-s", files[path]).strip())
    if size > MAX_FILE_BYTES:
        return f"File exceeds {MAX_FILE_BYTES} bytes."
    content = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "blob", files[path]],
        check=True, capture_output=True, timeout=30,
    ).stdout.decode(errors="replace")
    if "\x00" in content:
        return "Binary file cannot be read as text."
    lines = content.splitlines()
    if start_line > len(lines):
        return f"{path} has {len(lines)} lines."
    result = f"{path} ({len(lines)} lines):\n"
    for number in range(start_line, min(start_line + 150, len(lines) + 1)):
        line = f"{number}: {lines[number - 1]}\n"
        if len((result + line).encode()) > MAX_TOOL_BYTES:
            break
        result += line
    return result

def read_diff(checkout, changed_paths, base, head, path, start_line):
    if (not isinstance(path, str) or path not in changed_paths
            or not isinstance(start_line, int) or isinstance(start_line, bool)
            or not 1 <= start_line <= 100_000):
        return "Invalid changed path or diff line."
    process = subprocess.Popen(
        ["git", "-C", str(checkout), "diff", "--no-ext-diff", "--no-color",
         f"{base}..{head}", "--", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    result = f"{path} (diff lines from {start_line}):\n"
    found = False
    stopped_early = False
    try:
        for number, line in enumerate(process.stdout, 1):
            if number < start_line:
                continue
            found = True
            item = f"{number}: {line}"
            if len((result + item).encode()) > MAX_TOOL_BYTES:
                result += f"[Diff line {number} exceeds output limit; continue at {number + 1}]\n"
                stopped_early = True
                break
            result += item
            if number >= start_line + 149:
                stopped_early = True
                break
    finally:
        if stopped_early and process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()
    if not stopped_early and process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args)
    return result if found else result + "No diff lines."

def search_code(checkout, head, query):
    if (not isinstance(query, str) or not 3 <= len(query) <= 100
            or "\n" in query or "\r" in query or "\x00" in query):
        return "Search query must be 3 to 100 characters on one line."
    process = subprocess.Popen(
        ["git", "-C", str(checkout), "grep", "-n", "-I", "-F", "-e", query,
         head, "--"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        output = process.stdout.read(MAX_TOOL_BYTES + 1)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()
    if not output:
        return "No matches in tracked text files."
    return output[:MAX_TOOL_BYTES].decode(errors="replace") + (
        "\n[Results truncated]" if len(output) > MAX_TOOL_BYTES else "")

def blame_base(checkout, base_files, base, path, start_line):
    if (not isinstance(path, str) or path not in base_files
            or not isinstance(start_line, int) or isinstance(start_line, bool)
            or not 1 <= start_line <= 100_000):
        return "Invalid path or line at the PR merge base."
    try:
        output = git(checkout, "blame", "--no-progress", "--line-porcelain",
                     "-L", f"{start_line},+20", base, "--", path)
    except subprocess.CalledProcessError:
        return "No lines at that location in the PR merge base."
    summaries = {}
    lines = []
    current = None
    for line in output.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$", line)
        if header:
            current = (header.group(1), int(header.group(2)))
        elif line.startswith("summary ") and current:
            summaries[current[0]] = line[8:]
        elif line.startswith("\t") and current:
            lines.append((current[0], current[1], line[1:]))
    result = f"{path} at merge base {base}:\n"
    for commit, number, content in lines:
        item = f"{number}: {commit} {summaries.get(commit, '')}: {content}\n"
        if len((result + item).encode()) > MAX_TOOL_BYTES:
            return result + "[Results truncated]"
        result += item
    return result if lines else "No blame results."

def read_commit(checkout, base_files, base, commit, path):
    if (not isinstance(commit, str) or not SHA.fullmatch(commit)
            or not isinstance(path, str) or path not in base_files):
        return "Invalid commit or path at the PR merge base."
    ancestor = subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", commit, base],
        capture_output=True, timeout=30,
    )
    if ancestor.returncode:
        return "Commit is not an ancestor of the PR merge base."
    process = subprocess.Popen(
        ["git", "-C", str(checkout), "show", "--format=fuller",
         "--no-ext-diff", "--no-color", commit, "--", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        output = process.stdout.read(MAX_TOOL_BYTES + 1)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()
    return output[:MAX_TOOL_BYTES].decode(errors="replace") + (
        "\n[Commit output truncated]" if len(output) > MAX_TOOL_BYTES else "")

def audit_developer_notes(snapshot):
    checkout, base = snapshot.checkout, snapshot.merge_base
    try:
        notes = git(checkout, "show", f"{base}:doc/developer-notes.md")
    except subprocess.CalledProcessError:
        return "Developer notes are unavailable at the PR merge base."
    content = notes.encode()[:MAX_AUDIT_DOC_BYTES].decode(errors="replace")
    return content + ("\n[Developer notes truncated]"
                      if len(notes.encode()) > MAX_AUDIT_DOC_BYTES else "")

def is_test_path(path):
    return (path.startswith(("test/", "tests/", "qa/", "src/test/", "src/wallet/test/"))
            or "/test/" in path or "/tests/" in path)


def focused_review_input(review, snapshot, name):
    if f"Patch exceeds {MAX_REVIEW_BYTES} input bytes. Use read_diff" not in review:
        return review
    checkout, base, head = snapshot.checkout, snapshot.merge_base, snapshot.head_sha
    paths = sorted(snapshot.changed_paths)
    if name == "tests":
        paths.sort(key=lambda path: not is_test_path(path))
    elif name == "design":
        paths.sort(key=lambda path: is_test_path(path) or path.startswith("doc/"))
    excerpt = []
    size = 0
    for path in paths:
        patch = git(checkout, "diff", "--no-ext-diff", "--no-color",
                    f"{base}..{head}", "--", path)
        if size + len(patch.encode()) > MAX_FOCUSED_DIFF_BYTES:
            excerpt.append(patch.encode()[:MAX_FOCUSED_DIFF_BYTES - size]
                           .decode(errors="replace"))
            excerpt.append("\n[Diff excerpt incomplete. Use read_diff for the remaining "
                           "content of this file and later paths.]\n")
            break
        excerpt.append(patch)
        size += len(patch.encode())
    return review + "\nRelevant diff excerpt:\n" + "".join(excerpt)

def current_head(bot_config, number):
    result = subprocess.run(
        ["git", "ls-remote", bot_config.origin, f"refs/pull/{number}/head"],
        check=True, capture_output=True, text=True, timeout=180,
    ).stdout.strip()
    fields = result.split()
    if len(fields) != 2 or not SHA.fullmatch(fields[0]) or fields[1] != f"refs/pull/{number}/head":
        raise ValueError("Git returned invalid PR head")
    return fields[0]


@dataclass(frozen=True)
class RepositorySnapshot:
    checkout: Path
    base_sha: str
    head_sha: str
    merge_base: str
    head_files: object
    base_files: object
    changed_paths: frozenset[str]


def snapshot_repository(checkout, base_sha, head_sha):
    """Pin every repository read to object IDs captured after the fetch."""
    merge_base = git(checkout, "merge-base", base_sha, head_sha).strip()
    head_files = MappingProxyType(tracked_files_at(checkout, head_sha))
    base_files = MappingProxyType(tracked_files_at(checkout, merge_base))
    changed = git(checkout, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
                  f"{merge_base}..{head_sha}")
    return RepositorySnapshot(checkout, base_sha, head_sha, merge_base,
                              head_files, base_files,
                              frozenset(path for path in changed.split("\x00") if path))
