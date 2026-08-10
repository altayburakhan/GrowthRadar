# AGENTS.md

## Purpose

Build an autonomous AI Web Exploration Agent that behaves like a real first-time user. The objective is to explore SaaS products, gather evidence, and determine whether the product is a strong UserGuiding prospect.

The agent must never rely on a single signal. Decisions must always be evidence-based.

---

# General Rules

- Think before acting.
- Explore before concluding.
- Never assume.
- Prefer observation over hardcoded logic.
- Collect evidence continuously.
- Every conclusion must reference supporting evidence.
- Be resilient to UI differences.
- Recover from failures whenever possible.
- Log every important action.

---

# Exploration Behavior

Act like a real user.

Explore naturally.

Do not immediately inspect source code.

First understand the product.

Then investigate technical details.

Prioritize discovering:

- Sign Up
- Login
- Free Trial
- Dashboard
- Settings
- Navigation
- Projects
- Help Center
- Documentation
- Resource Center
- Product Updates
- Release Notes
- Changelog
- Blog
- Footer links

Explore until no meaningful new pages remain.

Avoid infinite loops.

---

# Registration

If registration is available:

- Create an account.
- Generate realistic user information.
- Generate secure passwords.
- Create realistic workspace/company names.
- Complete onboarding forms.
- Handle multi-step registration.

If email verification is required:

- Create a temporary email.
- Wait for verification.
- Open verification links.
- Continue automatically.

---

# Browser Behavior

Use Playwright.

Always wait for the page to become stable.

Handle:

- popups
- cookie banners
- dialogs
- modals
- redirects
- new tabs
- loading states

Retry failed interactions.

Never terminate because of one failed click.

---

# Exploration Strategy

Explore breadth first.

Visit important sections before deep navigation.

Examples:

Dashboard

Projects

Settings

Analytics

Reports

Billing

Help

Notifications

Team

Admin

Resource Center

Search for hidden navigation.

Inspect menus.

Inspect profile menus.

Inspect footer links.

Inspect documentation links.

---

# Screenshot Policy

Capture screenshots throughout exploration.

Required screenshots:

- Landing page
- Registration
- Login
- Dashboard
- Every major page
- Every popup
- Every onboarding experience
- Empty states
- Product updates
- Help center
- Errors

Store screenshots with timestamps.

---

# DOM Collection

For every important page collect:

- URL
- Title
- HTML
- Visible text
- Navigation structure
- Interactive elements

---

# JavaScript Inspection

Inspect loaded JavaScript.

Look for:

- UserGuiding
- Pendo
- Appcues
- WalkMe
- Chameleon
- Product Fruits
- Intercom
- Shepherd
- Intro.js
- Driver.js

Inspect:

- window object
- global variables
- script URLs

---

# Network Inspection

Monitor network requests.

Record:

- third-party scripts
- analytics providers
- onboarding providers
- API endpoints

---

# Onboarding Detection

Look for evidence of:

- Product Tours
- Tooltips
- Checklists
- Hotspots
- Coach Marks
- Popovers
- Guided Tours
- Interactive Walkthroughs
- Empty State Guides
- Resource Centers

Use:

- DOM
- JavaScript
- Screenshots
- Network requests

---

# AI Vision

Use screenshots to answer questions such as:

- Is onboarding visible?
- Is a product tour active?
- Is there a checklist?
- Is there a tooltip?
- Is this likely UserGuiding?
- What should be explored next?

Vision should complement browser inspection.

Never rely only on screenshots.

---

# Evidence Collection

Every discovery should become structured evidence.

Example:

- Screenshot
- URL
- DOM
- JavaScript
- Network
- Visible UI
- Confidence

Evidence is cumulative.

---

# Decision Making

Never conclude from one signal.

Combine multiple evidence sources.

Confidence should increase only when evidence agrees.

Example:

JS detected

+

Visible onboarding

+

Product Updates mention

+

Network requests

=

High confidence

---

# Error Recovery

If exploration fails:

- retry
- refresh
- reopen page
- continue elsewhere

Never stop because one path failed.

Continue gathering evidence.

---

# Logging

Log:

- visited pages
- actions
- screenshots
- discoveries
- errors
- retries
- decisions

Everything should be reproducible.

---

# Final Report

Generate a structured report.

Include:

- Company
- Product
- Explored pages
- Registration completed
- Trial available
- Onboarding detected
- Evidence collected
- Technologies detected
- Product update pages
- Help center
- Confidence score
- Final recommendation

Every conclusion must reference collected evidence.

Never make unsupported claims.

Evidence first.

Conclusion second.