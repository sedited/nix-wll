"""Forgejo API access, public discussion reads, and review comments."""
from datetime import datetime, timezone
import json
import urllib.error
import urllib.parse
import urllib.request

from .repository import MAX_TOOL_BYTES, current_head

MAX_DISCUSSION_RESPONSE_BYTES = 500_000
MAX_COMMENT_PAGES = 1000
DISCUSSION_PAGE_LIMIT = 10


def _retrieved_at():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")

def forgejo_request(bot_config, token, path, method="GET", data=None):
    headers = {"Authorization": f"token {token}", "Accept": "application/json",
               "User-Agent": "ForgejoReviewBot/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{bot_config.forgejo_api}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers, method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def public_discussion_request(bot_config, path):
    """Read only discussion data available without Forgejo credentials."""
    request = urllib.request.Request(
        f"{bot_config.forgejo_api}{path}",
        headers={"Accept": "application/json", "User-Agent": "ForgejoReviewBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read(MAX_DISCUSSION_RESPONSE_BYTES + 1)
    if len(content) > MAX_DISCUSSION_RESPONSE_BYTES:
        raise ValueError("Public discussion response exceeds context limit")
    return json.loads(content)

def search_discussions(bot_config, query, current_pr):
    if (not isinstance(query, str) or not 3 <= len(query) <= 100
            or any(char in query for char in "\r\n\x00")):
        return "Search query must be 3 to 100 characters on one line."
    path = "/issues?state=all&limit=10&q=" + urllib.parse.quote(query)
    try:
        issues = public_discussion_request(bot_config, path)
    except ValueError:
        return "Public search response is unavailable or exceeds the context limit."
    if not isinstance(issues, list):
        return "Forgejo returned invalid search results."
    result = "Public issue and PR matches in this repository:\n"
    for issue in issues[:10]:
        if (not isinstance(issue, dict) or not isinstance(issue.get("number"), int)
                or issue["number"] == current_pr):
            continue
        kind = "PR" if issue.get("pull_request") else "Issue"
        title = str(issue.get("title") or "").replace("\n", " ")[:200]
        number = issue["number"]
        url_kind = "pulls" if kind == "PR" else "issues"
        item = f"{kind} #{number}: {title} ({bot_config.repository_url}/{url_kind}/{number})\n"
        if len((result + item).encode()) > MAX_TOOL_BYTES:
            return result + "[Results truncated]"
        result += item
    return result if len(result.splitlines()) > 1 else "No matching discussions."

def read_discussion(bot_config, number, current_pr):
    if (not isinstance(number, int) or isinstance(number, bool)
            or not 1 <= number <= 10_000_000):
        return "Invalid issue or PR number."
    if number == current_pr:
        return "The current PR's discussion is excluded from this review."
    try:
        issue = public_discussion_request(bot_config, f"/issues/{number}")
    except ValueError:
        return "Public discussion is unavailable or exceeds the context limit."
    if not isinstance(issue, dict) or issue.get("number") != number:
        return "Forgejo returned an invalid discussion."
    kind = "PR" if issue.get("pull_request") else "Issue"
    url_kind = "pulls" if kind == "PR" else "issues"
    title = str(issue.get("title") or "")[:300]
    body = str(issue.get("body") or "")[:3000]
    result = (f"{kind} #{number}: {title}\n"
              f"{bot_config.repository_url}/{url_kind}/{number}\n"
              f"Description:\n{body}\n")
    if len(result.encode()) > MAX_TOOL_BYTES:
        return (result.encode()[:MAX_TOOL_BYTES].decode(errors="replace")
                + "\n[Description truncated]")
    try:
        comments = public_discussion_request(bot_config, f"/issues/{number}/comments?limit=20&page=1")
    except ValueError:
        return result + "Comments exceed the public context response limit."
    if not isinstance(comments, list):
        return result + "Forgejo returned invalid comments."
    human = [comment for comment in comments if isinstance(comment, dict)
             and bot_config.comment_marker not in str(comment.get("body") or "")]
    selected = human[:2] + human[-6:] if len(human) > 8 else human
    result += f"Selected comments ({len(selected)} of {len(human)}):\n"
    seen = set()
    for comment in selected:
        if comment.get("id") in seen:
            continue
        seen.add(comment.get("id"))
        author = (comment.get("user") or {}).get("login") or comment.get("original_author") or "unknown"
        content = str(comment.get("body") or "")[:1000]
        item = f"{author}: {content}\n"
        if len((result + item).encode()) > MAX_TOOL_BYTES:
            return result + "[Comments truncated]"
        result += item
    return result

def _comment_author(comment):
    return ((comment.get("user") or {}).get("login")
            or comment.get("original_author") or "unknown")


def _is_bot_comment(comment, bot_config):
    body = str(comment.get("body") or comment.get("content") or "")
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    login = str(user.get("login") or "")
    kind = str(user.get("type") or "")
    return (bot_config.comment_marker in body or kind.lower() == "bot"
            or login.endswith("[bot]"))


def _append_comments(result, comments, bot_config, *, limit=DISCUSSION_PAGE_LIMIT):
    if not isinstance(comments, list):
        return result + "Forgejo returned invalid comments.\n"
    human = [comment for comment in comments if isinstance(comment, dict)
             and not _is_bot_comment(comment, bot_config)]
    selected = human[:limit]
    result += f"Comments on this page ({len(selected)} of {len(human)} non-bot):\n"
    seen = set()
    for comment in selected:
        if comment.get("id") in seen:
            continue
        seen.add(comment.get("id"))
        author = _comment_author(comment)
        identifier = comment.get("id")
        identifier_text = f" id={identifier}" if identifier is not None else ""
        body = str(comment.get("body") or comment.get("content") or "")
        content = body[:1500] + ("\n[Comment truncated]" if len(body) > 1500 else "")
        url = (comment.get("html_url") or comment.get("pull_request_url")
               or f"{bot_config.repository_url}/pulls")
        item = f"{author}{identifier_text} ({url}): {content}\n"
        if len((result + item).encode()) > MAX_TOOL_BYTES:
            return result + "[Comments truncated]\n"
        result += item
    return result


def _public_discussion_page(bot_config, path, *, page, limit=DISCUSSION_PAGE_LIMIT):
    separator = "&" if "?" in path else "?"
    try:
        items = public_discussion_request(
            bot_config, f"{path}{separator}limit={limit}&page={page}")
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None, True
    if not isinstance(items, list):
        return None, True
    return items, len(items) == limit


def _slice_page(items, page, limit=DISCUSSION_PAGE_LIMIT):
    start = (page - 1) * limit
    selected = items[start:start + limit]
    return selected, len(items) > start + limit


def read_current_pr_discussion(bot_config, number, kind="comments", page=1,
                               review_id=0):
    if (not isinstance(number, int) or isinstance(number, bool)
            or not 1 <= number <= 10_000_000):
        return "Invalid pull request number."
    if (not isinstance(page, int) or isinstance(page, bool)
            or not 1 <= page <= MAX_COMMENT_PAGES):
        return f"Page must be 1 to {MAX_COMMENT_PAGES}."
    if kind not in {"comments", "inline", "reviews"}:
        return "Kind must be comments, inline, or reviews."
    if (not isinstance(review_id, int) or isinstance(review_id, bool)
            or review_id < 0):
        return "review_id must be a nonnegative integer."
    if kind == "inline" and review_id < 1:
        return "Use kind=reviews first, then pass a positive review_id for inline comments."
    try:
        issue = public_discussion_request(bot_config, f"/issues/{number}")
    except ValueError:
        return "Current PR discussion is unavailable or exceeds the context limit."
    if not isinstance(issue, dict) or issue.get("number") != number:
        return "Forgejo returned an invalid current PR discussion."
    title = str(issue.get("title") or "")[:300]
    raw_body = str(issue.get("body") or "")
    body = raw_body[:4000] + ("\n[Description truncated]" if len(raw_body) > 4000 else "")
    result = (f"Current PR #{number}: {title}\n"
              f"{bot_config.repository_url}/pulls/{number}\n"
              f"Retrieved at: {_retrieved_at()}\n"
              f"Description:\n{body}\n")
    if len(result.encode()) > MAX_TOOL_BYTES:
        return (result.encode()[:MAX_TOOL_BYTES].decode(errors="replace")
                + "\n[Description truncated]\n")
    if kind == "comments":
        comments, truncated = _public_discussion_page(
            bot_config, f"/issues/{number}/comments", page=page)
        result += f"Issue comments page {page}:\n"
        result = _append_comments(result, comments, bot_config)
    elif kind == "inline":
        truncated = True
        try:
            comments = public_discussion_request(
                bot_config, f"/pulls/{number}/reviews/{review_id}/comments")
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            comments = None
            truncated = True
        if isinstance(comments, list):
            comments, truncated = _slice_page(comments, page)
        result += f"Inline review comments for review {review_id}, page {page}:\n"
        result = _append_comments(result, comments, bot_config)
    else:
        reviews, truncated = _public_discussion_page(
            bot_config, f"/pulls/{number}/reviews", page=page)
        result += f"Pull review summaries page {page}:\n"
        result = _append_comments(result, reviews, bot_config)
    if truncated:
        result += f"{kind} may continue on page {page + 1}.\n"
    return result


def _github_api(path):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ForgejoReviewBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read(MAX_DISCUSSION_RESPONSE_BYTES + 1)
    if len(content) > MAX_DISCUSSION_RESPONSE_BYTES:
        raise ValueError("GitHub response exceeds context limit")
    return json.loads(content)


def _parse_github_issue_url(url):
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "github.com":
        return None
    path = [part for part in parts.path.split("/") if part]
    if len(path) < 4 or path[2] not in {"pull", "pulls", "issues"}:
        return None
    try:
        number = int(path[3])
    except ValueError:
        return None
    owner, repo = path[0], path[1]
    if not owner.replace("-", "").replace("_", "").isalnum():
        return None
    if not repo.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return None
    return owner, repo, number


def _github_page(path, *, page, per_page=DISCUSSION_PAGE_LIMIT):
    separator = "&" if "?" in path else "?"
    try:
        items = _github_api(f"{path}{separator}per_page={per_page}&page={page}")
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None, True
    if not isinstance(items, list):
        return None, True
    return items, len(items) == per_page


def _github_fragment_comment(base, fragment):
    if not isinstance(fragment, str):
        return None, False
    try:
        if fragment.startswith("discussion_r"):
            identifier = int(fragment.removeprefix("discussion_r"))
            return _github_api(f"{base}/pulls/comments/{identifier}"), True
        if fragment.startswith("issuecomment-"):
            identifier = int(fragment.removeprefix("issuecomment-"))
            return _github_api(f"{base}/issues/comments/{identifier}"), True
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None, True
    return None, False


def read_github_discussion(bot_config, url, kind="comments", page=1):
    parsed = _parse_github_issue_url(url)
    if parsed is None:
        return "URL must be a public GitHub pull or issue URL."
    if (not isinstance(page, int) or isinstance(page, bool)
            or not 1 <= page <= MAX_COMMENT_PAGES):
        return f"Page must be 1 to {MAX_COMMENT_PAGES}."
    if kind not in {"comments", "inline", "reviews"}:
        return "Kind must be comments, inline, or reviews."
    owner, repo, number = parsed
    base = f"/repos/{owner}/{repo}"
    fragment = urllib.parse.urlsplit(url).fragment
    try:
        issue = _github_api(f"{base}/issues/{number}")
    except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return "GitHub discussion is unavailable or exceeds the context limit."
    title = str(issue.get("title") or "")[:300] if isinstance(issue, dict) else ""
    raw_body = str(issue.get("body") or "") if isinstance(issue, dict) else ""
    body = raw_body[:4000] + ("\n[Description truncated]" if len(raw_body) > 4000 else "")
    result = (f"GitHub discussion {owner}/{repo} #{number}: {title}\n"
              f"https://github.com/{owner}/{repo}/pull/{number}\n"
              f"Retrieved at: {_retrieved_at()}\n"
              f"Description:\n{body}\n")
    fragment_comment, had_fragment = _github_fragment_comment(base, fragment)
    if had_fragment:
        result += "GitHub referenced comment:\n"
        result = _append_comments(
            result, [fragment_comment] if isinstance(fragment_comment, dict) else None,
            bot_config)
        return result
    if kind == "comments":
        comments, truncated = _github_page(
            f"{base}/issues/{number}/comments", page=page)
        result += f"GitHub issue comments page {page}:\n"
    elif kind == "inline":
        comments, truncated = _github_page(
            f"{base}/pulls/{number}/comments", page=page)
        result += f"GitHub inline review comments page {page}:\n"
    else:
        comments, truncated = _github_page(
            f"{base}/pulls/{number}/reviews", page=page)
        result += f"GitHub review summaries page {page}:\n"
    result = _append_comments(result, comments, bot_config)
    if truncated:
        result += f"GitHub {kind} may continue on page {page + 1}.\n"
    return result

def pull_request_context(bot_config, token, number):
    issue = forgejo_request(bot_config, token, f"/issues/{number}")
    if (not isinstance(issue, dict) or issue.get("number") != number
            or not isinstance(issue.get("pull_request"), dict)):
        raise ValueError("Forgejo returned invalid pull request")
    title, description = issue.get("title"), issue.get("body")
    if not isinstance(title, str) or not (description is None or isinstance(description, str)):
        raise ValueError("Forgejo returned invalid pull request text")
    return title, description or ""

def find_comment(bot_config, token, number, bot_login):
    page = 1
    marker_from_other_user = False
    seen_pages = set()
    while True:
        comments = forgejo_request(bot_config, token, f"/issues/{number}/comments?limit=50&page={page}")
        if not isinstance(comments, list):
            raise ValueError("Forgejo returned invalid comments")
        ids = tuple(comment.get("id") for comment in comments)
        if ids in seen_pages:
            break
        seen_pages.add(ids)
        for comment in comments:
            if bot_config.comment_marker in comment.get("body", ""):
                if comment.get("user", {}).get("login") == bot_login:
                    return comment
                marker_from_other_user = True
        if len(comments) < 50:
            break
        page += 1
    if marker_from_other_user:
        raise ValueError("Review marker belongs to another user")
    return None

def review_body(bot_config, prompt_config, base_sha, head_sha, content, debug=None):
    report_link = (f"\n[Full review report](<{debug['report_url']}>)\n"
                   if debug is not None and debug.get("report_url") else "")
    return (f"{bot_config.comment_marker}\n"
            f"Base: `{base_sha}`  \nHead: `{head_sha}`\n\n"
            f"{content.strip()}\n"
            f"{report_link}")

def comment_matches_head(comment, head_sha):
    return (comment is not None
            and f"Head: `{head_sha}`" in comment.get("body", "").splitlines()[:5])

def publish_review(bot_config, prompt_config, token, number, bot_login, base_sha, head_sha, content, debug=None):
    body = review_body(bot_config, prompt_config, base_sha, head_sha, content, debug)
    comment = find_comment(bot_config, token, number, bot_login)
    # Check as close as possible to publication, after paginating old comments.
    if current_head(bot_config, number) != head_sha:
        return "stale"
    if comment is None:
        created = forgejo_request(bot_config, token, f"/issues/{number}/comments", "POST", {"body": body})
        if created.get("user", {}).get("login") != bot_login:
            raise ValueError("Forgejo token does not belong to bot account")
        return "created"
    if comment.get("body") == body:
        return "unchanged"
    forgejo_request(bot_config, token, f"/issues/comments/{comment['id']}", "PATCH", {"body": body})
    return "updated"
