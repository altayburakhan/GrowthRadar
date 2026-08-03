from __future__ import annotations

# Signature-based, offline tech detection (a small local Wappalyzer-style table)
# instead of a paid BuiltWith/Wappalyzer API -- keeps the platform dependency-free
# and free to run at any volume. Extend by adding an entry; no other code changes.
TECH_SIGNATURES: dict[str, dict[str, object]] = {
    # Onboarding / digital adoption platforms -- the category most directly
    # relevant to UserGuiding's competitive and displacement analysis.
    "UserGuiding": {"category": "onboarding_adoption", "patterns": [r"userguiding\.com", r"ug_widget"]},
    "Appcues": {"category": "onboarding_adoption", "patterns": [r"appcues\.com", r"window\.Appcues"]},
    "Pendo": {"category": "onboarding_adoption", "patterns": [r"pendo\.io", r"pendo\.initialize"]},
    "Userpilot": {"category": "onboarding_adoption", "patterns": [r"userpilot\.io", r"Userpilot\.initialize"]},
    "WalkMe": {"category": "onboarding_adoption", "patterns": [r"walkme\.com", r"walkme_"]},
    "Chameleon": {"category": "onboarding_adoption", "patterns": [r"trychameleon", r"chameleon\.io"]},
    "Userlane": {"category": "onboarding_adoption", "patterns": [r"userlane\.com"]},
    "Whatfix": {"category": "onboarding_adoption", "patterns": [r"whatfix\.com"]},
    "Intro.js": {"category": "onboarding_adoption", "patterns": [r"introjs\.min\.js", r"introJs\("]},
    "Shepherd.js": {"category": "onboarding_adoption", "patterns": [r"shepherd\.js"]},
    # Chat / support widgets
    "Intercom": {"category": "chat_support", "patterns": [r"widget\.intercom\.io", r"window\.Intercom"]},
    "Drift": {"category": "chat_support", "patterns": [r"js\.driftt\.com", r"drift\.load"]},
    "Crisp": {"category": "chat_support", "patterns": [r"client\.crisp\.chat"]},
    "Zendesk Chat": {"category": "chat_support", "patterns": [r"zdassets\.com", r"zopim"]},
    "LiveChat": {"category": "chat_support", "patterns": [r"livechatinc\.com"]},
    "Tawk.to": {"category": "chat_support", "patterns": [r"tawk\.to"]},
    # Product/marketing analytics
    "Google Analytics": {"category": "analytics", "patterns": [r"googletagmanager\.com/gtag", r"google-analytics\.com"]},
    "Google Tag Manager": {"category": "analytics", "patterns": [r"googletagmanager\.com/gtm"]},
    "Mixpanel": {"category": "analytics", "patterns": [r"cdn\.mxpnl\.com", r"mixpanel\.init"]},
    "Amplitude": {"category": "analytics", "patterns": [r"cdn\.amplitude\.com"]},
    "Segment": {"category": "analytics", "patterns": [r"cdn\.segment\.com"]},
    "Heap": {"category": "analytics", "patterns": [r"heap\.io", r"heapanalytics\.com"]},
    "PostHog": {"category": "analytics", "patterns": [r"app\.posthog\.com"]},
    "Hotjar": {"category": "analytics", "patterns": [r"static\.hotjar\.com"]},
    "FullStory": {"category": "analytics", "patterns": [r"fullstory\.com/s/fs\.js"]},
    # Help centers / documentation -- proxy for existing self-serve support investment
    "Zendesk Help Center": {"category": "help_center", "patterns": [r"zendesk\.com"]},
    "Help Scout": {"category": "help_center", "patterns": [r"helpscout\.net"]},
    "GitBook": {"category": "help_center", "patterns": [r"gitbook\.io"]},
    "Document360": {"category": "help_center", "patterns": [r"document360\.io"]},
    "Notion (docs)": {"category": "help_center", "patterns": [r"notion\.site"]},
    # CRM / marketing automation -- proxy for sales-led vs. self-serve motion
    "HubSpot": {"category": "crm_marketing", "patterns": [r"js\.hs-scripts\.com", r"hsforms\.net"]},
    "Marketo": {"category": "crm_marketing", "patterns": [r"marketo\.net"]},
    "Salesforce": {"category": "crm_marketing", "patterns": [r"salesforce\.com/embeddedservice"]},
    "ActiveCampaign": {"category": "crm_marketing", "patterns": [r"activehosted\.com"]},
    # Payment processors -- confirms an active, self-serve billing SaaS model
    "Stripe": {"category": "payment", "patterns": [r"js\.stripe\.com"]},
    "Paddle": {"category": "payment", "patterns": [r"paddle\.com"]},
    "Chargebee": {"category": "payment", "patterns": [r"chargebee\.com"]},
}
