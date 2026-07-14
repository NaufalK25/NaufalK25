#!/usr/bin/env python3
"""
generate_gitlab_stats.py

Pulls stats for a GitLab user via the GitLab API and renders them
into an SVG card (gitlab-stats.svg) suitable for embedding in a GitHub README:

    ![GitLab Stats](./gitlab-stats.svg)

Card shows: project count, a top-languages bar chart, and a recent
daily-contribution calendar grid (based on push events).

Required environment variables:
    GITLAB_TOKEN     - GitLab Personal Access Token (scope: read_api, read_user)
    GITLAB_USERNAME  - Your GitLab username (e.g. "johndoe")

Optional:
    GITLAB_HOST      - Defaults to https://gitlab.com (set for self-hosted instances)
    OUTPUT_FILE      - Defaults to "gitlab-stats.svg"
"""

import os
import sys
import datetime
import requests
from collections import Counter, defaultdict

try:
    from dotenv import load_dotenv

    load_dotenv()  # loads variables from a .env file in the current directory, if present
except ImportError:
    pass  # dotenv is optional — fine if running where env vars are set another way (e.g. GitHub Actions)

GITLAB_HOST = os.environ.get("GITLAB_HOST", "https://gitlab.com").rstrip("/")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN")
GITLAB_USERNAME = os.environ.get("GITLAB_USERNAME")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "gitlab-stats.svg")

API_BASE = f"{GITLAB_HOST}/api/v4"
HEADERS = {"PRIVATE-TOKEN": GITLAB_TOKEN} if GITLAB_TOKEN else {}


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def get_json(url, params=None):
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        die(f"GitLab API request failed ({resp.status_code}): {url}\n{resp.text[:300]}")
    return resp.json(), resp.headers


def get_user_id(username):
    data, _ = get_json(f"{API_BASE}/users", params={"username": username})
    if not data:
        die(f"No GitLab user found for username '{username}'")
    return data[0]


