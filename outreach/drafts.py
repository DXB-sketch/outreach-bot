"""Outreach drafts. Written to files for you to review and send yourself; nothing is ever sent."""

from __future__ import annotations

import re
from pathlib import Path

from .config import Settings
from .llm import LLM, LLMError

REVIEW_SYSTEM = """You review small local businesses in South East Queensland as potential web design clients
for a one-person studio. You get facts from an automated audit. Be sceptical and concrete.
Never invent facts that are not in the input."""

REVIEW_PROMPT = """Business: {name}
Category: {category}
Location: {address} ({distance} km from the studio)
Reviews: {reviews}
Website: {website}
Website check: {check}
Audit issues: {issues}
Homepage text (truncated): {excerpt}

Return JSON:
{{"what_they_do": "one sentence",
  "summary": "2-3 sentences on why they would or would not want a new website",
  "adjustment": integer from -10 to 10 to apply to a rule-based score (negative if the site is actually fine,
                the business looks too small or inactive to pay, or it is clearly a chain; positive if a new site
                would obviously win them more work),
  "adjustment_reason": "one sentence",
  "red_flags": ["short items, empty if none"]}}"""

DRAFT_SYSTEM = """You write first-contact outreach for {sender_name}, who runs {sender_business}, a one-person web design
studio in {sender_location}. Rules:
- Australian English. Plain, warm, specific, no hype, no buzzwords, no exclamation marks.
- Write as "I". Never imply a team. Never invent clients, results, statistics or awards.
- Mention one or two concrete things from the audit, framed as lost customers, not as insults.
- Only state problems listed in the input. If no website was found, say you couldn't find one, never that
  they don't have one. If the site couldn't be checked, don't claim anything about it.
- Offer to send a free one-page mockup of what their site could look like. Soft call to action.
- Email body under 120 words. Do not add a signature or unsubscribe line (added automatically)."""

DRAFT_PROMPT = """Business: {name}
What they do: {what_they_do}
Location: {address}
Website: {website}
Website check: {check}
Top issues: {issues}
Recommended channel: {channel}

Return JSON:
{{"subject": "email subject under 60 characters, no clickbait",
  "email": "email body starting with a greeting",
  "phone_script": "4-6 short lines to say on a call or when walking in, including a one-line opener"}}"""


def review(llm: LLM, biz: dict, audit: dict | None) -> dict:
    audit = audit or {}
    return llm.chat_json("fast", "review", REVIEW_SYSTEM, REVIEW_PROMPT.format(
        name=biz["name"],
        category=biz.get("category") or "unknown",
        address=biz.get("address") or "unknown",
        distance=biz.get("distance_km") if biz.get("distance_km") is not None else "?",
        reviews=f"{biz.get('review_count')} reviews, rating {biz.get('rating')}" if biz.get("review_count") is not None else "unknown",
        website=biz.get("website") or "none",
        check=_check_summary(audit),
        issues="; ".join(audit.get("issues", [])) or "none found",
        excerpt=(audit.get("text_excerpt") or "")[:1500] or "n/a",
    ), max_tokens=500)


def _check_summary(audit: dict) -> str:
    if not audit.get("has_website"):
        if audit.get("social_only"):
            return "they only have a social media or directory page"
        return f"no website found (searched: {audit.get('website_search', 'unknown')}). They may still have one; say 'I couldn't find a website', never 'you don't have one'"
    return {"ok": "homepage loaded and was analysed", "blocked": "site refused automated checks, so quality is unknown",
            "robots": "site asks robots not to crawl it, so quality is unknown"}.get(
        audit.get("check_status"), f"site is broken ({audit.get('check_status')})")


def footer(s: Settings) -> str:
    lines = [s.sender_name, s.sender_business + (f", {s.sender_location}" if s.sender_location else "")]
    lines += [x for x in (s.sender_phone, s.sender_website) if x]
    lines.append("")
    lines.append("If you'd rather not hear from me again, just reply 'no thanks' and I won't contact you again.")
    return "\n".join(lines)


