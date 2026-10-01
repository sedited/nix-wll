"""Forgejo API access, public discussion reads, and review comments."""
import json
import urllib.parse
import urllib.request

from .repository import MAX_TOOL_BYTES, current_head

MAX_DISCUSSION_RESPONSE_BYTES = 500_000

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
    from .trace import debug_section

    report_link = (f"\n[Full review report](<{debug['report_url']}>)\n"
                   if debug is not None and debug.get("report_url") else "")
    return (f"{bot_config.comment_marker}\n"
            f"Base: `{base_sha}`  \nHead: `{head_sha}`\n\n"
            f"{content.strip()}\n"
            f"{report_link}"
            f"{debug_section(debug, prompt_config) if debug is not None else ''}")

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
