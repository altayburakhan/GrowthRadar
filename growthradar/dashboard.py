"""Streamlit dashboard for GrowthRadar. Thin UI layer only -- all logic lives
in core.pipeline / storage.repository, exactly as the CLI uses them."""
from __future__ import annotations

import streamlit as st

from growthradar.config import settings
from growthradar.core.models import CompanyEvidence, LeadScoreResult, Tier
from growthradar.core.pipeline import analyze_company
from growthradar.storage.repository import get_evidence_for_domain, list_results, save_result

TIER_COLOR = {
    Tier.HOT: "🔥",
    Tier.WARM: "🌤️",
    Tier.COLD: "🧊",
    Tier.EXCLUDED: "🚫",
}


def _render_result(result: LeadScoreResult) -> None:
    icon = TIER_COLOR.get(result.tier, "")
    col1, col2 = st.columns([1, 3])
    with col1:
        st.metric("Lead Score", f"{result.overall_score:.1f}/100", f"{icon} {result.tier.value.upper()}")
    with col2:
        st.caption(f"Provider: {result.provider_used}  ·  Confidence: {result.confidence:.0%}")
        if result.disqualifiers:
            for reason in result.disqualifiers:
                st.warning(reason)
        if result.recommended_pitch_angle:
            st.info(f"**Pitch angle:** {result.recommended_pitch_angle}")

    for dim in result.dimensions:
        with st.expander(f"{dim.name.replace('_', ' ').title()} — {dim.score:.0f}/100"):
            st.write(dim.reasoning)
            if dim.evidence:
                st.markdown("\n".join(f"- {e}" for e in dim.evidence))


def _render_evidence(evidence: CompanyEvidence) -> None:
    """Shows the raw collected data behind a score: per-page extracted text
    (about us, pricing, etc.), detected technologies, and derived signals --
    so a score can be inspected, not just trusted."""
    st.markdown("#### Detected technologies")
    if evidence.detected_technologies:
        st.dataframe(
            [{"Tool": t.name, "Category": t.category, "Matched": t.matched_pattern} for t in evidence.detected_technologies],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("No recognizable third-party tools detected.")

    st.markdown("#### Signals")
    s = evidence.signals
    st.json(s.model_dump(), expanded=False)

    st.markdown("#### Pages collected")
    for label, page in evidence.pages.items():
        status = "✅" if page.fetched_ok else f"❌ {page.fetch_error or page.status_code}"
        with st.expander(f"{label} — {page.url}  {status}"):
            if page.fetched_ok:
                st.caption(f"Title: {page.title or '(none)'}")
                if page.meta_description:
                    st.caption(f"Meta description: {page.meta_description}")
                st.text_area("Extracted text", page.text, height=200, key=f"text-{label}-{page.url}", disabled=True)
            else:
                st.caption("This page could not be fetched.")

    if evidence.fetch_errors:
        st.markdown("#### Collection warnings")
        for err in evidence.fetch_errors:
            st.caption(f"⚠️ {err}")


def main() -> None:
    st.set_page_config(page_title="GrowthRadar", page_icon="📡", layout="wide")
    st.title("📡 GrowthRadar")
    st.caption("AI Growth Intelligence for UserGuiding — analyze any company website and get an explainable Lead Score.")

    with st.form("analyze_form"):
        domain = st.text_input("Company website (domain or URL)", placeholder="e.g. linear.app")
        submitted = st.form_submit_button("Analyze")

    if submitted:
        if not domain.strip():
            st.error("Please enter a domain or URL.")
        else:
            with st.spinner(f"Analyzing {domain}..."):
                try:
                    result, evidence = analyze_company(domain.strip(), settings)
                except Exception as exc:  # noqa: BLE001 -- surface any pipeline failure to the UI instead of crashing it
                    st.error(f"Analysis failed: {exc}")
                else:
                    save_result(settings.db_path, result, evidence)
                    st.success(f"Analyzed {result.domain}")
                    _render_result(result)
                    st.divider()
                    st.markdown("### Collected data (raw evidence)")
                    _render_evidence(evidence)

    st.divider()
    st.subheader("History")
    tier_filter = st.selectbox("Filter by tier", ["all", "hot", "warm", "cold", "excluded"])
    history = list_results(settings.db_path, tier=None if tier_filter == "all" else tier_filter, limit=50)

    if not history:
        st.caption("No leads scored yet.")
    else:
        st.dataframe(
            [
                {
                    "Domain": r.domain,
                    "Score": r.overall_score,
                    "Tier": r.tier.value,
                    "Provider": r.provider_used,
                    "Analyzed at": r.generated_at.strftime("%Y-%m-%d %H:%M"),
                }
                for r in history
            ],
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("#### Inspect collected data for a past analysis")
        domains = sorted({r.domain for r in history})
        selected_domain = st.selectbox("Domain", domains)
        if st.button("Show collected data"):
            past_evidence = get_evidence_for_domain(settings.db_path, selected_domain)
            if past_evidence is None:
                st.warning("No stored evidence for this domain (it may predate this feature).")
            else:
                _render_evidence(past_evidence)


if __name__ == "__main__":
    main()