def template_draft(biz: dict, audit: dict | None, s: Settings) -> dict:
    """Fallback used when no LLM is configured or every model fails."""
    issues = (audit or {}).get("issues", [])[:2]
    first = biz["name"]
    if (audit or {}).get("has_website") and (audit or {}).get("reachable") is None:
        observation = "I came across your business and had a few ideas for how your website could bring in more enquiries."
    elif issues and (audit or {}).get("has_website"):
        observation = "I had a look at your website and noticed a couple of things that might be costing you enquiries: " + \
            "; ".join(i[0].lower() + i[1:] for i in issues) + "."
    else:
        observation = "I couldn't find a website for the business, which means people searching on Google for what you do are probably finding someone else first."
    return {
        "subject": f"A quick idea for {first}",
        "email": f"Hi there,\n\nI'm {s.sender_name}, a local web designer in {s.sender_location}. {observation}\n\n"
                 f"I'd be happy to put together a free one-page mockup showing what a modern site for {first} could look like. "
                 f"No obligation. If it's not useful, no worries at all.\n\nWould that be worth a look?",
        "phone_script": f"Hi, is this {first}? My name's {s.sender_name}, I'm a local web designer in {s.sender_location}.\n"
                        f"{observation}\nI'd like to mock up a free one-page example for you, no strings attached.\n"
                        "Would it be alright if I sent it through, or dropped by to show you?",
    }


def write_draft(llm: LLM | None, biz: dict, audit: dict | None, score: dict, channel: str, s: Settings) -> Path:
    llm_review = score.get("llm") or {}
    draft = None
    if llm:
        try:
            draft = llm.chat_json(
                "strong", "draft",
                DRAFT_SYSTEM.format(sender_name=s.sender_name, sender_business=s.sender_business,
                                    sender_location=s.sender_location),
                DRAFT_PROMPT.format(
                    name=biz["name"],
                    what_they_do=llm_review.get("what_they_do") or biz.get("category") or "unknown",
                    address=biz.get("address") or "unknown",
                    website=biz.get("website") or "none",
                    check=_check_summary(audit or {}),
                    issues="; ".join((audit or {}).get("issues", [])[:4]) or "none",
                    channel=channel,
                ),
                max_tokens=900,
            )
            if not all(isinstance(draft.get(k), str) and draft[k].strip() for k in ("subject", "email", "phone_script")):
                draft = None
        except LLMError:
            draft = None
    generated_by = "LLM" if draft else "template"
    draft = draft or template_draft(biz, audit, s)

    email_to = biz.get("email") or ", ".join((audit or {}).get("emails_found", [])[:2]) or "(no email found)"
    slug = re.sub(r"[^a-z0-9]+", "-", biz["name"].lower()).strip("-")[:50]
    path = s.drafts_dir / f"{biz['id']:05d}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    issues_md = "\n".join(f"- {i}" for i in (audit or {}).get("issues", [])) or "- none recorded"
    path.write_text(f"""# {biz['name']}  (score {score['score']})

**Recommended channel:** {channel}
**Category:** {biz.get('category') or '?'} · **Distance:** {biz.get('distance_km', '?')} km
**Address:** {biz.get('address') or '?'}
**Phone:** {biz.get('phone') or '?'} · **Email:** {email_to}
**Website:** {biz.get('website') or 'none'}
**Draft written by:** {generated_by}. Read and edit before sending. Nothing has been sent.

## Why they scored this way
{llm_review.get('summary', '')}

{chr(10).join('- ' + r for r in score.get('reasons', []))}

## Audit issues
{issues_md}

## Email draft
**To:** {email_to}
**Subject:** {draft['subject']}

{draft['email'].strip()}

{footer(s)}

## Phone / walk-in script
{draft['phone_script'].strip()}
""")
    return path