def get_all_pages(url, params=None, max_pages=10):
    """Fetch all pages up to max_pages (GitLab paginates via headers)."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results = []
    page = 1
    while page <= max_pages:
        params["page"] = page
        data, headers = get_json(url, params=params)
        if not data:
            break
        results.extend(data)
        next_page = headers.get("X-Next-Page")
        if not next_page:
            break
        page = int(next_page)
    return results


def collect_stats(user):
    user_id = user["id"]

    # Projects where the user is a formal member (own repos, org/group repos).
    member_projects = get_all_pages(
        f"{API_BASE}/projects",
        params={"membership": True, "order_by": "last_activity_at"},
    )

    # Projects the user has contributed to (pushes/MRs/comments) but isn't
    # necessarily a member of, e.g. a merge request on someone else's repo.
    # Membership alone misses these, so merge both sets by project id.
    contributed_projects = get_all_pages(
        f"{API_BASE}/users/{user_id}/contributed_projects",
    )

    projects_by_id = {p["id"]: p for p in member_projects}
    for p in contributed_projects:
        projects_by_id.setdefault(p["id"], p)
    projects = list(projects_by_id.values())
    project_count = len(projects)

    # Top languages, aggregated across projects (best-effort; skipped on error)
    lang_totals = Counter()
    for p in projects[:20]:  # cap to avoid excessive API calls
        try:
            langs, _ = get_json(f"{API_BASE}/projects/{p['id']}/languages")
            for lang, pct in langs.items():
                lang_totals[lang] += pct
        except SystemExit:
            # get_json calls die() on failure; treat as "skip this project"
            raise
        except Exception:
            continue

    top_lang_pairs = lang_totals.most_common(5)
    lang_sum = sum(v for _, v in top_lang_pairs) or 1
    top_languages = [(lang, round(v / lang_sum * 100, 1)) for lang, v in top_lang_pairs]

    # Contribution events (push events) as a commit-activity proxy. This is
    # already scoped to the user (not the membership-only project list above),
    # so it includes pushes to any project the user has access to, including
    # ones owned by other users where they contributed without being a formal
    # member. GitLab only omits events for projects the user has since lost
    # visibility into entirely, which isn't recoverable via the API.
    # Build a day -> count map for the most recent year.
    events = get_all_pages(
        f"{API_BASE}/users/{user_id}/events",
        params={"action": "pushed"},
        max_pages=20,
    )

    daily_counts = defaultdict(int)
    for e in events:
        created_at = e.get("created_at", "")
        day = created_at[:10]  # "YYYY-MM-DD"
        if day:
            daily_counts[day] += 1

    # Full rolling year, aligned to full weeks (Sunday-start) like GitHub's own graph.
    today = datetime.date.today()
    start = today - datetime.timedelta(days=364)
    start -= datetime.timedelta(days=(start.weekday() + 1) % 7)
    num_days = (today - start).days + 1
    all_days = [start + datetime.timedelta(days=i) for i in range(num_days)]
    contributions = [
        {"date": d.isoformat(), "count": daily_counts.get(d.isoformat(), 0)}
        for d in all_days
    ]
    total_contributions = sum(c["count"] for c in contributions)

    return {
        "username": user.get("username", ""),
        "name": user.get("name", user.get("username", "")),
        "projects": project_count,
        "top_languages": top_languages,
        "contributions": contributions,
        "total_contributions": total_contributions,
    }


LANG_COLORS = [
    "#3572A5",
    "#f1e05a",
    "#e34c26",
    "#563d7c",
    "#00ADD8",
    "#dea584",
    "#4F5D95",
    "#178600",
]


def render_svg(stats):
    # ---- Grid layout (drives overall card width) ----
    contributions = stats["contributions"]
    n = len(contributions)
    cell_size = 11
    grid_gap = 3
    if n:
        first_date = datetime.date.fromisoformat(contributions[0]["date"])
        start_offset = (first_date.weekday() + 1) % 7  # align so Sunday = row 0
        columns = -(-(n + start_offset) // 7)  # ceil division
    else:
        start_offset = 0
        columns = 1

    grid_width = columns * (cell_size + grid_gap) - grid_gap
    width = max(460, 60 + grid_width)

    # ---- Layout constants ----
    header_h = 60
    lang_row_h = 26
    lang_section_h = 24 + lang_row_h * max(len(stats["top_languages"]), 1)
    section_gap = 20
    padding_bottom = 24

    # ---- Language bar chart ----
    lang_y_start = header_h + 24
    lang_svg = (
        f'<text x="30" y="{header_h + 4}" class="section-title">Top Languages</text>'
    )
    bar_max_width = width - 60 - 90  # leave room for label + percentage
    if stats["top_languages"]:
        for i, (lang, pct) in enumerate(stats["top_languages"]):
            y = lang_y_start + i * lang_row_h
            bar_w = max(bar_max_width * (pct / 100.0), 2)
            color = LANG_COLORS[i % len(LANG_COLORS)]
            lang_svg += f"""
    <text x="30" y="{y}" class="label">{lang}</text>
    <rect x="140" y="{y - 11}" width="{bar_max_width}" height="12" rx="4" fill="#21262d"/>
    <rect x="140" y="{y - 11}" width="{bar_w:.1f}" height="12" rx="4" fill="{color}"/>
    <text x="{width - 30}" y="{y}" class="value" text-anchor="end">{pct}%</text>"""
    else:
        lang_svg += f'<text x="30" y="{lang_y_start}" class="label">No language data available</text>'

    # ---- Contribution grid (calendar heatmap, GitHub-style) ----
    contrib_y_top = header_h + lang_section_h + section_gap
    contrib_svg = f"""
    <text x="30" y="{contrib_y_top + 4}" class="section-title">Recent Contributions</text>
    <text x="{width - 30}" y="{contrib_y_top + 4}" class="label" text-anchor="end">Total: {stats["total_contributions"]}</text>"""

    grid_top = contrib_y_top + 20
    max_count = max((c["count"] for c in contributions), default=0) or 1

    def cell_color(count):
        if count == 0:
            return "#161b22"
        ratio = count / max_count
        if ratio > 0.75:
            return "#39d353"
        if ratio > 0.5:
            return "#26a641"
        if ratio > 0.25:
            return "#006d32"
        return "#0e4429"

    for i, c in enumerate(contributions):
        pos = i + start_offset
        col, row = divmod(pos, 7)
        x = 30 + col * (cell_size + grid_gap)
        y = grid_top + row * (cell_size + grid_gap)
        color = cell_color(c["count"])
        contrib_svg += f"""
    <rect x="{x:.1f}" y="{y:.1f}" width="{cell_size:.1f}" height="{cell_size:.1f}" rx="2" fill="{color}"><title>{c["date"]}: {c["count"]}</title></rect>"""

    grid_height = 7 * (cell_size + grid_gap)
    contrib_section_h = 24 + grid_height

    height = (
        header_h + lang_section_h + section_gap + contrib_section_h + padding_bottom
    )

    svg = f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <style>
    .card {{ fill: #0d1117; stroke: #30363d; stroke-width: 1; }}
    .title {{ font: 600 18px 'Segoe UI', Ubuntu, sans-serif; fill: #58a6ff; }}
    .section-title {{ font: 600 14px 'Segoe UI', Ubuntu, sans-serif; fill: #e6edf3; }}
    .label {{ font: 400 13px 'Segoe UI', Ubuntu, sans-serif; fill: #c9d1d9; }}
    .value {{ font: 600 13px 'Segoe UI', Ubuntu, sans-serif; fill: #e6edf3; }}
    .axis {{ font: 400 9px 'Segoe UI', Ubuntu, sans-serif; fill: #8b949e; }}
  </style>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" class="card"/>
  <text x="30" y="30" class="title">{stats["name"]}'s GitLab Stats</text>
  <text x="{width - 30}" y="30" class="label" text-anchor="end">Projects: {stats["projects"]}</text>
  {lang_svg}
  {contrib_svg}
</svg>"""
    return svg


def main():
    if not GITLAB_TOKEN:
        die("GITLAB_TOKEN environment variable is not set")
    if not GITLAB_USERNAME:
        die("GITLAB_USERNAME environment variable is not set")

    user = get_user_id(GITLAB_USERNAME)
    stats = collect_stats(user)
    svg = render_svg(stats)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(svg)

    print(f"Wrote {OUTPUT_FILE} for user '{stats['username']}'")
    print(stats)


if __name__ == "__main__":
    main()
