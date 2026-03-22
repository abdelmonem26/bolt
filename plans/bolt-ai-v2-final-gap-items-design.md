# Bolt AI v2 -- Final Gap Items Design

## Context

7 commits already landed on `feature/config-as-parameter-and-ci-cd` covering all P1/P2/P3 code items. This plan covers the 6 remaining items from the cross-referenced gap analysis.

---

## Item 1: Pre-commit Hooks

**Status:** `.pre-commit-config.yaml` already created (uncommitted). Just needs committing.

**Design:** Standard pre-commit with ruff lint, ruff format, mypy, plus basic file hygiene hooks (trailing whitespace, end-of-file-fixer, check-yaml, check-json).

**Files:** `.pre-commit-config.yaml` (already exists)

---

## Item 2: Avatar Body Language Config

**Pre-plan reference:** Section 4 -- "Eyes on camera at all times, hands visible and active, slight forward lean on key points, never static between sentences, never closed posture."

**Design:** Add an `avatar_config` section to `config.json` with provider-specific parameters. These are passed to the Vidnoz/D-ID/HeyGen API calls as configuration hints. Most avatar APIs support eye direction, idle motion, and posture settings.

```json
"avatar_config": {
    "eye_direction": "camera",
    "idle_motion": true,
    "gesture_intensity": "medium",
    "posture": "open_slight_angle",
    "emphasis_lean": true,
    "notes": "Pre-plan Section 4: eyes on camera, hands visible, forward lean on key points, never static, open posture"
}
```

**Integration:** `video_pipeline.py` reads these values when calling avatar APIs and passes them as parameters where supported by the provider.

---

## Item 3: Affiliate Link Rotation

**Pre-plan reference:** Section 22 -- "Affiliate link rotation. Caption composer selects the highest-earning affiliate link relevant to the specific video topic, not a generic link."

**Design:** Add an `affiliate_links` section to `config.json` mapping content pillars to affiliate URLs. The `caption_composer.py` selects the best link based on the video's pillar and article keywords.

```json
"affiliate_links": {
    "ai_tools": [
        {"url": "https://example.com/tool1?ref=bolt", "label": "Try it free", "keywords": ["productivity", "automation"]},
        {"url": "https://example.com/tool2?ref=bolt", "label": "Get started", "keywords": ["coding", "developer"]}
    ],
    "ai_news": [
        {"url": "https://example.com/newsletter?ref=bolt", "label": "Daily AI digest", "keywords": []}
    ],
    "default": {"url": "", "label": ""}
}
```

**Integration:** `caption_composer.py` gets a `_select_affiliate_link(pillar, article_title, config)` function that scores links by keyword overlap and returns the best match. YouTube descriptions include the affiliate link. TikTok/Instagram bios reference "link in bio".

---

## Item 4: Dashboard Loading Skeletons

**Design:** Create a reusable `LoadingSkeleton` component that renders animated placeholder shapes matching each page's layout. Each page wraps its data-dependent content in a loading state check.

**Pattern:**
```
if loading -> show LoadingSkeleton
if error -> show ErrorState with retry button
if data -> show actual content
```

**Files:**
- New: `src/components/ui/LoadingSkeleton.tsx` -- generic skeleton primitives
- New: `src/components/ErrorState.tsx` -- error display with retry
- Modified: `Dashboard.tsx`, `ContentManagement.tsx`, `Analytics.tsx`, `NewsMonitor.tsx`, `CostBackups.tsx` -- wrap in loading/error states

---

## Item 5: Dashboard Auth UI (Login Page)

**Design:** A simple login page that collects the API key and stores it in localStorage. The `api.ts` client reads the key from localStorage if `VITE_API_KEY` is not set. When the API returns 401, the user is redirected to login.

**Files:**
- New: `src/pages/Login.tsx` -- API key input form
- Modified: `src/lib/api.ts` -- read key from localStorage, handle 401
- Modified: `src/App.tsx` -- add `/login` route, auth guard wrapper

**Flow:**
```
App loads -> check localStorage for API key
  -> key exists: render normal routes
  -> no key AND backend returns 401: redirect to /login
  -> /login: user enters key, stored in localStorage, redirect to /
```

---

## Item 6: sys.path.insert Removal

**Current state:** `content_automation_master.py` line 28, `api.py` line 39, and `tests/conftest.py` line 15 all use `sys.path.insert(0, ...)`.

**Design:** The project already has `pyproject.toml` with `pythonpath = ["code"]` for pytest, and `__init__.py` exists in `code/`. The fix is:
- Use `python -m code.api` instead of `python code/api.py` as the entry point
- In Dockerfile/docker-compose, change CMD to `python -m code.api`
- For the orchestrator, use `python -m code.content_automation_master`
- Remove `sys.path.insert` lines from the source files
- Keep `tests/conftest.py` sys.path as-is (pytest already handles this via `pyproject.toml`)

**Risk:** This changes entry point invocation across Dockerfile, docker-compose, and scripts. Needs coordinated update.

---

## Execution Order

1. Config changes (avatar_config + affiliate_links) -- pure data, no risk
2. Caption composer affiliate integration -- small code change
3. Video pipeline avatar config pass-through -- small code change
4. Dashboard components (skeletons + error states + login) -- frontend only
5. sys.path removal + entry point updates -- coordinated change
6. Commit pre-commit config

---

## Files to Create/Modify

| File | Action | Item |
|------|--------|------|
| `code/config.json` | Modify -- add avatar_config + affiliate_links sections | 2, 3 |
| `code/caption_composer.py` | Modify -- add affiliate link selection | 3 |
| `code/video_pipeline.py` | Modify -- pass avatar_config to provider calls | 2 |
| `bolt-dashboard/src/components/ui/LoadingSkeleton.tsx` | Create | 4 |
| `bolt-dashboard/src/components/ErrorState.tsx` | Create | 4 |
| `bolt-dashboard/src/pages/Login.tsx` | Create | 5 |
| `bolt-dashboard/src/pages/Dashboard.tsx` | Modify -- add loading/error states | 4 |
| `bolt-dashboard/src/pages/ContentManagement.tsx` | Modify -- add loading/error states | 4 |
| `bolt-dashboard/src/pages/Analytics.tsx` | Modify -- add loading/error states | 4 |
| `bolt-dashboard/src/pages/NewsMonitor.tsx` | Modify -- add loading/error states | 4 |
| `bolt-dashboard/src/pages/CostBackups.tsx` | Modify -- add loading/error states | 4 |
| `bolt-dashboard/src/lib/api.ts` | Modify -- localStorage key + 401 handling | 5 |
| `bolt-dashboard/src/App.tsx` | Modify -- add login route + auth guard | 5 |
| `code/content_automation_master.py` | Modify -- remove sys.path.insert | 6 |
| `code/api.py` | Modify -- remove sys.path.insert | 6 |
| `Dockerfile` | Modify -- change CMD to python -m | 6 |
| `docker-compose.yml` | Modify -- change command entries | 6 |
